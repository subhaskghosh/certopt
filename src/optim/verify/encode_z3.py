"""Z3 encoding primitives for IR verification and witness synthesis.

Provides:
  - Bounded domain sorts (INT, STRING, DATE as finite Z3 sorts)
  - Nullable value encoding: each value is (is_null: Bool, val: Sort)
  - SQL 3-valued logic helpers: sql_eq, sql_and, sql_or, sql_not
  - Truth value enum: TRUE=0, FALSE=1, UNKNOWN=2
"""

# pyright: reportArgumentType=false, reportReturnType=false, reportOperatorIssue=false
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

import z3

from ..ir.types import SemType

logger = logging.getLogger(__name__)

# Counter to generate unique Z3 enum sort names across calls
_sort_counter_lock = threading.Lock()
_sort_counter = 0


def _next_sort_id() -> int:
    global _sort_counter
    with _sort_counter_lock:
        _sort_counter += 1
        return _sort_counter


# ---------------------------------------------------------------------------
# Bounded semantics scope
# ---------------------------------------------------------------------------

@dataclass
class BoundedScope:
    """Parameters for bounded verification / witness synthesis.

    This is the Σ(k, D, NULL) from the paper.
    """
    k_rows: int = 3
    int_bounds: tuple[int, int] = (-10, 10)
    string_symbols: list[str] = field(default_factory=lambda: ["s0", "s1", "s2", "s3"])
    date_values: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    null_semantics: bool = True  # 3-valued logic
    solver_timeout_ms: int = 30_000

    def int_range(self) -> range:
        return range(self.int_bounds[0], self.int_bounds[1] + 1)


# ---------------------------------------------------------------------------
# Z3 sort factory
# ---------------------------------------------------------------------------

@dataclass
class Z3Sorts:
    """Cached Z3 sorts for a given bounded scope."""
    int_sort: z3.SortRef
    bool_sort: z3.SortRef
    string_sort: z3.SortRef
    date_sort: z3.SortRef
    # The 3-valued truth sort: TRUE=0, FALSE=1, UNKNOWN=2
    truth_sort: z3.SortRef
    truth_TRUE: z3.ExprRef
    truth_FALSE: z3.ExprRef
    truth_UNKNOWN: z3.ExprRef
    # String symbol constants
    string_consts: dict[str, z3.ExprRef] = field(default_factory=dict)


def make_sorts(scope: BoundedScope) -> Z3Sorts:
    """Create Z3 sorts for the bounded scope."""
    sid = _next_sort_id()
    int_sort = z3.IntSort()
    bool_sort = z3.BoolSort()

    # Strings as an enumeration sort (unique name per call to avoid Z3 collisions)
    if scope.string_symbols:
        str_sort, str_consts = z3.EnumSort(
            f"StringSym_{sid}",
            scope.string_symbols,
        )
        str_map = dict(zip(scope.string_symbols, str_consts))
    else:
        logger.debug("Empty string_symbols — falling back to IntSort for strings")
        str_sort = z3.IntSort()
        str_map = {}

    # Dates as integers (day offsets)
    date_sort = z3.IntSort()

    # 3-valued truth (unique name per call)
    truth_sort, (t_true, t_false, t_unknown) = z3.EnumSort(
        f"Truth3_{sid}",
        ["T_TRUE", "T_FALSE", "T_UNKNOWN"],
    )

    return Z3Sorts(
        int_sort=int_sort,
        bool_sort=bool_sort,
        string_sort=str_sort,
        date_sort=date_sort,
        truth_sort=truth_sort,
        truth_TRUE=t_true,
        truth_FALSE=t_false,
        truth_UNKNOWN=t_unknown,
        string_consts=str_map,
    )


def sort_for_type(sem_type: SemType, sorts: Z3Sorts) -> z3.SortRef:
    """Map a SemType to its Z3 sort."""
    mapping = {
        SemType.INT: sorts.int_sort,
        SemType.FLOAT: sorts.int_sort,  # Approximate floats as ints in bounded scope
        SemType.DECIMAL: sorts.int_sort,
        SemType.BOOL: sorts.bool_sort,
        SemType.STRING: sorts.string_sort,
        SemType.DATE: sorts.date_sort,
        SemType.TIMESTAMP: sorts.date_sort,
    }
    return mapping.get(sem_type, sorts.int_sort)


# ---------------------------------------------------------------------------
# Nullable value: (is_null, val)
# ---------------------------------------------------------------------------

@dataclass
class NullableVar:
    """A nullable Z3 variable: pair of (is_null: Bool, val: Sort)."""
    is_null: z3.ExprRef
    val: z3.ExprRef
    sem_type: SemType = SemType.UNKNOWN

    @property
    def sort(self) -> z3.SortRef:
        return self.val.sort()


def make_nullable_var(
    name: str,
    sem_type: SemType,
    sorts: Z3Sorts,
    nullable: bool = True,
) -> NullableVar:
    """Create a nullable Z3 variable."""
    s = sort_for_type(sem_type, sorts)
    val = z3.Const(f"{name}_val", s)
    if nullable:
        is_null = z3.Bool(f"{name}_null")
    else:
        is_null = z3.BoolVal(False)
    return NullableVar(is_null=is_null, val=val, sem_type=sem_type)


def domain_constraints(var: NullableVar, scope: BoundedScope) -> list[z3.ExprRef]:
    """Generate domain bound constraints for a nullable variable."""
    constraints: list[z3.ExprRef] = []

    if var.sem_type in (SemType.INT, SemType.FLOAT, SemType.DECIMAL):
        lo, hi = scope.int_bounds
        constraints.append(
            z3.Implies(z3.Not(var.is_null), z3.And(var.val >= lo, var.val <= hi))
        )
    elif var.sem_type in (SemType.DATE, SemType.TIMESTAMP):
        if scope.date_values:
            date_options = z3.Or([var.val == d for d in scope.date_values])
            constraints.append(z3.Implies(z3.Not(var.is_null), date_options))

    return constraints


# ---------------------------------------------------------------------------
# SQL 3-valued logic (3VL)
# ---------------------------------------------------------------------------

def sql_eq(
    a: NullableVar, b: NullableVar, sorts: Z3Sorts
) -> z3.ExprRef:
    """SQL equality with 3-valued logic.

    NULL = anything → UNKNOWN
    a = b → TRUE if equal, FALSE otherwise
    Returns a Truth3 value.
    """
    either_null = z3.Or(a.is_null, b.is_null)
    vals_equal = a.val == b.val
    return z3.If(
        either_null,
        sorts.truth_UNKNOWN,
        z3.If(vals_equal, sorts.truth_TRUE, sorts.truth_FALSE),
    )


def sql_neq(
    a: NullableVar, b: NullableVar, sorts: Z3Sorts
) -> z3.ExprRef:
    """SQL != with 3VL."""
    either_null = z3.Or(a.is_null, b.is_null)
    vals_neq = a.val != b.val
    return z3.If(
        either_null,
        sorts.truth_UNKNOWN,
        z3.If(vals_neq, sorts.truth_TRUE, sorts.truth_FALSE),
    )


def sql_lt(
    a: NullableVar, b: NullableVar, sorts: Z3Sorts
) -> z3.ExprRef:
    """SQL < with 3VL."""
    either_null = z3.Or(a.is_null, b.is_null)
    return z3.If(
        either_null,
        sorts.truth_UNKNOWN,
        z3.If(a.val < b.val, sorts.truth_TRUE, sorts.truth_FALSE),
    )


def sql_lte(
    a: NullableVar, b: NullableVar, sorts: Z3Sorts
) -> z3.ExprRef:
    """SQL <= with 3VL."""
    either_null = z3.Or(a.is_null, b.is_null)
    return z3.If(
        either_null,
        sorts.truth_UNKNOWN,
        z3.If(a.val <= b.val, sorts.truth_TRUE, sorts.truth_FALSE),
    )


def sql_gt(
    a: NullableVar, b: NullableVar, sorts: Z3Sorts
) -> z3.ExprRef:
    """SQL > with 3VL."""
    either_null = z3.Or(a.is_null, b.is_null)
    return z3.If(
        either_null,
        sorts.truth_UNKNOWN,
        z3.If(a.val > b.val, sorts.truth_TRUE, sorts.truth_FALSE),
    )


def sql_gte(
    a: NullableVar, b: NullableVar, sorts: Z3Sorts
) -> z3.ExprRef:
    """SQL >= with 3VL."""
    either_null = z3.Or(a.is_null, b.is_null)
    return z3.If(
        either_null,
        sorts.truth_UNKNOWN,
        z3.If(a.val >= b.val, sorts.truth_TRUE, sorts.truth_FALSE),
    )


def sql_and(a: z3.ExprRef, b: z3.ExprRef, sorts: Z3Sorts) -> z3.ExprRef:
    """SQL AND with 3VL.

    TRUE AND TRUE = TRUE
    FALSE AND _ = FALSE
    _ AND FALSE = FALSE
    otherwise UNKNOWN
    """
    return z3.If(
        z3.Or(a == sorts.truth_FALSE, b == sorts.truth_FALSE),
        sorts.truth_FALSE,
        z3.If(
            z3.And(a == sorts.truth_TRUE, b == sorts.truth_TRUE),
            sorts.truth_TRUE,
            sorts.truth_UNKNOWN,
        ),
    )


def sql_or(a: z3.ExprRef, b: z3.ExprRef, sorts: Z3Sorts) -> z3.ExprRef:
    """SQL OR with 3VL.

    TRUE OR _ = TRUE
    _ OR TRUE = TRUE
    FALSE OR FALSE = FALSE
    otherwise UNKNOWN
    """
    return z3.If(
        z3.Or(a == sorts.truth_TRUE, b == sorts.truth_TRUE),
        sorts.truth_TRUE,
        z3.If(
            z3.And(a == sorts.truth_FALSE, b == sorts.truth_FALSE),
            sorts.truth_FALSE,
            sorts.truth_UNKNOWN,
        ),
    )


def sql_not(a: z3.ExprRef, sorts: Z3Sorts) -> z3.ExprRef:
    """SQL NOT with 3VL.

    NOT TRUE = FALSE
    NOT FALSE = TRUE
    NOT UNKNOWN = UNKNOWN
    """
    return z3.If(
        a == sorts.truth_TRUE,
        sorts.truth_FALSE,
        z3.If(a == sorts.truth_FALSE, sorts.truth_TRUE, sorts.truth_UNKNOWN),
    )


def sql_is_null_check(var: NullableVar, sorts: Z3Sorts) -> z3.ExprRef:
    """SQL IS NULL — always returns TRUE or FALSE (never UNKNOWN)."""
    return z3.If(var.is_null, sorts.truth_TRUE, sorts.truth_FALSE)


def sql_is_not_null_check(var: NullableVar, sorts: Z3Sorts) -> z3.ExprRef:
    """SQL IS NOT NULL — always returns TRUE or FALSE."""
    return z3.If(var.is_null, sorts.truth_FALSE, sorts.truth_TRUE)


def truth_is_true(truth_val: z3.ExprRef, sorts: Z3Sorts) -> z3.ExprRef:
    """Convert a 3VL truth value to a Bool: TRUE → true, else → false.

    This is used for WHERE clause filtering: keep only rows where
    the predicate is TRUE (not FALSE, not UNKNOWN).
    """
    return truth_val == sorts.truth_TRUE
