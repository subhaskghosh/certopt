"""Witness synthesis: find a distinguishing DB for two candidate queries.

Given two bound candidate queries Q1, Q2, synthesize a tiny database
instance D ∈ Σ such that ⟦Q1⟧_D ≠ ⟦Q2⟧_D.

If SAT  → witness DB proves the queries are semantically different.
If UNSAT → the queries are equivalent under scope Σ.

Encoding strategy (bounded evaluation):
  - Each table gets k symbolic rows (IntSort values + null flags).
  - Joins are evaluated as cartesian products over row indices.
  - WHERE/ON predicates use SQL 3-valued logic; only TRUE rows survive.
  - GROUP BY + aggregates computed as bounded sums over surviving combos.
  - Difference predicate asserts the two query results disagree.
"""

# pyright: reportArgumentType=false, reportReturnType=false, reportAttributeAccessIssue=false, reportOperatorIssue=false
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import product
from typing import Optional

logger = logging.getLogger(__name__)

import z3

from ..ir.types import (
    AggCall,
    AggFunc,
    Between,
    BinOp,
    BinOpKind,
    CaseExpr,
    CaseWhen,
    ColumnRef,
    ExistsSubquery,
    Expr,
    FuncCall,
    InList,
    InSubquery,
    JoinType,
    Literal,
    QueryIR,
    ScalarSubquery,
    SemType,
    SetOpKind,
    SortDir,
    SortSpec,
    Star,
    UnaryOp,
    UnaryOpKind,
    WindowFunc,
)
from ..schema.catalog import Catalog, ColumnInfo, TableInfo
from ..verify.encode_z3 import BoundedScope


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class NullableVal:
    """A nullable Z3 value: (is_null: Bool, val: Int)."""
    is_null: z3.ExprRef
    val: z3.ExprRef


@dataclass
class TriBool:
    """SQL 3-valued boolean: TRUE, FALSE, or UNKNOWN (NULL).

    is_unknown: True when the predicate result is UNKNOWN (NULL).
    val: The boolean value (meaningful only when not unknown).
    """
    is_unknown: z3.ExprRef
    val: z3.ExprRef


def _tb_true(tb: TriBool) -> z3.ExprRef:
    """Returns z3 Bool: True iff tb is TRUE (not UNKNOWN, not FALSE)."""
    return z3.And(z3.Not(tb.is_unknown), tb.val)


def _tb_not(tb: TriBool) -> TriBool:
    """SQL NOT: NOT TRUE→FALSE, NOT FALSE→TRUE, NOT UNKNOWN→UNKNOWN."""
    return TriBool(is_unknown=tb.is_unknown, val=z3.Not(tb.val))


def _tb_and(a: TriBool, b: TriBool) -> TriBool:
    """SQL AND with 3VL: FALSE dominates UNKNOWN."""
    a_false = z3.And(z3.Not(a.is_unknown), z3.Not(a.val))
    b_false = z3.And(z3.Not(b.is_unknown), z3.Not(b.val))
    either_false = z3.Or(a_false, b_false)
    either_unknown = z3.Or(a.is_unknown, b.is_unknown)
    return TriBool(
        is_unknown=z3.And(z3.Not(either_false), either_unknown),
        val=z3.And(a.val, b.val),
    )


def _tb_or(a: TriBool, b: TriBool) -> TriBool:
    """SQL OR with 3VL: TRUE dominates UNKNOWN."""
    a_true = _tb_true(a)
    b_true = _tb_true(b)
    either_true = z3.Or(a_true, b_true)
    either_unknown = z3.Or(a.is_unknown, b.is_unknown)
    return TriBool(
        is_unknown=z3.And(z3.Not(either_true), either_unknown),
        val=z3.Or(a.val, b.val),
    )


def _tb_from_bool(b: z3.ExprRef) -> TriBool:
    """Lift a definite Bool into TriBool (never UNKNOWN)."""
    return TriBool(is_unknown=z3.BoolVal(False), val=b)


def _tb_unknown() -> TriBool:
    """An UNKNOWN TriBool."""
    return TriBool(is_unknown=z3.BoolVal(True), val=z3.BoolVal(False))


@dataclass
class SymbolicRow:
    """One symbolic row: columns as NullableVal.

    The ``present`` flag indicates whether this row actually exists in
    the relation.  Base-table rows are always present.  Rows produced by
    compositional derived-table encoding carry ``present = inner_survives``
    so that non-surviving inner rows are treated as *absent* rather than
    as all-NULL rows (which are a legitimate SQL value).
    """
    cols: dict[str, NullableVal]
    present: z3.ExprRef = field(default_factory=lambda: z3.BoolVal(True))


@dataclass
class SymbolicTable:
    """k symbolic rows for one table."""
    name: str
    rows: list[SymbolicRow]
    col_types: dict[str, SemType]


@dataclass
class SymbolicDB:
    """All symbolic tables for witness synthesis."""
    tables: dict[str, SymbolicTable]


# A "combo" is one row combination across all tables in a query
# Represented as a dict: alias → row index
Combo = dict[str, int]


@dataclass
class ResultRow:
    """One potential output row of a query."""
    survives: z3.ExprRef  # does this combo survive filtering?
    values: list[NullableVal]  # Projected output values


@dataclass
class WitnessResult:
    """Result of witness synthesis.

    FIX.28b: ``proven_k`` records the cardinality bound at which the
    proof was actually established.  When the adaptive verifier returns
    a lower-k UNSAT because a higher-k schedule step was UNKNOWN/TMO,
    ``proven_k`` is set to the lower k and ``complete`` is False.
    Consumers that build certificates should record this so that the
    proof explicitly states its bound (UNSAT at k=2 does NOT imply
    UNSAT at k=8 for queries with cardinality predicates like
    ``HAVING COUNT(*) >= 3``).
    """
    status: str  # "sat", "unsat", "unknown", "timeout"
    witness_db: Optional[dict[str, list[dict[str, object]]]] = None
    solver_time_ms: float = 0.0
    proven_k: Optional[int] = None
    complete: bool = True


@dataclass
class WitnessStats:
    """Instrumentation debug counters for witness synthesis."""
    distinct_mismatch_pairs: int = 0
    distinct_mismatch_unsat: int = 0
    caseexpr_present_pairs: int = 0
    caseexpr_sat: int = 0
    caseexpr_unsat: int = 0
    caseexpr_unknown: int = 0
    total_pairs: int = 0
    total_sat: int = 0
    total_unsat: int = 0
    total_unknown: int = 0
    total_timeout: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


_stats = WitnessStats()


def get_witness_stats() -> WitnessStats:
    """Return the module-level witness stats singleton."""
    return _stats


def reset_witness_stats() -> None:
    """Reset all witness stats counters to zero."""
    global _stats
    _stats = WitnessStats()


def _contains_case_expr(exprs: list) -> bool:
    """Check if any expression in the list is a CaseExpr."""
    return any(isinstance(e, CaseExpr) for e in exprs)


# ---------------------------------------------------------------------------
# FIX.32n: Bounded-k completeness guard helpers
# ---------------------------------------------------------------------------

def _max_having_count_threshold(ir: QueryIR) -> int:
    """Max required group size from HAVING COUNT predicates.

    COUNT(*) > N  → requires N+1 rows; COUNT(*) >= N → requires N rows.
    Recurses into DTs and set_right.
    """
    max_required = 0

    def _extract(expr) -> None:
        nonlocal max_required
        if expr is None:
            return
        if isinstance(expr, BinOp):
            if expr.op in (BinOpKind.AND, BinOpKind.OR):
                _extract(expr.left)
                _extract(expr.right)
                return
            if isinstance(expr.left, AggCall) and expr.left.func == AggFunc.COUNT:
                if isinstance(expr.right, Literal) and isinstance(expr.right.value, (int, float)):
                    n = int(expr.right.value)
                    if expr.op == BinOpKind.GT:
                        max_required = max(max_required, n + 1)
                    elif expr.op in (BinOpKind.GTE, BinOpKind.EQ):
                        max_required = max(max_required, n)
                    return
            if isinstance(expr.right, AggCall) and expr.right.func == AggFunc.COUNT:
                if isinstance(expr.left, Literal) and isinstance(expr.left.value, (int, float)):
                    n = int(expr.left.value)
                    if expr.op == BinOpKind.LT:
                        max_required = max(max_required, n + 1)
                    elif expr.op in (BinOpKind.LTE, BinOpKind.EQ):
                        max_required = max(max_required, n)
                    return

    _extract(ir.having)
    from ..ir.types import DerivedTable
    if isinstance(ir.from_table, DerivedTable):
        max_required = max(max_required, _max_having_count_threshold(ir.from_table.query))
    for j in ir.joins:
        if isinstance(j.right, DerivedTable):
            max_required = max(max_required, _max_having_count_threshold(j.right.query))
    if ir.set_right is not None:
        max_required = max(max_required, _max_having_count_threshold(ir.set_right))
    return max_required


def _dt_agg_signatures(ir: QueryIR) -> list[tuple[str, bool]]:
    """Collect (base_table, has_group_by) for each aggregated DT."""
    from ..ir.types import DerivedTable, RelRef
    sigs: list[tuple[str, bool]] = []
    def _check(rel):
        if isinstance(rel, DerivedTable):
            inner = rel.query
            if inner.has_aggregation():
                base = ""
                if isinstance(inner.from_table, RelRef):
                    base = inner.from_table.table.lower()
                sigs.append((base, bool(inner.group_by)))
            _check(inner.from_table)
            for j in inner.joins:
                _check(j.right)
    _check(ir.from_table)
    for j in ir.joins:
        _check(j.right)
    return sigs


def _count_table_aliases(ir: QueryIR) -> dict[str, int]:
    """Count how many aliases each base table has at the top query level.

    Only counts tables that participate in the same cartesian product /
    join tree.  Aggregated DTs are opaque boundaries (the compositional
    encoding handles them separately), so we don't recurse into them.
    Non-aggregated DTs (which will be inlined) are recursed into.
    """
    from ..ir.types import DerivedTable, RelRef
    counts: dict[str, int] = {}
    def _add(rel):
        if isinstance(rel, RelRef):
            base = rel.table.lower()
            counts[base] = counts.get(base, 0) + 1
        elif isinstance(rel, DerivedTable):
            inner = rel.query
            # Only recurse into non-aggregated DTs (they get inlined)
            if not inner.has_aggregation() and not inner.group_by:
                _inner(inner)
    def _inner(q: QueryIR):
        _add(q.from_table)
        for j in q.joins:
            _add(j.right)
    _inner(ir)
    return counts


def _check_bounded_k_guards(
    q1: QueryIR, q2: QueryIR, k: int,
) -> str | None:
    """Run FIX.32n bounded-k completeness guards.

    Returns a reason string if a guard fires, or None if all pass.
    """
    # (a) HAVING COUNT threshold
    having_thresh = max(_max_having_count_threshold(q1), _max_having_count_threshold(q2))
    if having_thresh > k:
        return (
            f"HAVING COUNT requires {having_thresh} rows > k={k}"
        )

    # (b) DT GROUP BY asymmetry
    sigs_q1 = _dt_agg_signatures(q1)
    sigs_q2 = _dt_agg_signatures(q2)
    for base1, has_gb1 in sigs_q1:
        for base2, has_gb2 in sigs_q2:
            if base1 and base1 == base2 and has_gb1 != has_gb2:
                return (
                    f"DT GROUP BY asymmetry on '{base1}'"
                )

    # (c) Self-join asymmetry
    aliases_q1 = _count_table_aliases(q1)
    aliases_q2 = _count_table_aliases(q2)
    for base in set(aliases_q1.keys()) | set(aliases_q2.keys()):
        c1 = aliases_q1.get(base, 0)
        c2 = aliases_q2.get(base, 0)
        if (c1 >= 1 and c2 >= 1 and c1 != c2
                and not base.startswith("__")
                and max(c1, c2) >= k):
            return (
                f"self-join asymmetry on '{base}' ({c1} vs {c2}) with k={k}"
            )

    return None



# ---------------------------------------------------------------------------
# Star expansion (SELECT * → explicit columns)
# ---------------------------------------------------------------------------

def _expand_stars(ir: QueryIR, catalog: Catalog) -> QueryIR:
    """Expand Star() in SELECT to explicit ColumnRef nodes.

    Handles base tables (via catalog) and derived tables (via inner
    query's projected columns + column_aliases).  Recurses into
    set_right and into inner derived table queries.
    """
    from ..ir.types import DerivedTable, RelRef

    # First, recursively expand stars in inner derived table queries
    # so that _get_projected_col_names_for_star can resolve nested stars
    changed = False

    def _expand_rel(rel):
        nonlocal changed
        if isinstance(rel, DerivedTable):
            new_inner = _expand_stars(rel.query, catalog)
            if new_inner is not rel.query:
                rel = DerivedTable(
                    query=new_inner,
                    alias=rel.alias,
                    column_aliases=rel.column_aliases,
                )
                changed = True
        return rel

    new_from = _expand_rel(ir.from_table)
    new_joins = []
    for j in ir.joins:
        from ..ir.types import JoinClause
        new_right = _expand_rel(j.right)
        if new_right is not j.right:
            new_joins.append(JoinClause(
                join_type=j.join_type, right=new_right, on=j.on,
            ))
        else:
            new_joins.append(j)

    if changed:
        ir = ir.model_copy(deep=True)
        ir.from_table = new_from
        ir.joins = new_joins

    def _is_star(e) -> bool:
        """Check if expression is a Star or qualified star (ColumnRef with column='*')."""
        if isinstance(e, Star):
            return True
        # FIX.25c: Qualified star e.g. A.* parsed as ColumnRef(table='A', column='*')
        if isinstance(e, ColumnRef) and e.column == '*':
            return True
        return False

    if not any(_is_star(e) for e in ir.select):
        # Still recurse into set_right
        if ir.set_right:
            new_right = _expand_stars(ir.set_right, catalog)
            if new_right is not ir.set_right:
                ir = ir.model_copy(deep=True)
                ir.set_right = new_right
        return ir

    def _columns_for_rel(rel) -> list[ColumnRef]:
        """Get column refs for a relation (base table or derived table)."""
        if isinstance(rel, RelRef):
            tinfo = catalog.get_table(rel.table)
            if tinfo is None:
                return []
            alias = rel.alias or rel.table
            return [
                ColumnRef(table=alias, column=c.name, sem_type=c.sem_type)
                for c in tinfo.columns
            ]
        elif isinstance(rel, DerivedTable):
            inner_names = _get_projected_col_names_for_star(rel)
            alias = rel.alias
            return [
                ColumnRef(table=alias, column=name)
                for name in inner_names
            ]
        return []

    def _get_alias_for_rel(rel) -> str | None:
        """Get the alias name for a relation."""
        if isinstance(rel, RelRef):
            return (rel.alias or rel.table).lower()
        elif isinstance(rel, DerivedTable):
            return rel.alias.lower()
        return None

    # Build mapping: alias → relation (for qualified star resolution)
    alias_to_rel: dict[str, object] = {}
    alias_to_rel[_get_alias_for_rel(ir.from_table) or ''] = ir.from_table
    for j in ir.joins:
        a = _get_alias_for_rel(j.right)
        if a:
            alias_to_rel[a] = j.right

    # Collect all columns from FROM + JOINs
    expanded: list = []
    for expr in ir.select:
        if isinstance(expr, Star):
            cols = _columns_for_rel(ir.from_table)
            for j in ir.joins:
                cols.extend(_columns_for_rel(j.right))
            if cols:
                expanded.extend(cols)
            else:
                expanded.append(expr)
        elif isinstance(expr, ColumnRef) and expr.column == '*':
            # FIX.25c: Qualified star (e.g., A.*)
            qual = expr.table.lower() if expr.table else ''
            target_rel = alias_to_rel.get(qual)
            if target_rel is not None:
                cols = _columns_for_rel(target_rel)
                if cols:
                    expanded.extend(cols)
                else:
                    expanded.append(expr)
            else:
                expanded.append(expr)
        else:
            expanded.append(expr)

    new_ir = ir.model_copy(deep=True)
    new_ir.select = expanded

    # Recurse into set_right
    if new_ir.set_right:
        new_ir.set_right = _expand_stars(new_ir.set_right, catalog)

    return new_ir


def _normalize_column_order(q1: QueryIR, q2: QueryIR) -> tuple[QueryIR, QueryIR]:
    """Reorder Q2's SELECT to match Q1's column order when names are identical.

    FIX.20b: When both queries project the same multiset of column names
    but in a different order (e.g., ``SELECT *`` expands to catalog order
    while the other query lists columns explicitly), the positional
    comparison would produce a spurious difference.  Reorder Q2 to match Q1.
    """
    if len(q1.select) != len(q2.select):
        return q1, q2

    def _col_name(expr: Expr) -> Optional[str]:
        if hasattr(expr, 'alias') and expr.alias:
            return expr.alias.upper()
        if isinstance(expr, ColumnRef):
            return expr.column.upper()
        return None

    q1_names = [_col_name(e) for e in q1.select]
    q2_names = [_col_name(e) for e in q2.select]

    # Only reorder if every name is non-None, they have the same multiset,
    # and the order differs.
    if (None in q1_names or None in q2_names
            or sorted(q1_names) != sorted(q2_names)
            or q1_names == q2_names):
        return q1, q2

    # Build mapping: for each name in Q1's order, find the corresponding
    # expression in Q2.
    q2_by_name: dict[str, list[Expr]] = {}
    for name, expr in zip(q2_names, q2.select):
        q2_by_name.setdefault(name, []).append(expr)

    reordered: list[Expr] = []
    for name in q1_names:
        reordered.append(q2_by_name[name].pop(0))

    q2 = QueryIR(
        select=reordered,
        from_table=q2.from_table,
        joins=q2.joins,
        where=q2.where,
        group_by=q2.group_by,
        having=q2.having,
        order_by=q2.order_by,
        limit=q2.limit,
        distinct=q2.distinct,
        set_op=q2.set_op,
        set_right=q2.set_right,
    )
    return q1, q2


def _get_projected_col_names_for_star(derived) -> list[str]:
    """Get projected column names for star expansion through a DerivedTable.

    Handles column_aliases, set_op (uses left branch), and nested Stars.
    """
    inner = derived.query
    col_aliases = getattr(derived, 'column_aliases', [])

    # For set-op queries, use the left branch's SELECT list for column names
    select_list = inner.select
    if inner.set_op is not None and inner.select:
        # set_op queries: column names come from the left branch
        pass  # use inner.select which is already the left branch's SELECT

    # Get names from inner SELECT
    names: list[str] = []
    for idx, expr in enumerate(select_list):
        if isinstance(expr, Star):
            # Nested star — can't resolve without catalog context
            # Return empty to signal failure
            return []
        if col_aliases and idx < len(col_aliases):
            names.append(col_aliases[idx])
        elif expr.alias:
            names.append(expr.alias)
        elif isinstance(expr, ColumnRef):
            names.append(expr.column)
        elif isinstance(expr, AggCall):
            arg_name = ""
            if expr.arg and isinstance(expr.arg, ColumnRef):
                arg_name = expr.arg.column
            prefix = "distinct_" if expr.distinct else ""
            name = f"{expr.func.value.lower()}_{prefix}{arg_name}".rstrip("_")
            names.append(name)
        elif isinstance(expr, BinOp):
            # Arithmetic expression: try to derive a meaningful name
            names.append(f"expr_{idx}")
        else:
            names.append(f"expr_{idx}")

    return names


# ---------------------------------------------------------------------------
# Derived-table schema extraction
# ---------------------------------------------------------------------------

def _collect_derived_table_schemas(
    ir: QueryIR,
    catalog: Catalog,
) -> dict[str, list[tuple[str, SemType]]]:
    """Extract projected column schemas from DerivedTable nodes.

    Returns: alias → [(col_name, SemType), ...]
    """
    from ..ir.types import DerivedTable

    schemas: dict[str, list[tuple[str, SemType]]] = {}

    def _process(rel):
        if not isinstance(rel, DerivedTable):
            return
        inner = rel.query
        col_aliases = getattr(rel, 'column_aliases', [])
        cols: list[tuple[str, SemType]] = []
        for idx, expr in enumerate(inner.select):
            # FIX.13e: Respect column_aliases from DerivedTable
            if col_aliases and idx < len(col_aliases):
                name = col_aliases[idx]
            elif expr.alias:
                name = expr.alias
            elif isinstance(expr, ColumnRef):
                name = expr.column
            elif isinstance(expr, AggCall):
                arg_name = ""
                if expr.arg and isinstance(expr.arg, ColumnRef):
                    arg_name = expr.arg.column
                prefix = "distinct_" if expr.distinct else ""
                name = f"{expr.func.value.lower()}_{prefix}{arg_name}".rstrip("_")
            else:
                name = f"expr_{idx}"

            # Infer type
            sem_type = expr.sem_type
            if sem_type == SemType.UNKNOWN and isinstance(expr, ColumnRef):
                inner_tables = _get_tables_for_query(inner)
                actual = None
                if expr.table:
                    actual = inner_tables.get(expr.table.lower())
                else:
                    for tbl in inner_tables.values():
                        if catalog.get_column(tbl, expr.column):
                            actual = tbl
                            break
                if actual:
                    col_info = catalog.get_column(actual, expr.column)
                    if col_info:
                        sem_type = col_info.sem_type
            if sem_type == SemType.UNKNOWN and isinstance(expr, AggCall):
                if expr.func == AggFunc.COUNT:
                    sem_type = SemType.INT
                elif expr.arg and expr.arg.sem_type != SemType.UNKNOWN:
                    sem_type = expr.arg.sem_type

            cols.append((name, sem_type if sem_type != SemType.UNKNOWN else SemType.INT))
        schemas[rel.alias.lower()] = cols

    def _recurse(query: QueryIR):
        """Process all DTs in a query, recursing into nested DTs."""
        _process(query.from_table)
        for join in query.joins:
            _process(join.right)
        # FIX.20a: Recurse into nested DT inner queries so schemas for
        # deeply-nested DTs (e.g., t3/t4 inside t6) are also collected.
        if isinstance(query.from_table, DerivedTable):
            _recurse(query.from_table.query)
        for join in query.joins:
            if isinstance(join.right, DerivedTable):
                _recurse(join.right.query)

    _recurse(ir)
    return schemas


def _substitute_refs(
    expr: Expr, alias: str, col_map: dict[str, Expr],
    exclude_names: frozenset[str] | None = None,
) -> Expr:
    """Replace ColumnRef(table=alias, column=col) with col_map[col].

    Returns a new expression tree with derived-table refs substituted.

    *exclude_names*: unqualified column names that should NOT be substituted
    even if they match a col_map key (used to protect table aliases from
    being mis-substituted as column names in textbook-style abstract predicates).
    """
    if isinstance(expr, ColumnRef):
        if expr.table and expr.table.lower() == alias.lower():
            replacement = col_map.get(expr.column.lower())
            if replacement is not None:
                result = replacement.model_copy(deep=True)
                if expr.alias:
                    result.alias = expr.alias
                return result
        # Unqualified ref matching a derived col
        if not expr.table and expr.column.lower() in col_map:
            # FIX.36a: Don't substitute if the name is a protected table alias.
            if exclude_names and expr.column.lower() in exclude_names:
                return expr
            replacement = col_map[expr.column.lower()]
            result = replacement.model_copy(deep=True)
            if expr.alias:
                result.alias = expr.alias
            return result
        # FIX.36a: Unqualified ref matching the DT alias (tuple ref).
        # Textbook SQL uses B(X) where X is a table alias meaning "apply
        # predicate B to the tuple".  When col_map has exactly one column,
        # substitute with that column so B(Y) → B(X) after inlining.
        if not expr.table and expr.column.lower() == alias.lower() and len(col_map) == 1:
            replacement = next(iter(col_map.values()))
            result = replacement.model_copy(deep=True)
            if expr.alias:
                result.alias = expr.alias
            return result
        return expr

    if isinstance(expr, BinOp):
        return BinOp(
            op=expr.op,
            left=_substitute_refs(expr.left, alias, col_map, exclude_names),
            right=_substitute_refs(expr.right, alias, col_map, exclude_names),
            sem_type=expr.sem_type,
            nullability=expr.nullability,
            alias=expr.alias,
        )

    if isinstance(expr, UnaryOp):
        return UnaryOp(
            op=expr.op,
            operand=_substitute_refs(expr.operand, alias, col_map, exclude_names),
            sem_type=expr.sem_type,
            nullability=expr.nullability,
            alias=expr.alias,
        )

    if isinstance(expr, AggCall):
        new_arg = _substitute_refs(expr.arg, alias, col_map, exclude_names) if expr.arg else None
        return AggCall(
            func=expr.func,
            arg=new_arg,
            distinct=expr.distinct,
            sem_type=expr.sem_type,
            nullability=expr.nullability,
            alias=expr.alias,
        )

    if isinstance(expr, FuncCall):
        return FuncCall(
            func_name=expr.func_name,
            args=[_substitute_refs(a, alias, col_map, exclude_names) for a in expr.args],
            sem_type=expr.sem_type,
            nullability=expr.nullability,
            alias=expr.alias,
        )

    if isinstance(expr, InList):
        return InList(
            expr=_substitute_refs(expr.expr, alias, col_map, exclude_names),
            values=[_substitute_refs(v, alias, col_map, exclude_names) for v in expr.values],
            sem_type=expr.sem_type,
            alias=expr.alias,
        )

    if isinstance(expr, Between):
        return Between(
            expr=_substitute_refs(expr.expr, alias, col_map, exclude_names),
            low=_substitute_refs(expr.low, alias, col_map, exclude_names),
            high=_substitute_refs(expr.high, alias, col_map, exclude_names),
            sem_type=expr.sem_type,
            alias=expr.alias,
        )

    if isinstance(expr, CaseExpr):
        new_whens = [
            CaseWhen(
                when=_substitute_refs(cw.when, alias, col_map, exclude_names),
                then=_substitute_refs(cw.then, alias, col_map, exclude_names),
            )
            for cw in expr.whens
        ]
        new_else = _substitute_refs(expr.else_, alias, col_map, exclude_names) if expr.else_ is not None else None
        return CaseExpr(
            whens=new_whens,
            else_=new_else,
            sem_type=expr.sem_type,
            alias=expr.alias,
        )

    # Literal, Star, etc. — no substitution needed
    return expr


def _build_inner_col_map(
    inner: QueryIR,
    column_aliases: list[str] | None = None,
) -> dict[str, Expr]:
    """Build column name → expression mapping from a subquery's SELECT list.

    If *column_aliases* is provided (from DerivedTable.column_aliases),
    each positional alias is mapped in addition to the original name.
    """
    col_map: dict[str, Expr] = {}
    for idx, expr in enumerate(inner.select):
        if expr.alias:
            name = expr.alias.lower()
        elif isinstance(expr, ColumnRef):
            name = expr.column.lower()
        elif isinstance(expr, AggCall):
            arg_name = ""
            if expr.arg and isinstance(expr.arg, ColumnRef):
                arg_name = expr.arg.column
            prefix = "distinct_" if expr.distinct else ""
            name = f"{expr.func.value.lower()}_{prefix}{arg_name}".rstrip("_").lower()
        else:
            name = f"expr_{idx}"
        col_map[name] = expr
        # Apply positional column alias (FIX.7)
        if column_aliases and idx < len(column_aliases):
            col_map[column_aliases[idx].lower()] = expr
    return col_map


def _requalify_table_refs(
    expr: Optional[Expr],
    old_table: str,
    new_table: str,
    from_columns: frozenset[str] | None = None,
) -> Optional[Expr]:
    """Rewrite ColumnRef table qualifiers from old_table to new_table.

    Used when inlining a DT: the inner WHERE has refs qualified by the
    base table name; we requalify them to the DT alias so they resolve
    correctly in the binding.

    *from_columns*: lowercase column names belonging to ``old_table``.
    When provided, unqualified ColumnRefs are only requalified if
    their column name is in this set.  This prevents mis-qualifying
    refs that belong to joined tables (e.g., ``CUSTOMERID`` from
    ORDERS being requalified to the CUSTOMERS alias).
    """
    if expr is None:
        return None
    if isinstance(expr, ColumnRef):
        if (expr.table or "").lower() == old_table.lower():
            return ColumnRef(
                table=new_table, column=expr.column,
                sem_type=expr.sem_type, nullability=expr.nullability,
                alias=expr.alias,
            )
        if not expr.table:
            if from_columns is not None and expr.column.lower() not in from_columns:
                return expr
            return ColumnRef(
                table=new_table, column=expr.column,
                sem_type=expr.sem_type, nullability=expr.nullability,
                alias=expr.alias,
            )
        return expr
    if isinstance(expr, BinOp):
        return BinOp(
            op=expr.op,
            left=_requalify_table_refs(expr.left, old_table, new_table, from_columns),
            right=_requalify_table_refs(expr.right, old_table, new_table, from_columns),
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    if isinstance(expr, UnaryOp):
        return UnaryOp(
            op=expr.op,
            operand=_requalify_table_refs(expr.operand, old_table, new_table, from_columns),
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    if isinstance(expr, FuncCall):
        return FuncCall(
            func_name=expr.func_name,
            args=[_requalify_table_refs(a, old_table, new_table, from_columns) for a in expr.args],
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    if isinstance(expr, CaseExpr):
        return CaseExpr(
            whens=[
                CaseWhen(
                    when=_requalify_table_refs(cw.when, old_table, new_table, from_columns),
                    then=_requalify_table_refs(cw.then, old_table, new_table, from_columns),
                ) for cw in expr.whens
            ],
            else_=_requalify_table_refs(expr.else_, old_table, new_table, from_columns),
            sem_type=expr.sem_type, alias=expr.alias,
        )
    if isinstance(expr, InList):
        return InList(
            expr=_requalify_table_refs(expr.expr, old_table, new_table, from_columns),
            values=[_requalify_table_refs(v, old_table, new_table, from_columns) for v in expr.values],
            sem_type=expr.sem_type, alias=expr.alias,
        )
    if isinstance(expr, Between):
        return Between(
            expr=_requalify_table_refs(expr.expr, old_table, new_table, from_columns),
            low=_requalify_table_refs(expr.low, old_table, new_table, from_columns),
            high=_requalify_table_refs(expr.high, old_table, new_table, from_columns),
            sem_type=expr.sem_type, alias=expr.alias,
        )
    if isinstance(expr, AggCall):
        return AggCall(
            func=expr.func,
            arg=_requalify_table_refs(expr.arg, old_table, new_table, from_columns),
            distinct=expr.distinct,
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    return expr


def _inline_join_derived_tables(ir: QueryIR) -> QueryIR:
    """Inline non-aggregated DerivedTable nodes in JOIN positions.

    For each join whose right side is a DerivedTable with a simple
    (non-aggregated) inner query, replace it with the inner's base table
    and substitute column refs.

    Inner WHERE placement depends on join type:
    - INNER/LEFT: merge into ON (safe — unmatched right rows discarded).
    - RIGHT: promote to outer WHERE (right-side columns are never
      NULL-padded, so post-join filter ≡ pre-filter).
    - FULL: cannot safely inline when inner has WHERE — both sides may
      be NULL-padded, so neither ON nor outer WHERE reproduces pre-filter
      semantics.  Left as opaque fallback.

    Aggregated inner queries are left as-is (opaque fallback).
    """
    from ..ir.types import DerivedTable, JoinClause, RelRef, SortSpec

    if not any(isinstance(j.right, DerivedTable) for j in ir.joins):
        return ir

    new_joins: list = []
    col_maps: dict[str, dict[str, Expr]] = {}
    extra_wheres: list[Expr] = []  # inner WHEREs promoted from RIGHT JOINs
    deferred_on_wheres: dict[int, Expr] = {}  # FIX.20b: join_idx → inner WHERE to merge after substitution

    for join in ir.joins:
        if not isinstance(join.right, DerivedTable):
            new_joins.append(join)
            continue

        derived = join.right
        inner = derived.query
        alias = derived.alias.lower()

        # Only inline non-aggregated inner queries without their own joins
        # (splicing inner joins can change outer-join semantics)
        # FIX.16c: Also skip when inner has set_op — inlining would drop
        # the UNION ALL / INTERSECT / EXCEPT structure.
        # FIX.25b: Also skip when inner SELECT has window functions —
        # inlining would push WindowFunc into outer ON/WHERE, losing
        # partition context.
        if (inner.has_aggregation() or inner.group_by or inner.joins
                or inner.set_op
                or any(isinstance(e, WindowFunc) for e in inner.select)):
            new_joins.append(join)
            continue

        # FULL JOIN with inner WHERE: can't safely inline.
        # Both sides may be NULL-padded, so neither ON nor outer WHERE
        # reproduces pre-filter semantics.
        if inner.where and join.join_type == JoinType.FULL:
            new_joins.append(join)
            continue

        # FIX.20a: LEFT/FULL JOIN with non-ColumnRef SELECT expressions
        # (constants, literals, computed values) can't be safely inlined.
        # After inlining, references like `t3.i` (originally `TRUE AS i`)
        # are replaced with the literal TRUE everywhere. But on a LEFT JOIN,
        # when the right side doesn't match, `t3.i` should be NULL (from
        # NULL-padding), not TRUE. Leaving as opaque DT preserves correct
        # NULL-padding via compositional encoding.
        if join.join_type in (JoinType.LEFT, JoinType.FULL):
            has_non_colref = any(
                not isinstance(expr, ColumnRef)
                for expr in inner.select
            )
            if has_non_colref:
                new_joins.append(join)
                continue

        col_map = _build_inner_col_map(inner, derived.column_aliases)

        # When inlining a DT that wraps a single base table, preserve
        # the DT alias so self-joins remain distinguishable.  Requalify
        # inner WHERE column refs to use the DT alias.
        inner_base = inner.from_table
        if isinstance(inner_base, RelRef) and inner_base.alias is None:
            inlined_rel = RelRef(table=inner_base.table, alias=alias)
            inner_table_name = inner_base.table.lower()
            # FIX.16c: Rewrite col_map values to use the DT alias as qualifier.
            # Use _requalify_table_refs to handle both direct ColumnRefs
            # and nested ColumnRefs inside expressions (e.g., BinOp containing
            # ColumnRef(table=None, column='DEPTNO')).
            for cname in list(col_map.keys()):
                col_map[cname] = _requalify_table_refs(col_map[cname], inner_table_name, alias)
            # Requalify inner WHERE to use the DT alias
            inner_where = _requalify_table_refs(inner.where, inner_table_name, alias)
        else:
            inlined_rel = inner_base
            inner_where = inner.where

        col_maps[alias] = col_map

        # Substitute refs in ON clause
        new_on = _substitute_refs(join.on, alias, col_map)

        # FIX.20b: Defer merging inner WHERE into ON.  The inner WHERE
        # references base-table columns (requalified to the DT alias).
        # If merged now, the subsequent col_map substitution pass would
        # replace those base-table refs with the projected expressions
        # (e.g., t6.DEPTNO → Literal(10)), destroying the filter.
        # Store deferred merges keyed by join index.
        if inner_where:
            if join.join_type in (JoinType.INNER, JoinType.LEFT, JoinType.CROSS):
                deferred_on_wheres[len(new_joins)] = inner_where
            elif join.join_type == JoinType.RIGHT:
                extra_wheres.append(inner_where)

        new_joins.append(JoinClause(
            join_type=join.join_type,
            right=inlined_rel,
            on=new_on,
        ))
        # Splice inner's own joins
        new_joins.extend(inner.joins)

    if not col_maps:
        return ir

    # FIX.36a: Collect all table aliases (FROM + JOINs) to protect from
    # mis-substitution.  Textbook SQL uses B(X) where X is a table alias;
    # without this, _substitute_refs would replace ColumnRef(column='X')
    # with the DT's projected column because 'x' matches a col_map key.
    table_aliases: set[str] = set()
    from ..ir.types import RelRef as _RR
    if isinstance(ir.from_table, _RR):
        table_aliases.add((ir.from_table.alias or ir.from_table.table).lower())
    for j in new_joins:
        if isinstance(j.right, _RR):
            table_aliases.add((j.right.alias or j.right.table).lower())
    exclude = frozenset(table_aliases) if table_aliases else None

    # Substitute all refs to inlined aliases across the rest of the IR
    new_select = list(ir.select)
    new_where = ir.where
    new_group_by = list(ir.group_by)
    new_having = ir.having
    new_order_by = list(ir.order_by)

    for alias, col_map in col_maps.items():
        new_select = [_substitute_refs(e, alias, col_map, exclude) for e in new_select]
        if new_where:
            new_where = _substitute_refs(new_where, alias, col_map, exclude)
        new_group_by = [_substitute_refs(g, alias, col_map, exclude) for g in new_group_by]
        if new_having:
            new_having = _substitute_refs(new_having, alias, col_map, exclude)
        new_order_by = [
            SortSpec(expr=_substitute_refs(s.expr, alias, col_map, exclude), direction=s.direction)
            for s in new_order_by
        ]
        # Also substitute in ON clauses of other joins that may reference the alias
        new_joins = [
            JoinClause(
                join_type=j.join_type,
                right=j.right,
                on=_substitute_refs(j.on, alias, col_map, exclude),
            )
            for j in new_joins
        ]

    # FIX.20b: Now merge deferred inner WHEREs into ON clauses.
    # These reference base-table columns (not DT projected columns),
    # so they must be merged AFTER the col_map substitution pass.
    for join_idx, inner_w in deferred_on_wheres.items():
        j = new_joins[join_idx]
        new_joins[join_idx] = JoinClause(
            join_type=j.join_type,
            right=j.right,
            on=BinOp(op=BinOpKind.AND, left=j.on, right=inner_w),
        )

    # Merge promoted RIGHT JOIN inner WHEREs into outer WHERE
    for ew in extra_wheres:
        if new_where:
            new_where = BinOp(op=BinOpKind.AND, left=new_where, right=ew)
        else:
            new_where = ew

    # FIX.19c: Preserve set_op/set_right through join-position DT inlining.
    return QueryIR(
        select=new_select,
        from_table=ir.from_table,
        joins=new_joins,
        where=new_where,
        group_by=new_group_by,
        having=new_having,
        order_by=new_order_by,
        limit=ir.limit,
        distinct=ir.distinct,
        set_op=ir.set_op,
        set_right=ir.set_right,
    )


def _inline_derived_tables(ir: QueryIR, catalog: Catalog) -> QueryIR:
    """Inline DerivedTable nodes by rewriting to use base tables.

    Phase 1: Inline JOIN-position derived tables.
    Phase 2: Inline FROM-position derived table.

    Handles:
    - Non-aggregated inner: splice inner relations and merge WHERE
    - Aggregated inner with non-aggregated outer: promote to single-block query
    - Unsupported patterns: return IR unchanged (graceful fallback)
    """
    from ..ir.types import DerivedTable, JoinClause, RelRef, SortSpec

    # Phase 1: Inline JOIN-position derived tables
    ir = _inline_join_derived_tables(ir)

    # Phase 1b: Recursively inline DTs inside set_right branches.
    # FIX.21a: After inlining the top-level DT, set_right branches may
    # still contain DTs in their FROM position.  Recurse into each branch
    # so that nested DTs (like t4 inside a UNION ALL chain) get inlined.
    if ir.set_right is not None:
        new_set_right = _inline_derived_tables(ir.set_right, catalog)
        if new_set_right is not ir.set_right:
            ir = QueryIR(
                select=ir.select, from_table=ir.from_table, joins=list(ir.joins),
                where=ir.where, group_by=list(ir.group_by), having=ir.having,
                order_by=list(ir.order_by), limit=ir.limit, distinct=ir.distinct,
                set_op=ir.set_op, set_right=new_set_right,
            )

    # Phase 2: Inline FROM-position derived table
    if not isinstance(ir.from_table, DerivedTable):
        return ir

    derived = ir.from_table
    inner = derived.query
    alias = derived.alias.lower()
    col_map = _build_inner_col_map(inner, derived.column_aliases)

    # Case 1: Non-aggregated inner query
    if not inner.has_aggregation() and not inner.group_by:
        # FIX.25a: Don't inline FROM-position DT when inner SELECT
        # contains window functions.  Window functions are computed over
        # the full partition result set; inlining pushes them into the
        # outer WHERE/SELECT, losing partition context.  The compositional
        # encoding via _encode_derived_table_rows handles this correctly.
        if any(isinstance(e, WindowFunc) for e in inner.select):
            return ir  # leave as opaque DT for compositional encoding

        # FIX.17b: Don't inline FROM-position DT with WHERE when the outer
        # has RIGHT or FULL joins.  Moving the inner WHERE to the outer
        # WHERE changes semantics: the inner WHERE pre-filters before the
        # join, but the outer WHERE post-filters after the join (removing
        # NULL-padded rows from the outer side).
        if inner.where and any(
            j.join_type in (JoinType.RIGHT, JoinType.FULL) for j in ir.joins
        ):
            return ir  # leave as opaque DT for compositional encoding

        # FIX.16c: When the inner has a set-op (UNION ALL etc.), only inline
        # if the outer doesn't have GROUP BY/aggregation.  Otherwise the
        # non-set-op path drops the set-op structure, which is incorrect.
        # FIX.18b: Also skip when outer has joins — can't push a cross/inner
        # join into each set-op branch without duplicating the join logic.
        if inner.set_op is not None and (ir.group_by or ir.has_aggregation() or ir.joins):
            return ir  # leave as opaque DT for compositional encoding

        # FIX.13f: For set-op inner queries, push projection/filter into
        # both branches instead of dropping the set_op structure.
        if inner.set_op is not None and not ir.joins and not ir.group_by and not ir.has_aggregation():
            def _push_into_branch(branch: QueryIR, outer_ir: QueryIR, alias: str) -> QueryIR:
                """Push outer projection + filter into one branch of a set-op.

                FIX.16b: recurse into branch.set_right so chained set-ops
                (A UNION ALL B UNION ALL C) get the outer WHERE pushed into
                every branch, not just the first two.
                """
                branch_col_map = _build_inner_col_map(branch, derived.column_aliases)
                new_select = [_substitute_refs(e, alias, branch_col_map) for e in outer_ir.select]
                new_where = branch.where
                if outer_ir.where:
                    outer_w = _substitute_refs(outer_ir.where, alias, branch_col_map)
                    if new_where:
                        new_where = BinOp(op=BinOpKind.AND, left=new_where, right=outer_w)
                    else:
                        new_where = outer_w
                # Recursively push into set_right branches
                new_set_right = branch.set_right
                if new_set_right is not None:
                    new_set_right = _push_into_branch(new_set_right, outer_ir, alias)
                return QueryIR(
                    select=new_select,
                    from_table=branch.from_table,
                    joins=list(branch.joins),
                    where=new_where,
                    group_by=list(branch.group_by),
                    having=branch.having,
                    order_by=[],
                    limit=None,
                    distinct=branch.distinct,
                    set_op=branch.set_op,
                    set_right=new_set_right,
                )

            left_branch = _push_into_branch(inner, ir, alias)
            right_branch = None
            if inner.set_right:
                right_branch = _push_into_branch(inner.set_right, ir, alias)

            new_order_by = [
                SortSpec(expr=_substitute_refs(s.expr, alias, col_map), direction=s.direction)
                for s in ir.order_by
            ]

            result = QueryIR(
                select=left_branch.select,
                from_table=left_branch.from_table,
                joins=left_branch.joins,
                where=left_branch.where,
                group_by=left_branch.group_by,
                having=left_branch.having,
                order_by=new_order_by,
                limit=ir.limit,
                distinct=ir.distinct or inner.distinct,
                set_op=inner.set_op,
                set_right=right_branch,
            )

            # FIX.18b: Preserve outer set_op/set_right chain.
            # When the outer IR has its own set-op (e.g., SELECT * FROM
            # (A UNION ALL B) AS t UNION ALL C), we must append the outer
            # set-op chain to the tail of the inlined inner set-op chain.
            if ir.set_op is not None and ir.set_right is not None:
                tail = result
                while tail.set_right is not None:
                    tail = tail.set_right
                tail.set_op = ir.set_op
                tail.set_right = ir.set_right

            return _inline_derived_tables(result, catalog)

        # Non-set-op case: substitute outer expressions.
        # When the inner FROM is a bare base table, preserve the DT
        # alias so self-joins remain distinguishable after inlining.
        # FIX.16c: Always requalify when the inner FROM is a bare base
        # table (not just when ir.joins exists) — unqualified inner
        # ColumnRefs become ambiguous after inlining if the outer query
        # also references the same base table.
        inlined_from = inner.from_table
        inner_where = inner.where
        if isinstance(inlined_from, RelRef) and inlined_from.alias is None:
            inner_tbl = inlined_from.table.lower()
            inlined_from = RelRef(table=inlined_from.table, alias=alias)
            # FIX.37: Build from_columns set so _requalify_table_refs only
            # requalifies unqualified refs that belong to the FROM table,
            # not refs from joined tables.
            from_cols: frozenset[str] | None = None
            tinfo = catalog.get_table(inlined_from.table) if catalog else None
            if tinfo is not None:
                from_cols = frozenset(c.name.lower() for c in tinfo.columns)
            # FIX.16c: Rewrite col_map values to use the DT alias.
            # Use _requalify_table_refs for both direct and nested ColumnRefs.
            for cname in list(col_map.keys()):
                col_map[cname] = _requalify_table_refs(col_map[cname], inner_tbl, alias, from_cols)
            inner_where = _requalify_table_refs(inner.where, inner_tbl, alias, from_cols)

        new_select = [_substitute_refs(e, alias, col_map) for e in ir.select]

        # Merge WHERE clauses
        new_where = None
        outer_where_sub = _substitute_refs(ir.where, alias, col_map) if ir.where else None
        if inner_where and outer_where_sub:
            new_where = BinOp(op=BinOpKind.AND, left=inner_where, right=outer_where_sub)
        elif inner_where:
            new_where = inner_where
        elif outer_where_sub:
            new_where = outer_where_sub

        # Merge joins: inner joins first, then outer joins (with substituted ON)
        new_joins = list(inner.joins)
        for j in ir.joins:
            new_joins.append(JoinClause(
                join_type=j.join_type,
                right=j.right,
                on=_substitute_refs(j.on, alias, col_map),
            ))

        new_group_by = [_substitute_refs(g, alias, col_map) for g in ir.group_by]
        new_having = _substitute_refs(ir.having, alias, col_map) if ir.having else None
        new_order_by = [
            SortSpec(expr=_substitute_refs(s.expr, alias, col_map), direction=s.direction)
            for s in ir.order_by
        ]

        return QueryIR(
            select=new_select,
            from_table=inlined_from,
            joins=new_joins,
            where=new_where,
            group_by=new_group_by,
            having=new_having,
            order_by=new_order_by,
            limit=ir.limit,
            distinct=ir.distinct or inner.distinct,
            set_op=ir.set_op,
            set_right=ir.set_right,
        )

    # Case 1b: Aggregated inner with set-op, outer is pure passthrough.
    # FIX.21a: When the inner has set_op (UNION ALL etc.) AND aggregation
    # (GROUP BY in one or more branches), but the outer is a pure
    # passthrough (no aggregation/GROUP BY/joins), push the outer projection
    # into each branch.  This avoids the compositional row cap losing rows
    # from UNION ALL branches (e.g., pair 167: nested GROUP BY UNION ALL).
    if (inner.set_op is not None
            and inner.has_aggregation()
            and not ir.has_aggregation()
            and not ir.group_by
            and not ir.joins):

        def _push_into_agg_branch(branch: QueryIR, outer_ir: QueryIR, alias: str) -> QueryIR:
            """Push outer projection + filter into one branch of a set-op with aggregation."""
            branch_col_map = _build_inner_col_map(branch, derived.column_aliases)
            new_select = [_substitute_refs(e, alias, branch_col_map) for e in outer_ir.select]
            new_having = branch.having
            if outer_ir.where:
                outer_w = _substitute_refs(outer_ir.where, alias, branch_col_map)
                if branch.group_by:
                    # Outer WHERE on aggregated branch → HAVING
                    if new_having:
                        new_having = BinOp(op=BinOpKind.AND, left=new_having, right=outer_w)
                    else:
                        new_having = outer_w
                else:
                    # Non-aggregated branch — merge into WHERE
                    if branch.where:
                        branch = QueryIR(
                            select=branch.select, from_table=branch.from_table,
                            joins=list(branch.joins),
                            where=BinOp(op=BinOpKind.AND, left=branch.where, right=outer_w),
                            group_by=list(branch.group_by), having=branch.having,
                            order_by=[], limit=None, distinct=branch.distinct,
                            set_op=branch.set_op, set_right=branch.set_right,
                        )
                    else:
                        branch = QueryIR(
                            select=branch.select, from_table=branch.from_table,
                            joins=list(branch.joins), where=outer_w,
                            group_by=list(branch.group_by), having=branch.having,
                            order_by=[], limit=None, distinct=branch.distinct,
                            set_op=branch.set_op, set_right=branch.set_right,
                        )
            new_set_right = branch.set_right
            if new_set_right is not None:
                new_set_right = _push_into_agg_branch(new_set_right, outer_ir, alias)
            return QueryIR(
                select=new_select,
                from_table=branch.from_table,
                joins=list(branch.joins),
                where=branch.where,
                group_by=list(branch.group_by),
                having=new_having,
                order_by=[],
                limit=None,
                distinct=branch.distinct,
                set_op=branch.set_op,
                set_right=new_set_right,
            )

        left_branch = _push_into_agg_branch(inner, ir, alias)
        right_branch = None
        if inner.set_right:
            right_branch = _push_into_agg_branch(inner.set_right, ir, alias)

        new_order_by = [
            SortSpec(expr=_substitute_refs(s.expr, alias, col_map), direction=s.direction)
            for s in ir.order_by
        ]

        result = QueryIR(
            select=left_branch.select,
            from_table=left_branch.from_table,
            joins=left_branch.joins,
            where=left_branch.where,
            group_by=left_branch.group_by,
            having=left_branch.having,
            order_by=new_order_by,
            limit=ir.limit,
            distinct=ir.distinct or inner.distinct,
            set_op=inner.set_op,
            set_right=right_branch,
        )

        # Preserve outer set_op/set_right chain (same as FIX.18b)
        if ir.set_op is not None and ir.set_right is not None:
            tail = result
            while tail.set_right is not None:
                tail = tail.set_right
            tail.set_op = ir.set_op
            tail.set_right = ir.set_right

        return _inline_derived_tables(result, catalog)

    # Case 2: Aggregated inner (no set-op), outer is just projection/filter
    if inner.has_aggregation() and not ir.has_aggregation() and not ir.group_by and not ir.joins:
        new_select = [_substitute_refs(e, alias, col_map) for e in ir.select]

        # Outer WHERE becomes HAVING (it filters on aggregated results)
        new_having = inner.having
        if ir.where:
            outer_having = _substitute_refs(ir.where, alias, col_map)
            if new_having:
                new_having = BinOp(op=BinOpKind.AND, left=new_having, right=outer_having)
            else:
                new_having = outer_having

        new_order_by = [
            SortSpec(expr=_substitute_refs(s.expr, alias, col_map), direction=s.direction)
            for s in ir.order_by
        ]

        return QueryIR(
            select=new_select,
            from_table=inner.from_table,
            joins=inner.joins,
            where=inner.where,
            group_by=inner.group_by,
            having=new_having,
            order_by=new_order_by,
            limit=ir.limit,
            distinct=ir.distinct,
        )

    # Unsupported: nested aggregation, etc.
    # Return unchanged — falls back to opaque symbolic encoding
    return ir


# ---------------------------------------------------------------------------
# FIX.21b: Aggregate decomposition normalization
# ---------------------------------------------------------------------------

def _normalize_aggregate_decomposition(ir: QueryIR) -> QueryIR:
    """Normalize aggregate decomposition rewrites back to canonical form.

    Detects patterns like:
        SELECT COALESCE(SUM(t1.cnt * t2.cnt), 0)
        FROM (SELECT key, COUNT(*) AS cnt FROM R GROUP BY key) AS t1
        JOIN (SELECT key, COUNT(*) AS cnt FROM S GROUP BY key) AS t2
        ON t1.key = t2.key

    And rewrites to:
        SELECT COUNT(*)
        FROM R AS t1 JOIN S AS t2 ON t1.key = t2.key
        [WHERE lifted_filters]

    This is sound because COUNT(*) over an inner join = SUM of products
    of per-group COUNTs when joining on the GROUP BY keys.
    """
    from ..ir.types import (
        AggCall, AggFunc, BinOp, BinOpKind, ColumnRef, DerivedTable,
        FuncCall, JoinClause, JoinType, Literal, RelRef,
    )

    # Guard: only fire on very specific shapes
    if ir.group_by or ir.having or ir.distinct or ir.set_op:
        return ir
    if len(ir.joins) != 1:
        return ir
    if ir.joins[0].join_type != JoinType.INNER:
        return ir
    if len(ir.select) < 1:
        return ir

    # Match FROM and JOIN as DerivedTable
    if not isinstance(ir.from_table, DerivedTable):
        return ir
    if not isinstance(ir.joins[0].right, DerivedTable):
        return ir

    left_dt = ir.from_table
    right_dt = ir.joins[0].right
    join_on = ir.joins[0].on

    left_match = _match_count_by_key_dt(left_dt)
    right_match = _match_count_by_key_dt(right_dt)
    if left_match is None or right_match is None:
        return ir

    # Check each SELECT expression: must be COALESCE(SUM(cnt_l * cnt_r), 0)
    # or COUNT(*) already (pass-through)
    rewritten_selects: list = []
    for sel_expr in ir.select:
        product_match = _match_coalesced_sum_of_product(sel_expr, left_match, right_match)
        if product_match is not None:
            # Replace with COUNT(*)
            rewritten_selects.append(AggCall(
                func=AggFunc.COUNT, arg=None, distinct=False,
                alias=sel_expr.alias,
            ))
        elif isinstance(sel_expr, ColumnRef):
            # Non-aggregate passthrough column — substitute to base table ref
            rewritten_selects.append(sel_expr)
        else:
            return ir  # Can't rewrite this expression

    # Match join ON: must be equality of projected key columns
    keys_match = _match_eq_projected_keys(join_on, left_match, right_match)
    if not keys_match:
        return ir

    left_key_expr, right_key_expr = keys_match

    # Build rewritten join on base-table key expressions
    new_on = BinOp(op=BinOpKind.EQ, left=left_key_expr, right=right_key_expr)

    # Lift side-local WHERE filters
    new_where = ir.where
    if left_match.where is not None:
        lifted = _requalify_table_refs(
            left_match.where, left_match.source_name, left_match.outer_alias
        )
        new_where = BinOp(op=BinOpKind.AND, left=new_where, right=lifted) if new_where else lifted
    if right_match.where is not None:
        lifted = _requalify_table_refs(
            right_match.where, right_match.source_name, right_match.outer_alias
        )
        new_where = BinOp(op=BinOpKind.AND, left=new_where, right=lifted) if new_where else lifted

    # Build rewritten IR
    new_from = RelRef(table=left_match.source_table, alias=left_match.outer_alias)
    new_right = RelRef(table=right_match.source_table, alias=right_match.outer_alias)

    result = QueryIR(
        select=rewritten_selects,
        from_table=new_from,
        joins=[JoinClause(join_type=JoinType.INNER, right=new_right, on=new_on)],
        where=new_where,
        group_by=[],
        having=None,
        order_by=list(ir.order_by),
        limit=ir.limit,
        distinct=ir.distinct,
    )
    logger.debug("FIX.21b: Normalized aggregate decomposition → COUNT(*) JOIN")
    return result


@dataclass
class _CountByKeyDT:
    """Matched structure of a GROUP BY key, COUNT(*) derived table."""
    outer_alias: str      # alias of the DT in the outer query
    source_table: str     # base table name
    source_name: str      # base table name (or alias) for requalification
    key_proj_name: str    # projected column name of the GROUP BY key
    key_expr: Expr        # the actual GROUP BY key expression
    count_proj_name: str  # projected column name of COUNT(*)
    where: Optional[Expr] # inner WHERE filter (or None)


def _match_count_by_key_dt(dt) -> Optional[_CountByKeyDT]:
    """Match a DerivedTable as SELECT key, COUNT(*) FROM T [WHERE ...] GROUP BY key."""
    from ..ir.types import (
        AggCall, AggFunc, ColumnRef, DerivedTable, RelRef,
    )

    if not isinstance(dt, DerivedTable):
        return None

    inner = dt.query
    alias = dt.alias.lower() if dt.alias else ""

    # Must be: single base table, no joins, no set_op, no HAVING, no DISTINCT
    if not isinstance(inner.from_table, RelRef):
        return None
    if inner.joins or inner.set_op or inner.having or inner.distinct:
        return None

    # Must have GROUP BY
    if not inner.group_by or len(inner.group_by) != 1:
        return None

    # Must have exactly 2 SELECT: key + COUNT(*)
    if len(inner.select) != 2:
        return None

    # Find the key and count columns
    key_expr = None
    key_proj_name = None
    count_proj_name = None

    for sel in inner.select:
        if isinstance(sel, AggCall) and sel.func == AggFunc.COUNT and sel.arg is None and not sel.distinct:
            count_proj_name = (sel.alias or "count").lower()
        elif isinstance(sel, ColumnRef):
            key_expr = sel
            key_proj_name = (sel.alias or sel.column).lower()
        else:
            return None  # Unsupported expression

    if key_expr is None or count_proj_name is None:
        return None

    # GROUP BY key must match the projected key
    gb = inner.group_by[0]
    if isinstance(gb, ColumnRef) and isinstance(key_expr, ColumnRef):
        if gb.column.lower() != key_expr.column.lower():
            return None
    else:
        return None

    source_table = inner.from_table.table
    source_name = (inner.from_table.alias or inner.from_table.table).lower()

    # Requalify key_expr to use the DT outer alias
    requalified_key = ColumnRef(
        table=alias, column=key_expr.column,
    )

    return _CountByKeyDT(
        outer_alias=alias,
        source_table=source_table,
        source_name=source_name,
        key_proj_name=key_proj_name,
        key_expr=requalified_key,
        count_proj_name=count_proj_name,
        where=inner.where,
    )


def _match_coalesced_sum_of_product(
    expr, left: _CountByKeyDT, right: _CountByKeyDT,
) -> Optional[bool]:
    """Match COALESCE(SUM(left.cnt * right.cnt), 0) or just SUM(left.cnt * right.cnt)."""
    from ..ir.types import (
        AggCall, AggFunc, BinOp, BinOpKind, ColumnRef, FuncCall, Literal,
    )

    # Unwrap COALESCE(..., 0)
    sum_expr = expr
    if isinstance(expr, FuncCall) and expr.func_name.upper() == "COALESCE" and len(expr.args) == 2:
        inner_arg = expr.args[0]
        fallback = expr.args[1]
        if isinstance(fallback, Literal) and fallback.value == 0:
            sum_expr = inner_arg
        else:
            return None

    # Match SUM(left.cnt * right.cnt)
    if not isinstance(sum_expr, AggCall):
        return None
    if sum_expr.func != AggFunc.SUM:
        return None
    if sum_expr.distinct:
        return None

    product = sum_expr.arg
    if not isinstance(product, BinOp) or product.op != BinOpKind.MUL:
        return None

    # Match both operands as column refs to count columns (either order)
    def _is_count_ref(e, dt: _CountByKeyDT) -> bool:
        if not isinstance(e, ColumnRef):
            return False
        return (e.table and e.table.lower() == dt.outer_alias
                and e.column.lower() == dt.count_proj_name)

    if (_is_count_ref(product.left, left) and _is_count_ref(product.right, right)):
        return True
    if (_is_count_ref(product.left, right) and _is_count_ref(product.right, left)):
        return True

    return None


def _match_eq_projected_keys(
    on_expr, left: _CountByKeyDT, right: _CountByKeyDT,
) -> Optional[tuple]:
    """Match ON t1.key = t2.key and return base-table key expressions."""
    from ..ir.types import BinOp, BinOpKind, ColumnRef

    if not isinstance(on_expr, BinOp) or on_expr.op != BinOpKind.EQ:
        return None

    def _is_key_ref(e, dt: _CountByKeyDT) -> bool:
        if not isinstance(e, ColumnRef):
            return False
        return (e.table and e.table.lower() == dt.outer_alias
                and e.column.lower() == dt.key_proj_name)

    # Match either order
    if _is_key_ref(on_expr.left, left) and _is_key_ref(on_expr.right, right):
        # Return base-table key expressions requalified to outer aliases
        left_key = ColumnRef(table=left.outer_alias, column=left.key_expr.column)
        right_key = ColumnRef(table=right.outer_alias, column=right.key_expr.column)
        return left_key, right_key
    if _is_key_ref(on_expr.left, right) and _is_key_ref(on_expr.right, left):
        left_key = ColumnRef(table=left.outer_alias, column=left.key_expr.column)
        right_key = ColumnRef(table=right.outer_alias, column=right.key_expr.column)
        return left_key, right_key

    return None


# ---------------------------------------------------------------------------
# FIX.22: Anti-join LEFT JOIN normalization
# ---------------------------------------------------------------------------

def _normalize_antijoin_left_join(ir: QueryIR) -> QueryIR:
    """Normalize LEFT JOIN anti-join patterns to NOT IN subqueries.

    Detects:
        SELECT ... FROM T AS t1
        LEFT JOIN (SELECT key_col, <non-null literal> AS marker
                   FROM R [GROUP BY key_col]) AS dt
          ON t1.x = dt.key_col
        WHERE ... OR dt.marker IS NULL

    And rewrites to:
        SELECT ... FROM T AS t1
        WHERE ... OR t1.x NOT IN (SELECT key_col FROM R)

    The LEFT JOIN is removed and `dt.marker IS NULL` is replaced with
    a NOT IN subquery.  This fixes compositional DT encoding issues
    where the DT gets independent symbolic rows that don't reflect
    the actual base-table contents.

    Guards:
    - Marker must be a non-NULL literal (TRUE, 1, etc.)
    - Marker column must only appear in IS NULL tests in WHERE
    - Marker column must not appear in SELECT, GROUP BY, HAVING, ORDER BY
    - DT inner query must be simple: single base table, no joins, no set_op
    - ON clause must be a simple equality: left_col = dt.key_col
    """
    from ..ir.types import (
        BinOp, BinOpKind, ColumnRef, DerivedTable, InSubquery,
        JoinClause, JoinType, Literal, RelRef, UnaryOp, UnaryOpKind,
    )

    if not ir.joins or not ir.where:
        return ir

    # Find LEFT JOIN DTs matching the anti-join marker pattern
    for join_idx, join in enumerate(ir.joins):
        if join.join_type != JoinType.LEFT:
            continue
        if not isinstance(join.right, DerivedTable):
            continue

        dt = join.right
        inner = dt.query
        dt_alias = (dt.alias or "").lower()

        # Inner must be simple: single base table, no joins, no set_op
        if not isinstance(inner.from_table, RelRef):
            continue
        if inner.joins or inner.set_op:
            continue

        # Find the marker column: a non-NULL literal in the inner SELECT
        marker_name = None
        key_col_names = []
        for sel in inner.select:
            if isinstance(sel, Literal) and sel.value is not None and sel.alias:
                marker_name = sel.alias.lower()
            elif isinstance(sel, ColumnRef):
                key_col_names.append(sel)
            else:
                # Complex expression — not a simple marker DT
                marker_name = None
                break

        if marker_name is None or not key_col_names:
            continue

        # ON clause must be a simple equality: outer_col = dt.key_col
        # (or dt.key_col = outer_col)
        on = join.on
        if not isinstance(on, BinOp) or on.op != BinOpKind.EQ:
            continue

        # Determine which side references the DT
        outer_key = None
        dt_key_col = None
        if (isinstance(on.left, ColumnRef) and isinstance(on.right, ColumnRef)):
            if on.right.table and on.right.table.lower() == dt_alias:
                outer_key = on.left
                dt_key_col = on.right.column.lower()
            elif on.left.table and on.left.table.lower() == dt_alias:
                outer_key = on.right
                dt_key_col = on.left.column.lower()

        if outer_key is None or dt_key_col is None:
            continue

        # Check that the marker column is only used in IS NULL tests in WHERE
        # and not referenced in SELECT, GROUP BY, HAVING, ORDER BY
        marker_ref = (dt_alias, marker_name)

        def _refs_marker(expr, marker_ref) -> bool:
            """Check if expr references the marker column."""
            if expr is None:
                return False
            if isinstance(expr, ColumnRef):
                return (expr.table and expr.table.lower() == marker_ref[0]
                        and expr.column.lower() == marker_ref[1])
            if isinstance(expr, BinOp):
                return _refs_marker(expr.left, marker_ref) or _refs_marker(expr.right, marker_ref)
            if isinstance(expr, UnaryOp):
                return _refs_marker(expr.operand, marker_ref)
            if isinstance(expr, InSubquery):
                return _refs_marker(expr.expr, marker_ref)
            if hasattr(expr, 'arg') and expr.arg is not None:
                return _refs_marker(expr.arg, marker_ref)
            if hasattr(expr, 'args') and isinstance(getattr(expr, 'args', None), list):
                return any(_refs_marker(a, marker_ref) for a in expr.args)
            if hasattr(expr, 'whens'):
                for cw in expr.whens:
                    if _refs_marker(cw.when, marker_ref) or _refs_marker(cw.then, marker_ref):
                        return True
                if hasattr(expr, 'else_') and _refs_marker(expr.else_, marker_ref):
                    return True
            return False

        # Marker must not be in SELECT, GROUP BY, HAVING, ORDER BY
        if any(_refs_marker(s, marker_ref) for s in ir.select):
            continue
        if any(_refs_marker(g, marker_ref) for g in ir.group_by):
            continue
        if _refs_marker(ir.having, marker_ref):
            continue
        if any(_refs_marker(s.expr, marker_ref) for s in ir.order_by):
            continue

        # Check that the marker IS NULL pattern exists in WHERE
        # and extract the replacement
        def _replace_marker_is_null(expr, marker_ref, replacement):
            """Replace `marker IS NULL` with `replacement` in the WHERE tree."""
            if expr is None:
                return None, False
            # Match: UnaryOp(IS_NULL, ColumnRef(dt_alias.marker))
            if (isinstance(expr, UnaryOp) and expr.op == UnaryOpKind.IS_NULL
                    and isinstance(expr.operand, ColumnRef)
                    and expr.operand.table and expr.operand.table.lower() == marker_ref[0]
                    and expr.operand.column.lower() == marker_ref[1]):
                return replacement, True
            if isinstance(expr, BinOp):
                new_left, changed_l = _replace_marker_is_null(expr.left, marker_ref, replacement)
                new_right, changed_r = _replace_marker_is_null(expr.right, marker_ref, replacement)
                if changed_l or changed_r:
                    return BinOp(op=expr.op, left=new_left, right=new_right,
                                 sem_type=expr.sem_type, alias=expr.alias), True
                return expr, False
            if isinstance(expr, UnaryOp):
                new_operand, changed = _replace_marker_is_null(expr.operand, marker_ref, replacement)
                if changed:
                    return UnaryOp(op=expr.op, operand=new_operand,
                                   sem_type=expr.sem_type, alias=expr.alias), True
                return expr, False
            return expr, False

        # Build the NOT IN subquery: outer_key NOT IN (SELECT key_col FROM R [WHERE ...])
        # Find the inner key ColumnRef matching dt_key_col
        inner_key_ref = None
        for kcol in key_col_names:
            if kcol.column.lower() == dt_key_col:
                inner_key_ref = ColumnRef(table=None, column=kcol.column)
                break
        if inner_key_ref is None:
            continue

        subquery_ir = QueryIR(
            select=[inner_key_ref],
            from_table=inner.from_table,
            joins=list(inner.joins),
            where=inner.where,
            group_by=list(inner.group_by) if inner.group_by else [],
            having=inner.having,
            order_by=[],
            limit=None,
            distinct=False,
        )

        # Build NOT IN: UnaryOp(NOT, InSubquery(outer_key, subquery))
        not_in_expr = UnaryOp(
            op=UnaryOpKind.NOT,
            operand=InSubquery(expr=outer_key, query=subquery_ir),
        )

        new_where, replaced = _replace_marker_is_null(ir.where, marker_ref, not_in_expr)
        if not replaced:
            continue

        # Also check no other DT refs remain in WHERE/SELECT/etc.
        # (e.g., dt.deptno referenced outside the ON clause)
        def _refs_dt_alias(expr, alias) -> bool:
            """Check if expr references any column from the DT alias."""
            if expr is None:
                return False
            if isinstance(expr, ColumnRef):
                return expr.table and expr.table.lower() == alias
            if isinstance(expr, BinOp):
                return _refs_dt_alias(expr.left, alias) or _refs_dt_alias(expr.right, alias)
            if isinstance(expr, UnaryOp):
                return _refs_dt_alias(expr.operand, alias)
            if isinstance(expr, InSubquery):
                return _refs_dt_alias(expr.expr, alias)
            if hasattr(expr, 'arg') and expr.arg is not None:
                return _refs_dt_alias(expr.arg, alias)
            if hasattr(expr, 'args') and isinstance(getattr(expr, 'args', None), list):
                return any(_refs_dt_alias(a, alias) for a in expr.args)
            if hasattr(expr, 'whens'):
                for cw in expr.whens:
                    if _refs_dt_alias(cw.when, alias) or _refs_dt_alias(cw.then, alias):
                        return True
                if hasattr(expr, 'else_') and _refs_dt_alias(expr.else_, alias):
                    return True
            return False

        # After replacement, no DT alias refs should remain in WHERE
        if _refs_dt_alias(new_where, dt_alias):
            continue
        # Also check SELECT, GROUP BY, HAVING, ORDER BY
        if any(_refs_dt_alias(s, dt_alias) for s in ir.select):
            continue
        if any(_refs_dt_alias(g, dt_alias) for g in ir.group_by):
            continue
        if _refs_dt_alias(ir.having, dt_alias):
            continue
        if any(_refs_dt_alias(s.expr, dt_alias) for s in ir.order_by):
            continue

        # Remove the LEFT JOIN and apply the new WHERE
        new_joins = [j for k, j in enumerate(ir.joins) if k != join_idx]

        result = QueryIR(
            select=list(ir.select),
            from_table=ir.from_table,
            joins=new_joins,
            where=new_where,
            group_by=list(ir.group_by),
            having=ir.having,
            order_by=list(ir.order_by),
            limit=ir.limit,
            distinct=ir.distinct,
            set_op=ir.set_op,
            set_right=ir.set_right,
        )
        logger.debug("FIX.22: Normalized anti-join LEFT JOIN → NOT IN subquery (alias=%s)", dt_alias)
        return result

    return ir


# ---------------------------------------------------------------------------
# FIX.22b: Aggregate push-down reversal over UNION ALL
# ---------------------------------------------------------------------------

def _normalize_aggregate_pushdown_union(ir: QueryIR) -> QueryIR:
    """Reverse aggregate push-down over UNION ALL branches.

    Detects:
        SELECT [COALESCE(]SUM(agg_col)[, 0)], key_col
        FROM (
          SELECT const1 AS key, AGG(expr) AS agg_col
            FROM T1 [CROSS JOIN VALUES(const1) AS v(key)] GROUP BY [v.key]
          UNION ALL
          SELECT const2 AS key, AGG(expr) AS agg_col
            FROM T2 [CROSS JOIN VALUES(const2) AS v(key)] GROUP BY [v.key]
        ) AS dt
        GROUP BY key_col

    And rewrites to:
        SELECT AGG(expr), key_col
        FROM (
          SELECT const1 AS key, <agg arg columns> FROM T1
          UNION ALL
          SELECT const2 AS key, <agg arg columns> FROM T2
        ) AS dt
        GROUP BY key_col

    This is sound because each key group maps to exactly one UNION ALL
    branch (each key is a unique constant), so SUM(per-branch AGG) = AGG
    over that branch's rows.

    Guards:
    - FROM must be a DerivedTable with UNION ALL set_op
    - All branches must have the same aggregate function on the same expression
    - Each branch must have a unique constant key value
    - Outer GROUP BY must be by the key column
    - Outer SELECT must be COALESCE(SUM(agg_col), 0) or SUM(agg_col)
    """
    from ..ir.types import (
        AggCall, AggFunc, BinOp, BinOpKind, ColumnRef, DerivedTable,
        FuncCall, JoinClause, JoinType, Literal, RelRef,
    )

    # Must have FROM as DerivedTable with inner set_op
    if not isinstance(ir.from_table, DerivedTable):
        return ir
    if ir.joins:
        return ir
    if ir.having or ir.distinct:
        return ir

    dt = ir.from_table
    inner = dt.query
    dt_alias = (dt.alias or "").lower()

    # Inner must have UNION ALL
    if inner.set_op is None:
        return ir

    from ..ir.types import SetOpKind
    if inner.set_op != SetOpKind.UNION_ALL:
        return ir

    # Collect all UNION ALL branches
    branches = []
    branch = inner
    while branch is not None:
        branches.append(branch)
        if branch.set_op == SetOpKind.UNION_ALL and branch.set_right is not None:
            branch = branch.set_right
        else:
            break

    if len(branches) < 2:
        return ir

    # Must have GROUP BY with a single key column
    if len(ir.group_by) != 1:
        return ir
    outer_group_col = ir.group_by[0]
    if not isinstance(outer_group_col, ColumnRef):
        return ir
    group_col_name = outer_group_col.column.lower()

    # Analyze each branch: try constant-key pattern first, then data-key pattern
    branch_infos = []
    for br in branches:
        info = _analyze_agg_pushdown_branch(br, group_col_name)
        if info is None:
            return ir
        branch_infos.append(info)

    # All branches must use the same aggregate function
    first = branch_infos[0]
    for bi in branch_infos[1:]:
        if bi.agg_func != first.agg_func:
            return ir

    # For constant-key branches, check uniqueness
    if all(bi.key_value is not None for bi in branch_infos):
        key_values = [bi.key_value for bi in branch_infos]
        if len(set(str(k) for k in key_values)) != len(key_values):
            return ir
    # For data-key branches, all must be data-key (no mix)
    elif all(bi.key_is_data_col for bi in branch_infos):
        pass  # Data-key branches: partitioned by same GROUP BY key
    else:
        return ir  # Mixed constant/data keys not supported

    # Match outer SELECT: for each expression, check if it's COALESCE(SUM(agg_col), 0)
    # or SUM(agg_col), or a passthrough key column
    new_outer_selects = []
    for sel in ir.select:
        # Try matching COALESCE(SUM(agg_col), 0)
        matched_agg = _match_outer_sum_of_inner_agg(sel, dt_alias, first.agg_proj_name)
        if matched_agg is not None:
            # Replace with the original aggregate over the inner expr
            # The agg arg references need to be to columns in the new inner select
            new_agg = AggCall(
                func=first.agg_func,
                arg=ColumnRef(table=None, column=first.agg_proj_name),
                distinct=False,
                alias=sel.alias if hasattr(sel, 'alias') else None,
            )
            new_outer_selects.append(new_agg)
        elif isinstance(sel, ColumnRef):
            new_outer_selects.append(sel)
        else:
            return ir  # Unknown select expression

    # Build new inner branches: strip the aggregate, keep the key
    # and project the aggregate's argument expression
    new_branches = []
    for bi in branch_infos:
        new_branch_select = []
        # Add key: constant or data column
        if bi.key_is_data_col and bi.key_expr is not None:
            new_branch_select.append(bi.key_expr)
        else:
            new_branch_select.append(Literal(value=bi.key_value, alias=bi.key_proj_name))
        # Add the aggregate arg expression, projected with the agg_proj_name
        if bi.agg_arg is not None:
            agg_arg = bi.agg_arg
            if not hasattr(agg_arg, 'alias') or agg_arg.alias != bi.agg_proj_name:
                # Clone with the right alias
                if isinstance(agg_arg, ColumnRef):
                    agg_arg = ColumnRef(table=agg_arg.table, column=agg_arg.column, alias=bi.agg_proj_name)
                elif isinstance(agg_arg, CaseExpr):
                    agg_arg = CaseExpr(whens=agg_arg.whens, else_=agg_arg.else_,
                                       sem_type=agg_arg.sem_type, alias=bi.agg_proj_name)
                elif isinstance(agg_arg, Literal):
                    agg_arg = Literal(value=agg_arg.value, alias=bi.agg_proj_name)
                else:
                    return ir  # Can't handle this arg type
            new_branch_select.append(agg_arg)
        else:
            # COUNT(*) — no arg, use a dummy column that's always non-null
            # Actually, we need to handle this: COUNT(*) counts rows.
            # We can use Literal(1) as a proxy — COUNT(1) = COUNT(*).
            new_branch_select.append(Literal(value=1, alias=bi.agg_proj_name))

        new_branch = QueryIR(
            select=new_branch_select,
            from_table=bi.base_from,
            joins=list(bi.base_joins),
            where=bi.base_where,
            group_by=[],
            having=None,
            order_by=[],
            limit=None,
            distinct=False,
            set_op=None,
            set_right=None,
        )
        new_branches.append(new_branch)

    # Chain branches with UNION ALL
    for i in range(len(new_branches) - 1):
        new_branches[i] = QueryIR(
            select=new_branches[i].select,
            from_table=new_branches[i].from_table,
            joins=list(new_branches[i].joins),
            where=new_branches[i].where,
            group_by=[],
            having=None,
            order_by=[],
            limit=None,
            distinct=False,
            set_op=SetOpKind.UNION_ALL,
            set_right=new_branches[i + 1],
        )

    new_inner = new_branches[0]

    new_dt = DerivedTable(
        query=new_inner,
        alias=dt.alias,
        column_aliases=dt.column_aliases,
    )

    result = QueryIR(
        select=new_outer_selects,
        from_table=new_dt,
        joins=[],
        where=ir.where,
        group_by=list(ir.group_by),
        having=ir.having,
        order_by=list(ir.order_by),
        limit=ir.limit,
        distinct=ir.distinct,
        set_op=ir.set_op,
        set_right=ir.set_right,
    )
    logger.debug("FIX.22b: Normalized aggregate push-down over UNION ALL → single aggregate")
    return result


@dataclass
class _AggPushdownBranch:
    """Matched structure of one UNION ALL branch in agg push-down."""
    key_value: object        # constant key value (e.g., 1, 2) or None for data col
    key_proj_name: str       # projected name of the key column
    key_is_data_col: bool    # True if key is a data column (not a constant)
    key_expr: Optional[Expr] # key expression (for data-col keys)
    agg_func: AggFunc        # aggregate function (COUNT, SUM, etc.)
    agg_arg: Optional[Expr]  # aggregate argument expression (or None for COUNT(*))
    agg_proj_name: str       # projected name of the aggregate column
    base_from: object        # base table RelRef
    base_joins: list         # base joins (after removing VALUES cross-join)
    base_where: Optional[Expr]  # base WHERE


def _analyze_agg_pushdown_branch(branch: QueryIR, group_col_name: str) -> Optional[_AggPushdownBranch]:
    """Analyze one UNION ALL branch for the aggregate push-down pattern.

    Expected shape:
        SELECT [v.key | const] AS key_col, AGG(expr) AS agg_col
        FROM base_table [CROSS JOIN (VALUES(k)) AS v(key_col)]
        [WHERE ...]
        GROUP BY [v.key]
    """
    from ..ir.types import (
        AggCall, BinOp, BinOpKind, ColumnRef, DerivedTable,
        JoinClause, JoinType, Literal, RelRef,
    )

    if not branch.select or len(branch.select) != 2:
        return None

    # Find key column and agg column
    key_value = None
    key_proj_name = None
    agg_call = None
    agg_proj_name = None

    key_is_data_col = False
    key_expr = None

    for sel in branch.select:
        if isinstance(sel, AggCall):
            agg_call = sel
            agg_proj_name = (sel.alias or "").lower()
        elif isinstance(sel, Literal) and sel.alias:
            key_value = sel.value
            key_proj_name = sel.alias.lower()
        elif isinstance(sel, ColumnRef):
            # Try resolving from VALUES cross-join first
            resolved_val = _resolve_values_column(branch, sel)
            if resolved_val is not None:
                key_value = resolved_val
                key_proj_name = (sel.alias or sel.column).lower()
            else:
                # Data column key: the branch GROUPs BY this column
                col_name = (sel.alias or sel.column).lower()
                if col_name == group_col_name:
                    key_proj_name = col_name
                    key_is_data_col = True
                    key_expr = sel
                else:
                    return None
        else:
            return None

    if agg_call is None or agg_proj_name is None:
        return None
    if key_value is None and not key_is_data_col:
        return None

    # Key proj name must match the outer GROUP BY column
    if key_proj_name != group_col_name:
        return None

    # For data-column keys, the branch must GROUP BY the same column
    if key_is_data_col:
        if not branch.group_by:
            return None
        has_matching_gb = any(
            isinstance(g, ColumnRef) and g.column.lower() == group_col_name
            for g in branch.group_by
        )
        if not has_matching_gb:
            return None

    # Determine base FROM and joins (strip VALUES cross-join)
    base_from = branch.from_table
    base_joins = []
    for j in branch.joins:
        if _is_values_singleton_cross_join(j):
            continue  # Skip VALUES cross-join
        base_joins.append(j)

    return _AggPushdownBranch(
        key_value=key_value,
        key_proj_name=key_proj_name,
        key_is_data_col=key_is_data_col,
        key_expr=key_expr,
        agg_func=agg_call.func,
        agg_arg=agg_call.arg,
        agg_proj_name=agg_proj_name,
        base_from=base_from,
        base_joins=base_joins,
        base_where=branch.where,
    )


def _resolve_values_column(branch: QueryIR, col_ref: ColumnRef) -> Optional[object]:
    """Resolve a column reference to a constant from a VALUES cross-join."""
    from ..ir.types import DerivedTable, JoinClause, JoinType, Literal, RelRef

    if not col_ref.table:
        return None

    for j in branch.joins:
        if not _is_values_singleton_cross_join(j):
            continue
        if not isinstance(j.right, DerivedTable):
            continue
        dt = j.right
        dt_alias = (dt.alias or "").lower()
        if col_ref.table.lower() != dt_alias:
            continue
        # Found the VALUES DT — get the constant value
        inner = dt.query
        col_aliases = dt.column_aliases or []
        for i, sel in enumerate(inner.select):
            if isinstance(sel, Literal):
                # Check if this is the right column
                proj_name = (sel.alias or "").lower()
                if i < len(col_aliases):
                    proj_name = col_aliases[i].lower()
                if proj_name == col_ref.column.lower():
                    return sel.value
    return None


def _is_values_singleton_cross_join(join) -> bool:
    """Check if a join is a CROSS JOIN with a VALUES singleton."""
    from ..ir.types import DerivedTable, JoinType, Literal, RelRef

    if join.join_type not in (JoinType.CROSS, JoinType.INNER):
        return False
    if not isinstance(join.right, DerivedTable):
        return False
    inner = join.right.query
    if not isinstance(inner.from_table, RelRef):
        return False
    if inner.from_table.table.lower() != "__values_dual__":
        return False
    # Must have exactly one SELECT with a literal
    if len(inner.select) != 1 or not isinstance(inner.select[0], Literal):
        return False
    return True


def _match_outer_sum_of_inner_agg(
    expr, dt_alias: str, agg_proj_name: str,
) -> Optional[bool]:
    """Match COALESCE(SUM(agg_col), 0) or SUM(agg_col)."""
    from ..ir.types import AggCall, AggFunc, ColumnRef, FuncCall, Literal

    # Try COALESCE(SUM(...), 0) first
    sum_expr = expr
    if isinstance(expr, FuncCall) and expr.func_name.upper() == "COALESCE" and len(expr.args) == 2:
        fallback = expr.args[1]
        if isinstance(fallback, Literal) and fallback.value == 0:
            sum_expr = expr.args[0]
        else:
            return None

    # Match SUM(agg_col)
    if not isinstance(sum_expr, AggCall):
        return None
    if sum_expr.func != AggFunc.SUM:
        return None
    if not isinstance(sum_expr.arg, ColumnRef):
        return None
    if sum_expr.arg.column.lower() != agg_proj_name:
        return None

    return True


# ---------------------------------------------------------------------------
# Compositional derived-table encoding
# ---------------------------------------------------------------------------

def _collect_remaining_derived_tables(ir: QueryIR) -> list:
    """Return DerivedTable nodes that remain after inlining attempts."""
    from ..ir.types import DerivedTable

    result = []
    if isinstance(ir.from_table, DerivedTable):
        result.append(ir.from_table)
    for join in ir.joins:
        if isinstance(join.right, DerivedTable):
            result.append(join.right)
    return result


def _get_projected_col_names(
    derived,
) -> list[str]:
    """Get projected column names for a DerivedTable.

    Uses column_aliases if set, otherwise derives names from the SELECT list.
    """
    inner = derived.query
    names: list[str] = []
    for idx, expr in enumerate(inner.select):
        # Positional column_aliases take priority
        if derived.column_aliases and idx < len(derived.column_aliases):
            names.append(derived.column_aliases[idx].lower())
        elif expr.alias:
            names.append(expr.alias.lower())
        elif isinstance(expr, ColumnRef):
            names.append(expr.column.lower())
        elif isinstance(expr, AggCall):
            arg_name = ""
            if expr.arg and isinstance(expr.arg, ColumnRef):
                arg_name = expr.arg.column
            prefix = "distinct_" if expr.distinct else ""
            name = f"{expr.func.value.lower()}_{prefix}{arg_name}".rstrip("_")
            names.append(name.lower())
        else:
            names.append(f"expr_{idx}")
    return names


def _encode_derived_table_rows(
    derived,
    db: SymbolicDB,
    catalog: Catalog,
    scope: BoundedScope,
) -> Optional[SymbolicTable]:
    """Encode a non-inlined derived table as a symbolic table.

    Recursively encodes the inner query, then converts its result rows
    into a SymbolicTable that the outer query can reference.  This
    produces *constrained* symbolic rows (bound to the inner query's
    evaluation) instead of free variables.
    """
    inner = derived.query
    alias = derived.alias.lower()

    # 1. Get projected column names
    col_names = _get_projected_col_names(derived)
    if not col_names:
        return None

    # FIX.20a: Recursively encode nested DTs inside the inner query
    # before evaluating it.  Without this, nested DTs (e.g., t3 and t4
    # inside t6) remain as free unconstrained symbolic rows, producing
    # garbage values in the compositional encoding.
    for nested_dt in _collect_remaining_derived_tables(inner):
        nested_alias = nested_dt.alias.lower()
        if nested_alias in db.tables:
            nested_encoded = _encode_derived_table_rows(nested_dt, db, catalog, scope)
            if nested_encoded is not None:
                db.tables[nested_alias] = nested_encoded
                logger.debug("Compositional encoding: replaced nested %s (inside %s)", nested_alias, alias)

    # 2. Encode the inner query to get result rows
    try:
        inner_results = _encode_query_with_setops(inner, db, scope)
    except Exception as exc:
        logger.debug("Compositional derived-table encoding failed for %s: %s", alias, exc)
        return None

    if not inner_results:
        return None

    # 3. Apply DISTINCT deduplication if the inner query is DISTINCT
    if inner.distinct:
        deduped: list[ResultRow] = []
        for i, row in enumerate(inner_results):
            if i == 0:
                deduped.append(row)
                continue
            earlier_match = z3.Or([
                z3.And(deduped[j].survives, _rows_equal(deduped[j].values, row.values))
                for j in range(len(deduped))
            ])
            new_survives = z3.And(row.survives, z3.Not(earlier_match))
            deduped.append(ResultRow(survives=new_survives, values=row.values))
        inner_results = deduped

    # 4. Convert result rows into SymbolicRows.
    #    For each result row, create a SymbolicRow whose column values
    #    are conditionally bound: if the row survives, use the result
    #    values; if not, all columns are NULL.
    sym_rows: list[SymbolicRow] = []
    col_types: dict[str, SemType] = {}

    # Infer column types from the inner query's SELECT list
    for idx, expr in enumerate(inner.select):
        if idx < len(col_names):
            cname = col_names[idx]
            sem = expr.sem_type if expr.sem_type != SemType.UNKNOWN else SemType.INT
            col_types[cname] = sem

    # Cap result rows.  For set_op queries (UNION ALL, etc.), the inner
    # may produce more rows than k_rows.  Allow up to 2*k_rows to
    # preserve more fidelity through compositional encoding (FIX.13c).
    # FIX.20a: For aggregated/GROUP BY inner queries, keep all result rows
    # (up to a safety limit).  The GROUP BY representative logic ensures
    # at most one survivor per group, but truncating before the right
    # group rep causes surviving combos to be lost (e.g., when combo 2
    # is the first surviving group rep but cap=k_rows=2 drops it).
    is_agg_inner = inner.has_aggregation() or bool(inner.group_by)
    if inner.set_op is not None:
        cap = scope.k_rows * 2
    elif is_agg_inner:
        # Keep all aggregated result rows but limit to avoid explosion.
        # With n combos and k groups, we need at least n result rows to
        # capture all group representatives, but cap to avoid O(n²)
        # growth at higher k values.
        cap = min(len(inner_results), scope.k_rows * 2)
    else:
        cap = scope.k_rows
    capped_results = inner_results[:cap]

    for i, result_row in enumerate(capped_results):
        cols: dict[str, NullableVal] = {}
        for j, cname in enumerate(col_names):
            if j < len(result_row.values):
                rv = result_row.values[j]
                # Use the inner row's computed values directly.
                # Row absence is tracked via the ``present`` flag
                # instead of replacing columns with NULL.
                cols[cname] = NullableVal(
                    is_null=rv.is_null,
                    val=rv.val,
                )
            else:
                cols[cname] = NullableVal(
                    is_null=z3.BoolVal(True), val=z3.RealVal(0),
                )
        # present = inner row survives filtering
        sym_rows.append(SymbolicRow(cols=cols, present=result_row.survives))

    # If fewer result rows than cap, pad with absent rows
    while len(sym_rows) < cap:
        null_cols: dict[str, NullableVal] = {}
        for cname in col_names:
            null_cols[cname] = NullableVal(
                is_null=z3.BoolVal(True), val=z3.RealVal(0),
            )
        sym_rows.append(SymbolicRow(cols=null_cols, present=z3.BoolVal(False)))

    return SymbolicTable(name=alias, rows=sym_rows, col_types=col_types)


# ---------------------------------------------------------------------------
# String literal collection (for domain widening)
# ---------------------------------------------------------------------------

def _collect_string_literals(ir: QueryIR) -> set[str]:
    """Collect all string literal values from the IR.

    These values are used to build a deterministic, lex-order-preserving
    symbol table for string encoding.
    """
    strings: set[str] = set()

    def _walk(expr):
        if expr is None:
            return
        if isinstance(expr, Literal) and isinstance(expr.value, str):
            strings.add(expr.value)
        elif isinstance(expr, BinOp):
            _walk(expr.left)
            _walk(expr.right)
        elif isinstance(expr, UnaryOp):
            _walk(expr.operand)
        elif isinstance(expr, AggCall):
            _walk(expr.arg)
        elif isinstance(expr, FuncCall):
            for a in expr.args:
                _walk(a)
        elif isinstance(expr, CaseExpr):
            for cw in expr.whens:
                _walk(cw.when)
                _walk(cw.then)
            _walk(expr.else_)
        elif isinstance(expr, InList):
            _walk(expr.expr)
            for v in expr.values:
                _walk(v)
        elif isinstance(expr, Between):
            _walk(expr.expr)
            _walk(expr.low)
            _walk(expr.high)
        elif isinstance(expr, ScalarSubquery):
            strings.update(_collect_string_literals(expr.query))
        elif isinstance(expr, InSubquery):
            _walk(expr.expr)
            strings.update(_collect_string_literals(expr.query))
        elif isinstance(expr, ExistsSubquery):
            strings.update(_collect_string_literals(expr.query))
        elif isinstance(expr, WindowFunc):
            for a in expr.args:
                _walk(a)
            for p in expr.partition_by:
                _walk(p)
            for o in expr.order_by:
                _walk(o.expr)

    for s in ir.select:
        _walk(s)
    _walk(ir.where)
    for j in ir.joins:
        _walk(j.on)
    for g in ir.group_by:
        _walk(g)
    _walk(ir.having)
    for o in ir.order_by:
        _walk(o.expr)
    if ir.set_right:
        strings.update(_collect_string_literals(ir.set_right))
    # Recurse into derived table inner queries
    for dt in _collect_remaining_derived_tables(ir):
        strings.update(_collect_string_literals(dt.query))
    return strings


def _collect_constraint_string_literals(constraints: list[dict]) -> set[str]:
    """Collect string literals from VeriEQL value constraints.

    FIX.35: IN constraints on ENUM columns use ``{"literal": "..."}``
    operands.  These must be in the symbol table so that
    ``_resolve_operand`` can map them to Z3 integer indices.
    """
    literals: set[str] = set()
    for constraint in constraints:
        for key in ("in", "between", "gt", "gte", "lt", "lte", "neq"):
            args = constraint.get(key)
            if not isinstance(args, list):
                continue
            for arg in args:
                if isinstance(arg, dict) and "literal" in arg:
                    literals.add(str(arg["literal"]))
                elif isinstance(arg, list):
                    for item in arg:
                        if isinstance(item, dict) and "literal" in item:
                            literals.add(str(item["literal"]))
    return literals


def _build_string_symbol_table(
    q1: QueryIR,
    q2: QueryIR,
    value_constraints: list[dict] | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Build a deterministic, lex-order-preserving string symbol table.

    Collects all string literals from both IRs, adds fresh sentinel strings
    for counterexample flexibility, sorts lexicographically, and returns
    (symbols, sym2idx) where integer rank preserves lex order.
    """
    literals = _collect_string_literals(q1) | _collect_string_literals(q2)
    # FIX.35: Include string literals from value constraints (ENUM IN constraints)
    if value_constraints:
        literals |= _collect_constraint_string_literals(value_constraints)
    # Add fresh strings for solver flexibility (values that don't match any literal)
    fresh = {"\x01__fresh_lo__", "\x7f__fresh_hi__"}
    all_strings = sorted(literals | fresh)
    sym2idx = {s: i for i, s in enumerate(all_strings)}
    return all_strings, sym2idx


# Module-level symbol table for the current synthesis run (single-threaded)
_current_sym2idx: dict[str, int] = {}

# Module-level state for the current synthesis run, used by _encode_inner_query
# to access the symbolic DB, scope, and catalog from within _eval_value/_eval_predicate_3vl.
_current_db: Optional[SymbolicDB] = None
_current_scope: Optional[BoundedScope] = None
_current_catalog: Optional[Catalog] = None

# Pre-computed window function values, keyed by (id(expr), id(binding)).
# Populated by _precompute_window_values before _eval_value runs.
_precomputed_windows: dict[tuple[int, int], NullableVal] = {}

# FIX.36a: Known SQL function names that are handled by specific blocks
# in _eval_value. Any function NOT in this set is treated as an abstract
# uninterpreted function so that structurally identical calls (e.g. B(X))
# in Q1 and Q2 share the same Z3 symbol.
_KNOWN_SQL_FUNCS: frozenset[str] = frozenset({
    "COALESCE", "NULLIF", "IIF", "IF", "IFNULL", "ISNULL",
    "ABS", "ROUND", "CAST", "GREATEST", "LEAST",
    "DATEDIFF", "DATE_ADD", "ADDDATE", "DATE_SUB", "SUBDATE",
    "TIMESTAMPDIFF", "LENGTH", "FLOOR", "CEIL", "CEILING",
    "TRUNCATE", "UPPER", "LOWER",
})

# FIX.36a: Cache of Z3 uninterpreted function declarations, keyed by
# (function_name, arity). Shared across Q1 and Q2 within one synthesis run
# so that identical calls get the same Z3 function symbol.
_uninterp_funcs: dict[tuple[str, int], z3.FuncDeclRef] = {}


def _collect_int_literal_values(ir: QueryIR) -> set[int]:
    """Collect all integer literal values from the IR.

    These must be representable in the integer domain for numeric
    predicates to be satisfiable.  Bug #21: without this, predicates
    like A11 > 8000 are always false when int_bounds = (-10, 10),
    causing vacuous UNSAT (same class as string-domain bug #19).
    """
    values: set[int] = set()

    def _walk(expr):
        if expr is None:
            return
        if isinstance(expr, Literal) and isinstance(expr.value, (int, float)):
            values.add(int(expr.value))
        elif isinstance(expr, BinOp):
            _walk(expr.left)
            _walk(expr.right)
        elif isinstance(expr, UnaryOp):
            _walk(expr.operand)
        elif isinstance(expr, AggCall):
            _walk(expr.arg)
        elif isinstance(expr, FuncCall):
            for a in expr.args:
                _walk(a)
        elif isinstance(expr, CaseExpr):
            for cw in expr.whens:
                _walk(cw.when)
                _walk(cw.then)
            _walk(expr.else_)
        elif isinstance(expr, InList):
            _walk(expr.expr)
            for v in expr.values:
                _walk(v)
        elif isinstance(expr, Between):
            _walk(expr.expr)
            _walk(expr.low)
            _walk(expr.high)
        elif isinstance(expr, ScalarSubquery):
            values.update(_collect_int_literal_values(expr.query))
        elif isinstance(expr, InSubquery):
            _walk(expr.expr)
            values.update(_collect_int_literal_values(expr.query))
        elif isinstance(expr, ExistsSubquery):
            values.update(_collect_int_literal_values(expr.query))
        elif isinstance(expr, WindowFunc):
            for a in expr.args:
                _walk(a)
            for p in expr.partition_by:
                _walk(p)
            for o in expr.order_by:
                _walk(o.expr)

    for s in ir.select:
        _walk(s)
    _walk(ir.where)
    for j in ir.joins:
        _walk(j.on)
    for g in ir.group_by:
        _walk(g)
    _walk(ir.having)
    for o in ir.order_by:
        _walk(o.expr)
    if ir.set_right:
        values.update(_collect_int_literal_values(ir.set_right))
    # Recurse into derived table inner queries
    for dt in _collect_remaining_derived_tables(ir):
        values.update(_collect_int_literal_values(dt.query))
    return values


# ---------------------------------------------------------------------------
# Symbolic DB creation
# ---------------------------------------------------------------------------

def _create_symbolic_db(
    table_names: list[str],
    catalog: Catalog,
    scope: BoundedScope,
) -> tuple[SymbolicDB, list[z3.ExprRef]]:
    """Create symbolic tables and return domain constraints."""
    tables: dict[str, SymbolicTable] = {}
    constraints: list[z3.ExprRef] = []

    for tname in table_names:
        # FIX.5+16b: __values_dual__ sentinel table for VALUES lowering.
        # Must have exactly 1 row (unit table) so each VALUES tuple maps
        # to exactly 1 output row.  Using k_rows would multiply each
        # constant SELECT by k, producing spurious duplicates.
        if tname.lower() == "__values_dual__":
            prefix = "__vd_0"
            dummy_val = z3.Real(f"{prefix}_v")
            dummy_null = z3.Bool(f"{prefix}_n")
            constraints.append(z3.Not(dummy_null))
            dummy_rows = [SymbolicRow(cols={"_dummy": NullableVal(is_null=dummy_null, val=dummy_val)})]
            tables["__values_dual__"] = SymbolicTable(
                name="__values_dual__", rows=dummy_rows, col_types={"_dummy": SemType.INT},
            )
            continue

        tinfo = catalog.get_table(tname)
        if tinfo is None:
            continue

        rows: list[SymbolicRow] = []
        col_types: dict[str, SemType] = {}

        for row_idx in range(scope.k_rows):
            cols: dict[str, NullableVal] = {}
            for cinfo in tinfo.columns:
                prefix = f"{tname}_{cinfo.name}_{row_idx}"
                if cinfo.sem_type.is_numeric() or cinfo.sem_type == SemType.UNKNOWN:
                    val = z3.Real(f"{prefix}_v")
                else:
                    val = z3.Int(f"{prefix}_v")
                is_null = z3.Bool(f"{prefix}_n")

                # Domain constraints
                lo, hi = scope.int_bounds
                if cinfo.sem_type.is_numeric():
                    constraints.append(z3.Implies(z3.Not(is_null), z3.And(val >= lo, val <= hi)))
                    # FIX.27: INT columns must stay integral under RealSort encoding.
                    # Without this, the solver can pick fractional values (e.g., -0.5)
                    # for integer columns, producing invalid witness databases.
                    if cinfo.sem_type == SemType.INT:
                        constraints.append(z3.Implies(z3.Not(is_null), z3.IsInt(val)))
                elif cinfo.sem_type == SemType.DATE:
                    n_syms = getattr(scope, '_n_string_symbols', 0)
                    if n_syms > 0:
                        constraints.append(z3.Implies(
                            z3.Not(is_null),
                            z3.And(val >= 0, val < n_syms),
                        ))
                    elif scope.date_values:
                        constraints.append(z3.Implies(
                            z3.Not(is_null),
                            z3.Or([val == d for d in scope.date_values]),
                        ))
                elif cinfo.sem_type == SemType.STRING:
                    n_syms = getattr(scope, '_n_string_symbols', len(scope.string_symbols))
                    if n_syms > 0:
                        constraints.append(z3.Implies(
                            z3.Not(is_null),
                            z3.And(val >= 0, val < n_syms),
                        ))
                elif cinfo.sem_type == SemType.BOOL:
                    constraints.append(z3.Implies(
                        z3.Not(is_null),
                        z3.Or(val == 0, val == 1),
                    ))
                else:
                    constraints.append(z3.Implies(z3.Not(is_null), z3.And(val >= lo, val <= hi)))
                    # UNKNOWN type columns use Real; constrain to integers for valid witnesses
                    if cinfo.sem_type == SemType.UNKNOWN:
                        constraints.append(z3.Implies(z3.Not(is_null), z3.IsInt(val)))

                # Non-nullable columns
                if not cinfo.nullable:
                    constraints.append(z3.Not(is_null))

                cols[cinfo.name.lower()] = NullableVal(is_null=is_null, val=val)
                col_types[cinfo.name.lower()] = cinfo.sem_type

            rows.append(SymbolicRow(cols=cols))

        tables[tname.lower()] = SymbolicTable(name=tname, rows=rows, col_types=col_types)

    # PK uniqueness constraints: pairwise inequality across rows
    # VeriEQL encodes this (Thm 4.8); without it, solver can produce
    # infeasible witnesses with duplicate PKs → false SAT.
    for tname_lower, sym_table in tables.items():
        tinfo = catalog.get_table(tname_lower)
        if tinfo is None:
            continue
        if tinfo.primary_key_groups:
            # Composite PK groups: for each group, enforce tuple inequality
            for pk_group in tinfo.primary_key_groups:
                pk_cols_lower = [c.lower() for c in pk_group]
                for i in range(len(sym_table.rows)):
                    for j in range(i + 1, len(sym_table.rows)):
                        # Collect nullable vals for all group columns
                        group_vals = []
                        for pk_col_lower in pk_cols_lower:
                            vi = sym_table.rows[i].cols.get(pk_col_lower)
                            vj = sym_table.rows[j].cols.get(pk_col_lower)
                            if vi is None or vj is None:
                                continue
                            group_vals.append((vi, vj))
                        if not group_vals:
                            continue
                        # All columns non-null
                        all_non_null = z3.And(*[
                            z3.And(z3.Not(vi.is_null), z3.Not(vj.is_null))
                            for vi, vj in group_vals
                        ])
                        # At least one column value must differ (tuple inequality)
                        at_least_one_diff = z3.Or(*[
                            vi.val != vj.val
                            for vi, vj in group_vals
                        ])
                        constraints.append(z3.Implies(all_non_null, at_least_one_diff))
        else:
            # Fallback: per-column PK uniqueness for backward compat
            for pk_col_name in tinfo.primary_keys:
                pk_col_lower = pk_col_name.lower()
                for i in range(len(sym_table.rows)):
                    row_i_val = sym_table.rows[i].cols.get(pk_col_lower)
                    if row_i_val is None:
                        continue
                    for j in range(i + 1, len(sym_table.rows)):
                        row_j_val = sym_table.rows[j].cols.get(pk_col_lower)
                        if row_j_val is None:
                            continue
                        # If both non-null, values must differ
                        constraints.append(z3.Implies(
                            z3.And(z3.Not(row_i_val.is_null), z3.Not(row_j_val.is_null)),
                            row_i_val.val != row_j_val.val,
                        ))

    # UNIQUE column constraints: same pairwise inequality as PK
    for tname_lower, sym_table in tables.items():
        tinfo = catalog.get_table(tname_lower)
        if tinfo is None:
            continue
        for uq_col_name in tinfo.unique_columns:
            uq_col_lower = uq_col_name.lower()
            for i in range(len(sym_table.rows)):
                row_i_val = sym_table.rows[i].cols.get(uq_col_lower)
                if row_i_val is None:
                    continue
                for j in range(i + 1, len(sym_table.rows)):
                    row_j_val = sym_table.rows[j].cols.get(uq_col_lower)
                    if row_j_val is None:
                        continue
                    constraints.append(z3.Implies(
                        z3.And(z3.Not(row_i_val.is_null), z3.Not(row_j_val.is_null)),
                        row_i_val.val != row_j_val.val,
                    ))

    # FK constraints: enforce child→parent referential integrity.
    # For every FK row, if the FK column is not null, its value must
    # match at least one row in the parent table's referenced column.
    for fk in catalog.foreign_keys:
        src_t = fk.src_table.lower()
        dst_t = fk.dst_table.lower()
        src_c = fk.src_column.lower()
        dst_c = fk.dst_column.lower()
        if src_t in tables and dst_t in tables:
            for src_row in tables[src_t].rows:
                src_val = src_row.cols.get(src_c)
                if src_val is None:
                    continue
                # If not null, must match some dst row
                dst_options = []
                for dst_row in tables[dst_t].rows:
                    dst_val = dst_row.cols.get(dst_c)
                    if dst_val is not None:
                        dst_options.append(z3.And(
                            z3.Not(dst_val.is_null),
                            src_val.val == dst_val.val,
                        ))
                if dst_options:
                    constraints.append(z3.Implies(
                        z3.Not(src_val.is_null),
                        z3.Or(dst_options),
                    ))

    # Value constraints from VeriEQL benchmark data
    if catalog.value_constraints:
        _apply_value_constraints(catalog.value_constraints, tables, constraints)

    return SymbolicDB(tables=tables), constraints


# ---------------------------------------------------------------------------
# Value-constraint helpers (VeriEQL gt/gte/lt/lte/in/between/neq/inc/consec)
# ---------------------------------------------------------------------------

def _resolve_operand(
    operand: object,
    tables: dict[str, SymbolicTable],
    row_idx: int,
) -> tuple[z3.ExprRef | None, z3.ExprRef | None]:
    """Resolve a constraint operand to ``(is_null, val)`` for *row_idx*."""
    if isinstance(operand, (int, float)):
        return None, z3.RealVal(operand)
    if isinstance(operand, dict):
        if "value" in operand:
            tbl, col = operand["value"].split("__", 1)
            sym_table = tables.get(tbl.lower())
            if sym_table and row_idx < len(sym_table.rows):
                nv = sym_table.rows[row_idx].cols.get(col.lower())
                if nv:
                    return nv.is_null, nv.val
        if "date" in operand:
            return None, None  # skip date constraints for now
        if "literal" in operand:
            # FIX.35: Map string literal to its symbol-table index.
            # String columns use z3.Int() values that index into the
            # symbol table; constraint literals must use the same encoding.
            lit_str = str(operand["literal"])
            idx = _current_sym2idx.get(lit_str)
            if idx is not None:
                return None, z3.IntVal(idx)
            return None, None
    return None, None


def _table_for_ref(ref: dict, tables: dict[str, SymbolicTable]) -> SymbolicTable | None:
    """Return the SymbolicTable referenced by a ``{"value": "TBL__COL"}`` dict."""
    if isinstance(ref, dict) and "value" in ref:
        tbl, _ = ref["value"].split("__", 1)
        return tables.get(tbl.lower())
    return None


_CMP_OPS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}


def _apply_value_constraints(
    constraints: list[dict],
    tables: dict[str, SymbolicTable],
    z3_constraints: list[z3.ExprRef],
) -> None:
    """Translate VeriEQL value constraints into Z3 axioms."""
    for constraint in constraints:
        # --- comparison: gt, gte, lt, lte ----------------------------------
        for op_name, op_fn in _CMP_OPS.items():
            if op_name in constraint:
                args = constraint[op_name]
                if len(args) < 2:
                    break
                lhs_ref, rhs_ref = args[0], args[1]
                sym_tbl = _table_for_ref(lhs_ref, tables)
                if sym_tbl is None:
                    break
                for i in range(len(sym_tbl.rows)):
                    lhs_null, lhs_val = _resolve_operand(lhs_ref, tables, i)
                    rhs_null, rhs_val = _resolve_operand(rhs_ref, tables, i)
                    if lhs_val is None or rhs_val is None:
                        continue
                    # VeriEQL semantics: value constraints (gte, lte, etc.)
                    # unconditionally imply NOT NULL for the constrained columns.
                    # E.g., gte >= 0 means And(value >= 0, Not(NULL)).
                    not_null_parts = []
                    if lhs_null is not None:
                        not_null_parts.append(z3.Not(lhs_null))
                    if rhs_null is not None:
                        not_null_parts.append(z3.Not(rhs_null))
                    body = op_fn(lhs_val, rhs_val)
                    parts = not_null_parts + [body]
                    z3_constraints.append(z3.And(parts))
                break

        # --- in: enumeration -----------------------------------------------
        if "in" in constraint:
            args = constraint["in"]
            if len(args) >= 2:
                col_ref, values = args[0], args[1]
                sym_tbl = _table_for_ref(col_ref, tables)
                if sym_tbl is not None and isinstance(values, list):
                    for i in range(len(sym_tbl.rows)):
                        c_null, c_val = _resolve_operand(col_ref, tables, i)
                        if c_val is None:
                            continue
                        options = []
                        for v in values:
                            _, v_z3 = _resolve_operand(v, tables, i) if isinstance(v, dict) else (None, z3.RealVal(v))
                            if v_z3 is not None:
                                options.append(c_val == v_z3)
                        if options:
                            guard = z3.Not(c_null) if c_null is not None else z3.BoolVal(True)
                            z3_constraints.append(z3.Implies(guard, z3.Or(options)))

        # --- between: range ------------------------------------------------
        if "between" in constraint:
            args = constraint["between"]
            if len(args) >= 3:
                col_ref, lo, hi = args[0], args[1], args[2]
                sym_tbl = _table_for_ref(col_ref, tables)
                if sym_tbl is not None:
                    for i in range(len(sym_tbl.rows)):
                        c_null, c_val = _resolve_operand(col_ref, tables, i)
                        if c_val is None:
                            continue
                        _, lo_z3 = _resolve_operand(lo, tables, i) if isinstance(lo, dict) else (None, z3.RealVal(lo))
                        _, hi_z3 = _resolve_operand(hi, tables, i) if isinstance(hi, dict) else (None, z3.RealVal(hi))
                        if lo_z3 is not None and hi_z3 is not None:
                            guard = z3.Not(c_null) if c_null is not None else z3.BoolVal(True)
                            z3_constraints.append(z3.Implies(guard, z3.And(c_val >= lo_z3, c_val <= hi_z3)))

        # --- neq: inequality -----------------------------------------------
        if "neq" in constraint:
            args = constraint["neq"]
            if len(args) >= 2:
                lhs_ref, rhs_ref = args[0], args[1]
                sym_tbl = _table_for_ref(lhs_ref, tables)
                if sym_tbl is not None:
                    for i in range(len(sym_tbl.rows)):
                        lhs_null, lhs_val = _resolve_operand(lhs_ref, tables, i)
                        rhs_null, rhs_val = _resolve_operand(rhs_ref, tables, i)
                        if lhs_val is None or rhs_val is None:
                            continue
                        guard_parts = []
                        if lhs_null is not None:
                            guard_parts.append(z3.Not(lhs_null))
                        if rhs_null is not None:
                            guard_parts.append(z3.Not(rhs_null))
                        if guard_parts:
                            z3_constraints.append(z3.Implies(z3.And(guard_parts), lhs_val != rhs_val))
                        else:
                            z3_constraints.append(lhs_val != rhs_val)

        # --- inc / consec: consecutive integers ----------------------------
        for key in ("inc", "consec"):
            if key in constraint:
                col_ref = constraint[key]
                sym_tbl = _table_for_ref(col_ref, tables)
                if sym_tbl is not None:
                    for i in range(len(sym_tbl.rows)):
                        c_null, c_val = _resolve_operand(col_ref, tables, i)
                        if c_val is None:
                            continue
                        guard = z3.Not(c_null) if c_null is not None else z3.BoolVal(True)
                        z3_constraints.append(z3.Implies(guard, c_val == z3.RealVal(i + 1)))

        # --- imply: skip for now (complex recursive encoding) --------------


# ---------------------------------------------------------------------------
# Expression evaluation
# ---------------------------------------------------------------------------

def _get_tables_for_query(ir: QueryIR) -> dict[str, str]:
    """Return mapping: alias → actual table name.

    Recursively collects tables from subqueries (ExistsSubquery,
    InSubquery, ScalarSubquery) so they are included in the symbolic DB.
    """
    from ..ir.types import RelRef, DerivedTable

    tables: dict[str, str] = {}

    def _add(rel) -> None:
        if isinstance(rel, RelRef):
            alias = (rel.alias or rel.table).lower()
            tables[alias] = rel.table.lower()
            tables[rel.table.lower()] = rel.table.lower()
        elif isinstance(rel, DerivedTable):
            dt_alias = rel.alias.lower()
            tables[dt_alias] = dt_alias
            # Also collect base tables from the inner query so they
            # are created in the symbolic DB for compositional encoding.
            inner_tables = _get_tables_for_query(rel.query)
            for k, v in inner_tables.items():
                # FIX.25a: Don't let inner query's table mapping
                # overwrite the DT alias → DT alias self-mapping.
                # E.g., inner FROM PERSON AS A adds 'a' → 'person',
                # which would overwrite the outer 'a' → 'a' mapping
                # needed for compositional encoding.
                if k != dt_alias:
                    tables[k] = v

    _add(ir.from_table)
    for join in ir.joins:
        _add(join.right)
    if ir.set_right:
        tables.update(_get_tables_for_query(ir.set_right))

    # Recursively collect tables from subquery expressions
    def _walk_expr(expr):
        if expr is None:
            return
        if isinstance(expr, ExistsSubquery):
            tables.update(_get_tables_for_query(expr.query))
        elif isinstance(expr, InSubquery):
            _walk_expr(expr.expr)
            tables.update(_get_tables_for_query(expr.query))
        elif isinstance(expr, ScalarSubquery):
            tables.update(_get_tables_for_query(expr.query))
        elif isinstance(expr, BinOp):
            _walk_expr(expr.left)
            _walk_expr(expr.right)
        elif isinstance(expr, UnaryOp):
            _walk_expr(expr.operand)
        elif isinstance(expr, AggCall):
            _walk_expr(expr.arg)
        elif isinstance(expr, FuncCall):
            for a in expr.args:
                _walk_expr(a)
        elif isinstance(expr, CaseExpr):
            for cw in expr.whens:
                _walk_expr(cw.when)
                _walk_expr(cw.then)
            _walk_expr(expr.else_)
        elif isinstance(expr, InList):
            _walk_expr(expr.expr)
            for v in expr.values:
                _walk_expr(v)
        elif isinstance(expr, Between):
            _walk_expr(expr.expr)
            _walk_expr(expr.low)
            _walk_expr(expr.high)
        elif isinstance(expr, WindowFunc):
            for a in expr.args:
                _walk_expr(a)
            for p in expr.partition_by:
                _walk_expr(p)
            for o in expr.order_by:
                _walk_expr(o.expr)

    for s in ir.select:
        _walk_expr(s)
    _walk_expr(ir.where)
    for j in ir.joins:
        _walk_expr(j.on)
    for g in ir.group_by:
        _walk_expr(g)
    _walk_expr(ir.having)
    for o in ir.order_by:
        _walk_expr(o.expr)
    return tables


def _make_binding(
    combo: Combo,
    alias_to_table: dict[str, str],
    db: SymbolicDB,
) -> dict[str, SymbolicRow]:
    """Create a row binding for a combo."""
    binding: dict[str, SymbolicRow] = {}
    for alias, row_idx in combo.items():
        actual_table = alias_to_table.get(alias, alias)
        sym_table = db.tables.get(actual_table)
        if sym_table:
            binding[alias] = sym_table.rows[row_idx]
            # Also bind by actual table name, but only if no other alias
            # already claimed this name (avoids self-join overwrite where
            # EMP0→emp overwrites the earlier EMP→emp binding).
            if actual_table not in binding:
                binding[actual_table] = sym_table.rows[row_idx]
    return binding


def _make_binding_scoped(
    combo: Combo,
    alias_to_table: dict[str, str],
    db: SymbolicDB,
) -> dict[str, SymbolicRow]:
    """Create a scoped row binding for an inner subquery combo.

    FIX.26b: Unlike _make_binding, this only binds each relation under
    its visible qualifier (alias if aliased, table name if not).  It does
    NOT add the base table name as a secondary key for aliased tables.
    This prevents inner binding keys from shadowing outer binding keys
    when inner and outer reference the same physical table.

    Example: inner `FROM PERSON P` binds only 'p', not 'person'.
    This lets outer 'person' remain accessible for qualified refs like
    `PERSON.EMAIL` that refer to the outer table.
    """
    binding: dict[str, SymbolicRow] = {}
    for alias, row_idx in combo.items():
        actual_table = alias_to_table.get(alias, alias)
        sym_table = db.tables.get(actual_table)
        if sym_table:
            binding[alias] = sym_table.rows[row_idx]
    return binding


def _encode_inner_query(
    inner_ir: QueryIR,
    outer_binding: dict[str, SymbolicRow],
) -> list[ResultRow]:
    """Encode an inner subquery result using the same symbolic DB.

    Uses module-level _current_db, _current_scope, _current_catalog
    (set during the synthesis run) so this can be called from within
    _eval_value / _eval_predicate_3vl without threading extra parameters.

    For correlated subqueries, the outer_binding provides column values
    from the outer query's current row combination.

    Returns a list of ResultRow, each with .survives and .values.
    """
    db = _current_db
    scope = _current_scope
    if db is None or scope is None:
        return []

    # FIX.30c: Compositionally encode any DerivedTable nodes inside this
    # inner query before evaluating it.  Without this, DTs created by
    # tuple-IN lowering (FIX.30a) inside EXISTS subqueries remain as
    # unconstrained symbolic rows, causing spurious SAT.
    #
    # DTs inside EXISTS/IN/scalar subqueries are found by _get_tables_for_query
    # (which recurses into subqueries) but NOT by _collect_derived_table_schemas
    # (which only walks direct FROM/JOINs).  So their schemas are never added
    # to the catalog and they have no entry in db.tables.  We must:
    # 1. Augment the catalog with the DT schema
    # 2. Create placeholder symbolic rows
    # 3. Replace them with compositionally encoded rows
    catalog = _current_catalog
    if catalog is not None:
        for dt in _collect_remaining_derived_tables(inner_ir):
            dt_alias = dt.alias.lower()
            if dt_alias not in db.tables:
                # Create catalog entry and placeholder symbolic rows for the DT
                col_names = _get_projected_col_names(dt)
                if col_names:
                    from ..schema.catalog import ColumnInfo as CI, TableInfo as TI
                    dt_cols = [CI(name=cn, sem_type=SemType.INT, nullable=True) for cn in col_names]
                    dt_tinfo = TI(name=dt_alias, columns=dt_cols)
                    catalog.tables[dt_alias] = dt_tinfo
                    # Create placeholder symbolic rows (will be replaced below)
                    placeholder_rows = []
                    col_types: dict[str, SemType] = {}
                    for cn in col_names:
                        col_types[cn] = SemType.INT
                    for ri in range(scope.k_rows):
                        cols = {}
                        for cn in col_names:
                            prefix = f"{dt_alias}_{cn}_{ri}"
                            import z3 as _z3
                            cols[cn] = NullableVal(
                                is_null=_z3.Bool(f"{prefix}_n"),
                                val=_z3.Real(f"{prefix}_v"),
                            )
                        placeholder_rows.append(SymbolicRow(cols=cols))
                    db.tables[dt_alias] = SymbolicTable(name=dt_alias, rows=placeholder_rows, col_types=col_types)
            # Now encode compositionally (replace placeholder or existing unconstrained rows)
            encoded = _encode_derived_table_rows(dt, db, catalog, scope)
            if encoded is not None:
                db.tables[dt_alias] = encoded
                logger.debug("Compositional encoding (inner): replaced %s with bound rows", dt_alias)

    inner_alias_to_table = _get_tables_for_query(inner_ir)
    inner_aliases, inner_combos = _enumerate_combos(inner_ir, scope.k_rows, db, inner_alias_to_table)
    # FIX.26a: Use the same aggregation check as _encode_query_result:
    # GROUP BY without SELECT-level aggregates (e.g., SELECT EMAIL ...
    # GROUP BY EMAIL HAVING COUNT(*) > 1) must still use the aggregation
    # path to correctly evaluate group deduplication and HAVING.
    is_agg = inner_ir.has_aggregation() or bool(inner_ir.group_by)

    # Build (survives, binding) pairs for each combo, merging outer binding
    combo_data: list[tuple[z3.ExprRef, dict[str, SymbolicRow]]] = []
    for combo in inner_combos:
        # FIX.26b: Use scoped binding that only binds under the visible
        # qualifier (alias or unaliased table name), not the base table
        # name for aliased tables.  This prevents inner `FROM PERSON P`
        # from claiming the 'person' key, which would shadow the outer
        # query's 'person' binding and break qualified refs like
        # `PERSON.EMAIL` that refer to the outer table.
        binding = _make_binding_scoped(combo, inner_alias_to_table, db)
        # Merge: inner entries first (for SQL inner-scope-shadows-outer),
        # then outer entries that don't conflict.
        merged = dict(binding)
        for k, v in outer_binding.items():
            if k not in merged:
                merged[k] = v
        present = _binding_rows_present(binding)
        join_ok = _combo_survives_join_only(inner_ir, merged)
        combo_data.append((z3.And(present, join_ok), merged))

    if not is_agg:
        result_rows: list[ResultRow] = []
        for survives, binding in combo_data:
            where_ok = _eval_where(inner_ir, binding)
            final = z3.And(survives, where_ok)
            values = [_eval_value(expr, binding) for expr in inner_ir.select]
            result_rows.append(ResultRow(survives=final, values=values))
        return result_rows
    else:
        return _encode_aggregated_result_v2(inner_ir, combo_data, scope)


def _eval_value(
    expr: Expr,
    binding: dict[str, SymbolicRow],
) -> NullableVal:
    """Evaluate a value expression against a row binding. Returns NullableVal."""
    if isinstance(expr, ColumnRef):
        table_key = (expr.table or "").lower()
        row = binding.get(table_key)
        if row is None:
            # Try to find column in any bound table
            for r in binding.values():
                if expr.column.lower() in r.cols:
                    row = r
                    break
        if row is None:
            # FIX.36a: Unqualified ref matching a table alias (tuple ref).
            # Textbook SQL uses B(X) where X is a table alias.  If the
            # column name matches a binding key (table alias) and that
            # table has exactly one column, return that column's value.
            alias_row = binding.get(expr.column.lower())
            if alias_row is not None and len(alias_row.cols) == 1:
                return next(iter(alias_row.cols.values()))
            return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))
        col = row.cols.get(expr.column.lower())
        if col is None:
            return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))
        return col

    if isinstance(expr, Literal):
        if expr.value is None:
            return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))
        if isinstance(expr.value, bool):
            return NullableVal(is_null=z3.BoolVal(False), val=z3.RealVal(1 if expr.value else 0))
        if isinstance(expr.value, (int, float)):
            return NullableVal(is_null=z3.BoolVal(False), val=z3.RealVal(expr.value))
        if isinstance(expr.value, str):
            idx = _current_sym2idx.get(expr.value)
            if idx is not None:
                return NullableVal(is_null=z3.BoolVal(False), val=z3.IntVal(idx))
            # Fallback for literals not in symbol table (shouldn't happen if collection is complete)
            logger.warning("String literal %r not in symbol table", expr.value)
            return NullableVal(is_null=z3.BoolVal(False), val=z3.IntVal(len(_current_sym2idx)))
        return NullableVal(is_null=z3.BoolVal(False), val=z3.RealVal(0))

    if isinstance(expr, BinOp) and expr.op in _ARITH_OPS:
        # FIX.28c: Date arithmetic (date +/- int) — convert date symbol
        # indices to day-offsets, perform arithmetic in day-offset space.
        # This keeps `date + 1` consistent with `DATEDIFF(d1, d2)` so
        # equivalent expressions produce the same Z3 terms.
        if expr.op in (BinOpKind.ADD, BinOpKind.SUB) and _is_date_arithmetic(expr):
            left = _eval_value(expr.left, binding)
            right = _eval_value(expr.right, binding)
            either_null = z3.Or(left.is_null, right.is_null)
            # Convert date operand(s) to day-offsets; leave int operands as-is
            left_days = _sym_idx_to_day_offset(left.val) if _is_date_expr_type(expr.left) else None
            right_days = _sym_idx_to_day_offset(right.val) if _is_date_expr_type(expr.right) else None
            l_val = left_days if left_days is not None else left.val
            r_val = right_days if right_days is not None else right.val
            val = _arith_op(expr.op, l_val, r_val)
            return NullableVal(
                is_null=either_null,
                val=z3.If(either_null, z3.RealVal(0), val),
            )

        left = _eval_value(expr.left, binding)
        right = _eval_value(expr.right, binding)
        either_null = z3.Or(left.is_null, right.is_null)
        val = _arith_op(expr.op, left.val, right.val)
        return NullableVal(
            is_null=either_null,
            val=z3.If(either_null, z3.RealVal(0), val),
        )

    if isinstance(expr, FuncCall):
        fname = expr.func_name.upper()

        if fname == "COALESCE" and len(expr.args) >= 2:
            # Fold right-to-left: COALESCE(a,b,c) = IF(a IS NULL, COALESCE(b,c), a)
            result = _eval_value(expr.args[-1], binding)
            for arg in reversed(expr.args[:-1]):
                prev = _eval_value(arg, binding)
                result = NullableVal(
                    is_null=z3.And(prev.is_null, result.is_null),
                    val=z3.If(prev.is_null, result.val, prev.val),
                )
            return result

        if fname == "NULLIF" and len(expr.args) >= 2:
            a = _eval_value(expr.args[0], binding)
            b = _eval_value(expr.args[1], binding)
            # NULLIF(a,b) = NULL if a=b is TRUE, else a.
            # If a=b is UNKNOWN (either null), result is a (per SQL spec).
            eq = _compare_3vl(BinOpKind.EQ, a, b)
            eq_is_true = z3.And(z3.Not(eq.is_unknown), eq.val)
            return NullableVal(
                is_null=z3.Or(a.is_null, eq_is_true),
                val=a.val,
            )

        if fname in ("IIF", "IF") and len(expr.args) >= 3:
            # IIF(cond, true_val, false_val) = CASE WHEN cond THEN true_val ELSE false_val END
            cond = _eval_predicate_3vl(expr.args[0], binding)
            true_val = _eval_value(expr.args[1], binding)
            false_val = _eval_value(expr.args[2], binding)
            cond_true = _tb_true(cond)
            return NullableVal(
                is_null=z3.If(cond_true, true_val.is_null, false_val.is_null),
                val=z3.If(cond_true, true_val.val, false_val.val),
            )

        if fname in ("IIF", "IF") and len(expr.args) == 2:
            # IF(cond, true_val) — true_val if true, NULL otherwise
            cond = _eval_predicate_3vl(expr.args[0], binding)
            true_val = _eval_value(expr.args[1], binding)
            cond_true = _tb_true(cond)
            return NullableVal(
                is_null=z3.If(cond_true, true_val.is_null, z3.BoolVal(True)),
                val=z3.If(cond_true, true_val.val, z3.RealVal(0)),
            )

        if fname == "IFNULL" and len(expr.args) >= 2:
            # IFNULL(a, b) = COALESCE(a, b) = if a IS NULL then b else a
            a = _eval_value(expr.args[0], binding)
            b = _eval_value(expr.args[1], binding)
            return NullableVal(
                is_null=z3.And(a.is_null, b.is_null),
                val=z3.If(a.is_null, b.val, a.val),
            )

        if fname == "ISNULL" and len(expr.args) >= 1:
            # FIX.24a: MySQL ISNULL(x): returns 1 if x IS NULL, 0 otherwise
            a = _eval_value(expr.args[0], binding)
            return NullableVal(
                is_null=z3.BoolVal(False),
                val=z3.If(a.is_null, z3.RealVal(1), z3.RealVal(0)),
            )

        if fname == "ABS" and len(expr.args) >= 1:
            a = _eval_value(expr.args[0], binding)
            return NullableVal(
                is_null=a.is_null,
                val=z3.If(a.val >= 0, a.val, -a.val),
            )

        if fname == "ROUND" and len(expr.args) >= 1:
            # FIX.27: ROUND(expr, n) under RealSort encoding.
            # FIX.28a: ROUND(x) without precision arg defaults to ROUND(x, 0).
            # ROUND(x) = floor(x + 0.5) for non-negative,
            # = -floor(-x + 0.5) for negative (half-away-from-zero).
            # ROUND(x, n) = ROUND(x * 10^n) / 10^n.
            inner = _eval_value(expr.args[0], binding)
            n_digits = 0
            if len(expr.args) >= 2:
                # For literal decimal places, use exact power of 10
                if isinstance(expr.args[1], Literal) and isinstance(expr.args[1].value, (int, float)):
                    n_digits = int(expr.args[1].value)
                    scale = z3.RealVal(10 ** n_digits)
                else:
                    scale = z3.RealVal(1)  # fallback: identity for non-literal
            else:
                scale = z3.RealVal(1)
            scaled = inner.val * scale
            # Half-away-from-zero: floor(|x| + 0.5) * sign(x)
            half = z3.RealVal(z3.Q(1, 2))  # exact 1/2
            rounded = z3.If(
                scaled >= 0,
                z3.ToReal(z3.ToInt(scaled + half)),
                -z3.ToReal(z3.ToInt(-scaled + half)),
            )
            result_val = rounded / scale if n_digits != 0 else rounded
            return NullableVal(is_null=inner.is_null, val=result_val)

        if fname == "CAST" and len(expr.args) >= 1:
            # FIX.13d: Improved CAST encoding.
            # CAST(NULL AS type) → NULL (with correct nullability).
            # CAST(expr AS BOOLEAN) → clamp to 0/1.
            # Other CAST: identity under RealSort encoding.
            inner_val = _eval_value(expr.args[0], binding)
            # Inspect target type if available (args[1] is Literal with type name)
            if len(expr.args) >= 2 and isinstance(expr.args[1], Literal) and isinstance(expr.args[1].value, str):
                target = expr.args[1].value.upper()
                if target in ("BOOLEAN", "BOOL"):
                    return NullableVal(
                        is_null=inner_val.is_null,
                        val=z3.If(inner_val.is_null, z3.RealVal(0),
                                  z3.If(inner_val.val != 0, z3.RealVal(1), z3.RealVal(0))),
                    )
            return inner_val

        if fname == "GREATEST" and len(expr.args) >= 2:
            # PostgreSQL semantics: NULL if any argument is NULL
            result = _eval_value(expr.args[0], binding)
            for arg in expr.args[1:]:
                b = _eval_value(arg, binding)
                result = NullableVal(
                    is_null=z3.Or(result.is_null, b.is_null),
                    val=z3.If(result.val >= b.val, result.val, b.val),
                )
            return result

        if fname == "LEAST" and len(expr.args) >= 2:
            # PostgreSQL semantics: NULL if any argument is NULL
            result = _eval_value(expr.args[0], binding)
            for arg in expr.args[1:]:
                b = _eval_value(arg, binding)
                result = NullableVal(
                    is_null=z3.Or(result.is_null, b.is_null),
                    val=z3.If(result.val <= b.val, result.val, b.val),
                )
            return result

        # FIX.28c: DATEDIFF, DATE_ADD, DATE_SUB — date arithmetic functions.
        # Dates are encoded as indices into a lex-sorted string symbol table.
        # Convert indices to day-offsets via ITE chain, compute, then convert back.
        if fname == "DATEDIFF" and len(expr.args) >= 2:
            a = _eval_value(expr.args[0], binding)
            b = _eval_value(expr.args[1], binding)
            either_null = z3.Or(a.is_null, b.is_null)
            # Map symbol indices to day-offsets, then subtract
            a_days = _sym_idx_to_day_offset(a.val)
            b_days = _sym_idx_to_day_offset(b.val)
            if a_days is not None and b_days is not None:
                result_val = a_days - b_days
            else:
                result_val = z3.Real(f"func_DATEDIFF_{id(expr)}_{id(binding)}")
            return NullableVal(is_null=either_null, val=result_val)

        if fname in ("DATE_ADD", "ADDDATE") and len(expr.args) >= 2:
            a = _eval_value(expr.args[0], binding)
            b = _eval_value(expr.args[1], binding)
            either_null = z3.Or(a.is_null, b.is_null)
            # Convert date arg to day-offset, add interval, result is day-offset
            a_days = _sym_idx_to_day_offset(a.val)
            if a_days is not None:
                result_val = a_days + b.val
            else:
                result_val = z3.Real(f"func_{fname}_{id(expr)}_{id(binding)}")
            return NullableVal(is_null=either_null, val=result_val)

        if fname in ("DATE_SUB", "SUBDATE") and len(expr.args) >= 2:
            a = _eval_value(expr.args[0], binding)
            b = _eval_value(expr.args[1], binding)
            either_null = z3.Or(a.is_null, b.is_null)
            a_days = _sym_idx_to_day_offset(a.val)
            if a_days is not None:
                result_val = a_days - b.val
            else:
                result_val = z3.Real(f"func_{fname}_{id(expr)}_{id(binding)}")
            return NullableVal(is_null=either_null, val=result_val)

        if fname == "TIMESTAMPDIFF" and len(expr.args) >= 3:
            # TIMESTAMPDIFF(unit, date1, date2) — similar to DATEDIFF but
            # with an explicit unit arg. Treat as DATEDIFF for DAY unit.
            a = _eval_value(expr.args[1], binding)
            b = _eval_value(expr.args[2], binding)
            either_null = z3.Or(a.is_null, b.is_null)
            a_days = _sym_idx_to_day_offset(a.val)
            b_days = _sym_idx_to_day_offset(b.val)
            if a_days is not None and b_days is not None:
                result_val = b_days - a_days
            else:
                result_val = z3.Real(f"func_TIMESTAMPDIFF_{id(expr)}_{id(binding)}")
            return NullableVal(is_null=either_null, val=result_val)

        if fname == "LENGTH" and len(expr.args) >= 1:
            # Under integer-coded strings, LENGTH maps each symbol-table
            # index to its known string length. For unknown strings,
            # returns a fresh variable (conservative).
            a = _eval_value(expr.args[0], binding)
            if _current_sym2idx:
                # Build ITE chain: if val==idx then len(str) else ...
                idx2str = {idx: s for s, idx in _current_sym2idx.items()}
                result_val: z3.ExprRef = z3.IntVal(0)
                for idx in sorted(idx2str.keys(), reverse=True):
                    result_val = z3.If(a.val == idx, z3.IntVal(len(idx2str[idx])), result_val)
                return NullableVal(is_null=a.is_null, val=result_val)
            return NullableVal(
                is_null=a.is_null,
                val=z3.Real(f"func_LENGTH_{id(expr)}_{id(binding)}"),
            )

        if fname in ("FLOOR", "CEIL", "CEILING", "TRUNCATE") and len(expr.args) >= 1:
            # FIX.27: Proper encoding under RealSort.
            inner = _eval_value(expr.args[0], binding)
            if fname == "FLOOR":
                val = z3.ToReal(z3.ToInt(inner.val))
            elif fname in ("CEIL", "CEILING"):
                # ceil(x) = -floor(-x)
                val = -z3.ToReal(z3.ToInt(-inner.val))
            else:  # TRUNCATE
                # truncate(x) = sign(x) * floor(|x|)
                val = z3.If(
                    inner.val >= 0,
                    z3.ToReal(z3.ToInt(inner.val)),
                    -z3.ToReal(z3.ToInt(-inner.val)),
                )
            return NullableVal(is_null=inner.is_null, val=val)

        if fname in ("UPPER", "LOWER") and len(expr.args) >= 1:
            # Under integer-coded strings, UPPER/LOWER maps each
            # symbol-table index to the index of its uppercased/lowercased
            # form (if present in the symbol table).
            a = _eval_value(expr.args[0], binding)
            if _current_sym2idx:
                idx2str = {idx: s for s, idx in _current_sym2idx.items()}
                transform = str.upper if fname == "UPPER" else str.lower
                result_val = a.val  # default: identity if transform not in table
                for idx in sorted(idx2str.keys(), reverse=True):
                    transformed = transform(idx2str[idx])
                    target_idx = _current_sym2idx.get(transformed)
                    if target_idx is not None:
                        result_val = z3.If(a.val == idx, z3.IntVal(target_idx), result_val)
                return NullableVal(is_null=a.is_null, val=result_val)
            return NullableVal(
                is_null=a.is_null,
                val=z3.Real(f"func_{fname}_{id(expr)}_{id(binding)}"),
            )

        # FIX.36a: Unknown function names (e.g. B(X), B1(X)) are encoded
        # as Z3 uninterpreted functions so that structurally identical
        # calls in Q1 and Q2 share the same Z3 symbol. This makes
        # x=y ⟹ f(x)=f(y) provable and avoids spurious SAT.
        if fname not in _KNOWN_SQL_FUNCS and expr.args:
            arity = len(expr.args)
            key = (fname, arity)
            if key not in _uninterp_funcs:
                # z3.Function(name, *domain_sorts, range_sort)
                _uninterp_funcs[key] = z3.Function(
                    fname, *([z3.RealSort()] * (arity + 1)),
                )
            uf = _uninterp_funcs[key]
            arg_vals = [_eval_value(a, binding) for a in expr.args]
            uf_result = uf(*[av.val for av in arg_vals])
            any_null = arg_vals[0].is_null
            for av in arg_vals[1:]:
                any_null = z3.Or(any_null, av.is_null)
            return NullableVal(is_null=any_null, val=uf_result)

        # For other known-but-unhandled functions (STRFTIME, SUBSTR, etc.),
        # use a fresh unconstrained Z3 variable whose nullability depends
        # on the first column argument.  Bug #20: see original comment.
        if expr.args:
            col_arg = None
            for arg in expr.args:
                if not isinstance(arg, Literal):
                    col_arg = _eval_value(arg, binding)
                    break
            fresh = z3.Real(f"func_{expr.func_name}_{id(expr)}_{id(binding)}")
            if col_arg is not None:
                return NullableVal(is_null=col_arg.is_null, val=fresh)
            return NullableVal(is_null=z3.BoolVal(False), val=fresh)

    if isinstance(expr, CaseExpr):
        # CASE WHEN c1 THEN v1 WHEN c2 THEN v2 ... ELSE ve END
        # Encoded as nested If: If(c1, v1, If(c2, v2, ..., ve))
        else_val = _eval_value(expr.else_, binding) if expr.else_ is not None else NullableVal(
            is_null=z3.BoolVal(True), val=z3.RealVal(0),
        )
        # Build from the inside out (last WHEN first)
        result_is_null = else_val.is_null
        result_val = else_val.val
        for cw in reversed(expr.whens):
            cond = _eval_predicate(cw.when, binding)
            then_val = _eval_value(cw.then, binding)
            result_is_null = z3.If(cond, then_val.is_null, result_is_null)
            result_val = z3.If(cond, then_val.val, result_val)
        return NullableVal(is_null=result_is_null, val=result_val)

    if isinstance(expr, UnaryOp):
        if expr.op == UnaryOpKind.NEG:
            operand = _eval_value(expr.operand, binding)
            return NullableVal(is_null=operand.is_null, val=-operand.val)
        # FIX.13f: Boolean predicates used as values (e.g., in CASE THEN branches)
        if expr.op == UnaryOpKind.IS_NULL:
            val = _eval_value(expr.operand, binding)
            return NullableVal(is_null=z3.BoolVal(False), val=z3.If(val.is_null, z3.RealVal(1), z3.RealVal(0)))
        if expr.op == UnaryOpKind.IS_NOT_NULL:
            val = _eval_value(expr.operand, binding)
            return NullableVal(is_null=z3.BoolVal(False), val=z3.If(val.is_null, z3.RealVal(0), z3.RealVal(1)))
        if expr.op == UnaryOpKind.NOT:
            tb = _eval_predicate_3vl(expr.operand, binding)
            return NullableVal(
                is_null=tb.is_unknown,
                val=z3.If(tb.is_unknown, z3.RealVal(0), z3.If(tb.val, z3.RealVal(0), z3.RealVal(1))),
            )

    # FIX.13f: Comparison/boolean BinOps used as values (e.g., DEPTNO > 10 AND NULL)
    if isinstance(expr, BinOp) and expr.op in _COMPARE_OPS:
        tb = _eval_predicate_3vl(expr, binding)
        return NullableVal(
            is_null=tb.is_unknown,
            val=z3.If(tb.is_unknown, z3.RealVal(0), z3.If(tb.val, z3.RealVal(1), z3.RealVal(0))),
        )
    if isinstance(expr, BinOp) and expr.op in (BinOpKind.AND, BinOpKind.OR):
        tb = _eval_predicate_3vl(expr, binding)
        return NullableVal(
            is_null=tb.is_unknown,
            val=z3.If(tb.is_unknown, z3.RealVal(0), z3.If(tb.val, z3.RealVal(1), z3.RealVal(0))),
        )
    if isinstance(expr, BinOp) and expr.op == BinOpKind.IS:
        tb = _eval_predicate_3vl(expr, binding)
        return NullableVal(is_null=z3.BoolVal(False), val=z3.If(tb.val, z3.RealVal(1), z3.RealVal(0)))

    if isinstance(expr, Star):
        return NullableVal(is_null=z3.BoolVal(False), val=z3.RealVal(1))

    # ScalarSubquery: encode inner query and pick the first surviving row's value.
    # If 0 rows survive → NULL; if ≥1 → first surviving row's value.
    if isinstance(expr, ScalarSubquery):
        inner_result = _encode_inner_query(expr.query, binding)
        if not inner_result:
            return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))

        surviving_values = []
        for row in inner_result:
            if row.values:
                surviving_values.append((row.survives, row.values[0]))

        if not surviving_values:
            return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))

        # Use if-then-else chain: first surviving row's value
        # Start with NULL (no rows survive)
        result_val = z3.RealVal(0)
        result_null = z3.BoolVal(True)
        for surv, val in reversed(surviving_values):
            result_val = z3.If(surv, val.val, result_val)
            result_null = z3.If(surv, val.is_null, result_null)

        return NullableVal(is_null=result_null, val=result_val)

    # FIX.18c: InSubquery as value expression (e.g., SELECT col IN (SELECT ...))
    # Evaluate as boolean: TRUE(1) / FALSE(0) / UNKNOWN(NULL)
    if isinstance(expr, InSubquery):
        tb = _eval_predicate_3vl(expr, binding)
        return NullableVal(
            is_null=tb.is_unknown,
            val=z3.If(tb.is_unknown, z3.RealVal(0), z3.If(tb.val, z3.RealVal(1), z3.RealVal(0))),
        )

    # FIX.18c: ExistsSubquery as value expression
    if isinstance(expr, ExistsSubquery):
        tb = _eval_predicate_3vl(expr, binding)
        return NullableVal(
            is_null=z3.BoolVal(False),
            val=z3.If(tb.val, z3.RealVal(1), z3.RealVal(0)),
        )

    # WindowFunc: look up precomputed value from _precompute_window_values.
    if isinstance(expr, WindowFunc):
        key = (id(expr), id(binding))
        cached = _precomputed_windows.get(key)
        if cached is not None:
            return cached
        # Fallback for window functions not precomputed (e.g., in subqueries)
        logger.debug("WindowFunc %s not precomputed (binding id=%s), using fresh var", expr.func_name, id(binding))
        fresh = z3.Real(f"wf_{expr.func_name}_{id(expr)}_{id(binding)}")
        return NullableVal(is_null=z3.BoolVal(False), val=fresh)

    # Fallback: use a fresh variable instead of constant NULL to avoid
    # vacuous UNSAT from unmodeled expression types.
    fresh = z3.Real(f"fallback_{id(expr)}_{id(binding)}")
    return NullableVal(is_null=z3.BoolVal(False), val=fresh)


_ARITH_OPS = {BinOpKind.ADD, BinOpKind.SUB, BinOpKind.MUL, BinOpKind.DIV, BinOpKind.MOD}

_COMPARE_OPS = {BinOpKind.EQ, BinOpKind.NEQ, BinOpKind.LT, BinOpKind.GT, BinOpKind.LTE, BinOpKind.GTE}


def _sym_idx_to_day_offset(val: z3.ExprRef) -> z3.ExprRef | None:
    """Convert a symbol-table index to a day-offset Z3 expression.

    FIX.28c: For each date-like string in the symbol table (YYYY-MM-DD format),
    build an ITE chain mapping its index to the number of days since 2024-01-01.
    Returns None if the symbol table has no parseable dates.
    """
    if not _current_sym2idx:
        return None
    idx2str = {idx: s for s, idx in _current_sym2idx.items()}
    base = date(2024, 1, 1)
    has_any = False
    result: z3.ExprRef = z3.IntVal(0)
    for idx in sorted(idx2str.keys(), reverse=True):
        s = idx2str[idx]
        try:
            d = date.fromisoformat(s)
            days = (d - base).days
            result = z3.If(val == idx, z3.IntVal(days), result)
            has_any = True
        except (ValueError, TypeError):
            pass
    return result if has_any else None


def _is_date_expr_type(e: Expr) -> bool:
    """Check if an expression has DATE or TIMESTAMP type.

    FIX.28c: Used to detect date operands in arithmetic and convert
    them from symbol-table indices to day-offsets.
    """
    if e.sem_type in (SemType.DATE, SemType.TIMESTAMP):
        return True
    if isinstance(e, ColumnRef) and _current_catalog is not None:
        tname = (e.table or "").lower()
        for t in _current_catalog.tables.values():
            if t.name.lower() == tname or tname == "":
                for c in t.columns:
                    if c.name.lower() == e.column.lower():
                        if c.sem_type in (SemType.DATE, SemType.TIMESTAMP):
                            return True
    return False


def _is_date_arithmetic(expr: BinOp) -> bool:
    """Check if a BinOp is date arithmetic (date +/- integer)."""
    return _is_date_expr_type(expr.left) or _is_date_expr_type(expr.right)


def _arith_op(op: BinOpKind, a: z3.ExprRef, b: z3.ExprRef) -> z3.ExprRef:
    if op == BinOpKind.ADD:
        return a + b
    if op == BinOpKind.SUB:
        return a - b
    if op == BinOpKind.MUL:
        return a * b
    if op == BinOpKind.DIV:
        return z3.If(b == 0, z3.RealVal(0), a / b)
    if op == BinOpKind.MOD:
        # z3 % only works on IntSort. Coerce to Int for modulo.
        a_int = z3.ToInt(a) if a.sort().kind() == z3.Z3_REAL_SORT else a
        b_int = z3.ToInt(b) if b.sort().kind() == z3.Z3_REAL_SORT else b
        return z3.If(b == 0, z3.RealVal(0), z3.ToReal(a_int % b_int))
    return a


# ---------------------------------------------------------------------------
# Predicate evaluation (SQL 3-valued logic via TriBool)
# ---------------------------------------------------------------------------

def _eval_predicate_3vl(
    expr: Expr,
    binding: dict[str, SymbolicRow],
) -> TriBool:
    """Evaluate a boolean predicate under SQL 3-valued logic."""
    if isinstance(expr, BinOp):
        if expr.op in _COMPARE_OPS:
            left = _eval_value(expr.left, binding)
            right = _eval_value(expr.right, binding)
            return _compare_3vl(expr.op, left, right)

        if expr.op == BinOpKind.AND:
            return _tb_and(
                _eval_predicate_3vl(expr.left, binding),
                _eval_predicate_3vl(expr.right, binding),
            )

        if expr.op == BinOpKind.OR:
            return _tb_or(
                _eval_predicate_3vl(expr.left, binding),
                _eval_predicate_3vl(expr.right, binding),
            )

        if expr.op == BinOpKind.LIKE:
            left = _eval_value(expr.left, binding)
            right = _eval_value(expr.right, binding)
            # Exact LIKE encoding: when the pattern is a literal string,
            # match against all strings in the symbol table that satisfy
            # the LIKE pattern, producing a disjunction of equalities.
            if isinstance(expr.right, Literal) and isinstance(expr.right.value, str) and _current_sym2idx:
                import fnmatch
                pattern = expr.right.value
                # Convert SQL LIKE to fnmatch: % → *, _ → ?
                fn_pattern = pattern.replace("%", "*").replace("_", "?")
                matching_idxs = [
                    idx for s, idx in _current_sym2idx.items()
                    if fnmatch.fnmatchcase(s, fn_pattern)
                ]
                either_null = z3.Or(left.is_null, right.is_null)
                if matching_idxs:
                    match_expr = z3.Or([left.val == z3.IntVal(idx) for idx in matching_idxs])
                else:
                    match_expr = z3.BoolVal(False)
                return TriBool(is_unknown=either_null, val=match_expr)
            # Fallback: treat LIKE as equality (approximate)
            return _compare_3vl(BinOpKind.EQ, left, right)

        if expr.op == BinOpKind.IN:
            # BinOp(op=IN) should not be produced by the parser (it uses InList),
            # but guard against it to avoid silent fallthrough to unconstrained fresh variable.
            logger.warning("BinOp(op=IN) encountered in witness synthesis; expected InList node")
            left = _eval_value(expr.left, binding)
            right = _eval_value(expr.right, binding)
            return _compare_3vl(BinOpKind.EQ, left, right)

        if expr.op == BinOpKind.IS:
            # IS NULL check
            if isinstance(expr.right, Literal) and expr.right.value is None:
                left = _eval_value(expr.left, binding)
                return _tb_from_bool(left.is_null)
            # IS TRUE / IS FALSE (FIX.13f): 2-valued boolean tests
            if isinstance(expr.right, Literal) and isinstance(expr.right.value, bool):
                left = _eval_value(expr.left, binding)
                if expr.right.value:
                    # x IS TRUE → x is non-null AND x != 0
                    return _tb_from_bool(z3.And(z3.Not(left.is_null), left.val != 0))
                else:
                    # x IS FALSE → x is non-null AND x == 0
                    return _tb_from_bool(z3.And(z3.Not(left.is_null), left.val == 0))
            # IS is a 2-valued operator: NULL IS NULL → TRUE
            left = _eval_value(expr.left, binding)
            right = _eval_value(expr.right, binding)
            both_null = z3.And(left.is_null, right.is_null)
            both_non_null_eq = z3.And(
                z3.Not(left.is_null), z3.Not(right.is_null), left.val == right.val,
            )
            return _tb_from_bool(z3.Or(both_null, both_non_null_eq))

        # IS NOT DISTINCT FROM: null-safe equality (2-valued, never UNKNOWN)
        if expr.op == BinOpKind.IS_NOT_DISTINCT_FROM:
            left = _eval_value(expr.left, binding)
            right = _eval_value(expr.right, binding)
            both_null = z3.And(left.is_null, right.is_null)
            both_non_null_eq = z3.And(
                z3.Not(left.is_null), z3.Not(right.is_null), left.val == right.val,
            )
            return _tb_from_bool(z3.Or(both_null, both_non_null_eq))

        # IS DISTINCT FROM: null-safe inequality (2-valued, never UNKNOWN)
        if expr.op == BinOpKind.IS_DISTINCT_FROM:
            left = _eval_value(expr.left, binding)
            right = _eval_value(expr.right, binding)
            both_null = z3.And(left.is_null, right.is_null)
            both_non_null_eq = z3.And(
                z3.Not(left.is_null), z3.Not(right.is_null), left.val == right.val,
            )
            return _tb_from_bool(z3.Not(z3.Or(both_null, both_non_null_eq)))

    if isinstance(expr, UnaryOp):
        if expr.op == UnaryOpKind.NOT:
            return _tb_not(_eval_predicate_3vl(expr.operand, binding))
        if expr.op == UnaryOpKind.IS_NULL:
            val = _eval_value(expr.operand, binding)
            return _tb_from_bool(val.is_null)
        if expr.op == UnaryOpKind.IS_NOT_NULL:
            val = _eval_value(expr.operand, binding)
            return _tb_from_bool(z3.Not(val.is_null))

    if isinstance(expr, InList):
        target = _eval_value(expr.expr, binding)
        match_options = []
        has_null_option = []
        for v in expr.values:
            vv = _eval_value(v, binding)
            match_options.append(z3.And(
                z3.Not(target.is_null), z3.Not(vv.is_null),
                target.val == vv.val,
            ))
            has_null_option.append(vv.is_null)
        if not match_options:
            return _tb_from_bool(z3.BoolVal(False))
        any_match = z3.Or(match_options)
        any_null_in_list = z3.Or(has_null_option)
        # SQL 3VL: if target is NULL → UNKNOWN; if match → TRUE;
        # if no match but list has NULL → UNKNOWN; else FALSE
        return TriBool(
            is_unknown=z3.And(
                z3.Not(any_match),
                z3.Or(target.is_null, any_null_in_list),
            ),
            val=any_match,
        )

    if isinstance(expr, Between):
        val = _eval_value(expr.expr, binding)
        lo = _eval_value(expr.low, binding)
        hi = _eval_value(expr.high, binding)
        any_null = z3.Or(val.is_null, lo.is_null, hi.is_null)
        return TriBool(
            is_unknown=any_null,
            val=z3.And(val.val >= lo.val, val.val <= hi.val),
        )

    if isinstance(expr, Literal):
        if expr.value is True:
            return _tb_from_bool(z3.BoolVal(True))
        if expr.value is False:
            return _tb_from_bool(z3.BoolVal(False))
        if expr.value is None:
            return _tb_unknown()

    # EXISTS subquery: evaluate inner query, TRUE iff at least one row survives
    if isinstance(expr, ExistsSubquery):
        inner_result = _encode_inner_query(expr.query, binding)
        if inner_result:
            exists_true = z3.Or([r.survives for r in inner_result])
        else:
            exists_true = z3.BoolVal(False)
        # EXISTS is 2-valued (never UNKNOWN per SQL spec)
        return _tb_from_bool(exists_true)

    # IN (subquery): exact encoding with 3VL
    if isinstance(expr, InSubquery):
        target = _eval_value(expr.expr, binding)
        inner_result = _encode_inner_query(expr.query, binding)

        matches = []
        null_candidates = []
        for row in inner_result:
            if not row.values:
                continue
            inner_val = row.values[0]  # IN subquery projects exactly 1 column
            matches.append(z3.And(
                row.survives,
                z3.Not(target.is_null),
                z3.Not(inner_val.is_null),
                target.val == inner_val.val,
            ))
            null_candidates.append(z3.And(row.survives, inner_val.is_null))

        any_match = z3.Or(matches) if matches else z3.BoolVal(False)
        any_null = z3.Or(null_candidates) if null_candidates else z3.BoolVal(False)

        return TriBool(
            is_unknown=z3.And(z3.Not(any_match), z3.Or(target.is_null, any_null)),
            val=any_match,
        )

    # FIX.13f: Boolean-valued scalar expressions used as predicates
    # (e.g., CASE WHEN ... THEN TRUE ELSE FALSE END in WHERE/ON/HAVING).
    # Evaluate as a value and convert: NULL→UNKNOWN, 0→FALSE, nonzero→TRUE.
    if isinstance(expr, (CaseExpr, FuncCall, ColumnRef, ScalarSubquery, AggCall)):
        nv = _eval_value(expr, binding)
        return TriBool(
            is_unknown=nv.is_null,
            val=nv.val != 0,
        )

    # Fallback for unmodeled predicate types:
    # Use fresh unconstrained TriBool (can be TRUE, FALSE, or UNKNOWN)
    fresh_val = z3.Bool(f"pred_fallback_{id(expr)}_{id(binding)}")
    fresh_unk = z3.Bool(f"pred_unk_{id(expr)}_{id(binding)}")
    return TriBool(is_unknown=fresh_unk, val=fresh_val)


def _eval_predicate(
    expr: Expr,
    binding: dict[str, SymbolicRow],
) -> z3.ExprRef:
    """Evaluate a boolean predicate. Returns z3 Bool: True iff predicate is TRUE under 3VL.

    This is the correct interface for WHERE, JOIN ON, HAVING, CASE WHEN
    contexts where UNKNOWN should filter out (behave like FALSE).
    """
    return _tb_true(_eval_predicate_3vl(expr, binding))


def _compare_3vl(
    op: BinOpKind,
    left: NullableVal,
    right: NullableVal,
) -> TriBool:
    """Compare two nullable values under SQL 3VL. Returns TriBool."""
    either_null = z3.Or(left.is_null, right.is_null)

    if op == BinOpKind.EQ:
        cmp = left.val == right.val
    elif op == BinOpKind.NEQ:
        cmp = left.val != right.val
    elif op == BinOpKind.LT:
        cmp = left.val < right.val
    elif op == BinOpKind.GT:
        cmp = left.val > right.val
    elif op == BinOpKind.LTE:
        cmp = left.val <= right.val
    elif op == BinOpKind.GTE:
        cmp = left.val >= right.val
    else:
        return _tb_from_bool(z3.BoolVal(False))

    return TriBool(is_unknown=either_null, val=cmp)


# ---------------------------------------------------------------------------
# Query result encoding
# ---------------------------------------------------------------------------

def _enumerate_combos(
    ir: QueryIR,
    k: int,
    db: Optional[SymbolicDB] = None,
    alias_to_table: Optional[dict[str, str]] = None,
) -> tuple[list[str], list[Combo]]:
    """Enumerate all row-index combos for a query's FROM/JOIN tables.

    Returns (list_of_aliases, list_of_combos).

    FIX.16b: When *db* is provided, uses actual table row counts instead
    of *k* for tables with fewer rows (e.g. ``__values_dual__`` has 1 row).
    """
    aliases: list[str] = []
    alias_name = ir.from_table.ref_name.lower()
    aliases.append(alias_name)
    for join in ir.joins:
        jalias = join.right.ref_name.lower()
        aliases.append(jalias)

    index_ranges: list[range] = []
    for alias in aliases:
        actual_k = k
        if db is not None and alias_to_table is not None:
            tbl_name = alias_to_table.get(alias, alias)
            sym_tbl = db.tables.get(tbl_name) or db.tables.get(alias)
            if sym_tbl is not None:
                actual_k = len(sym_tbl.rows)
        index_ranges.append(range(actual_k))

    combos: list[Combo] = []
    for indices in product(*index_ranges):
        combo = dict(zip(aliases, indices))
        combos.append(combo)

    return aliases, combos


# ---------------------------------------------------------------------------
# Window function encoding
# ---------------------------------------------------------------------------

_RANKING_FUNCS = {"ROW_NUMBER", "RANK", "DENSE_RANK"}
_AGG_WINDOW_MAP: dict[str, AggFunc] = {
    "SUM": AggFunc.SUM,
    "COUNT": AggFunc.COUNT,
    "MIN": AggFunc.MIN,
    "MAX": AggFunc.MAX,
    "AVG": AggFunc.AVG,
}
_VALUE_WINDOW_FUNCS = {"FIRST_VALUE", "LAST_VALUE", "NTH_VALUE"}


def _contains_window_func(expr) -> bool:
    """Recursively check if expr contains any WindowFunc node."""
    if expr is None:
        return False
    if isinstance(expr, WindowFunc):
        return True
    if isinstance(expr, BinOp):
        return _contains_window_func(expr.left) or _contains_window_func(expr.right)
    if isinstance(expr, UnaryOp):
        return _contains_window_func(expr.operand)
    if isinstance(expr, FuncCall):
        return any(_contains_window_func(a) for a in expr.args)
    if isinstance(expr, CaseExpr):
        for cw in expr.whens:
            if _contains_window_func(cw.when) or _contains_window_func(cw.then):
                return True
        return _contains_window_func(expr.else_)
    if isinstance(expr, AggCall):
        return _contains_window_func(expr.arg)
    if isinstance(expr, Between):
        return _contains_window_func(expr.expr) or _contains_window_func(expr.low) or _contains_window_func(expr.high)
    if isinstance(expr, InList):
        return _contains_window_func(expr.expr) or any(_contains_window_func(v) for v in expr.values)
    return False


def _collect_window_funcs(expr, out: list) -> None:
    """Recursively collect all WindowFunc nodes from an expression tree."""
    if expr is None:
        return
    if isinstance(expr, WindowFunc):
        out.append(expr)
        return
    if isinstance(expr, BinOp):
        _collect_window_funcs(expr.left, out)
        _collect_window_funcs(expr.right, out)
    elif isinstance(expr, UnaryOp):
        _collect_window_funcs(expr.operand, out)
    elif isinstance(expr, FuncCall):
        for a in expr.args:
            _collect_window_funcs(a, out)
    elif isinstance(expr, CaseExpr):
        for cw in expr.whens:
            _collect_window_funcs(cw.when, out)
            _collect_window_funcs(cw.then, out)
        _collect_window_funcs(expr.else_, out)
    elif isinstance(expr, AggCall):
        _collect_window_funcs(expr.arg, out)
    elif isinstance(expr, Between):
        _collect_window_funcs(expr.expr, out)
        _collect_window_funcs(expr.low, out)
        _collect_window_funcs(expr.high, out)
    elif isinstance(expr, InList):
        _collect_window_funcs(expr.expr, out)
        for v in expr.values:
            _collect_window_funcs(v, out)


def _has_window_functions(ir: QueryIR) -> bool:
    """Check if any select expression contains a WindowFunc (recursive)."""
    return any(_contains_window_func(e) for e in ir.select)


def _same_partition(
    part_keys_i: list[NullableVal],
    part_keys_j: list[NullableVal],
) -> z3.ExprRef:
    """True iff two rows belong to the same partition (NULL-safe equality)."""
    if not part_keys_i:
        return z3.BoolVal(True)
    conds = []
    for ki, kj in zip(part_keys_i, part_keys_j):
        both_null = z3.And(ki.is_null, kj.is_null)
        both_eq = z3.And(z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val == kj.val)
        conds.append(z3.Or(both_null, both_eq))
    return z3.And(conds)


def _is_strictly_better_window(
    keys_i: list[NullableVal],
    keys_j: list[NullableVal],
    order_specs: list[SortSpec],
) -> z3.ExprRef:
    """True if row i is strictly before row j under window ORDER BY.

    Lexicographic comparison with NULL-aware ordering (same logic as
    _is_strictly_better in _apply_order_limit).
    """
    conditions: list[z3.ExprRef] = []
    prefix_eq: list[z3.ExprRef] = []

    for k_idx, spec in enumerate(order_specs):
        if k_idx >= len(keys_i) or k_idx >= len(keys_j):
            break
        ki, kj = keys_i[k_idx], keys_j[k_idx]
        asc = spec.direction == SortDir.ASC

        if asc:
            i_better_null = z3.And(z3.Not(ki.is_null), kj.is_null)
            val_better = z3.And(
                z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val < kj.val
            )
            val_eq = z3.And(
                z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val == kj.val
            )
        else:
            i_better_null = z3.And(ki.is_null, z3.Not(kj.is_null))
            val_better = z3.And(
                z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val > kj.val
            )
            val_eq = z3.And(
                z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val == kj.val
            )

        both_null_eq = z3.And(ki.is_null, kj.is_null)
        key_eq = z3.Or(val_eq, both_null_eq)
        key_better = z3.Or(i_better_null, val_better)

        if prefix_eq:
            conditions.append(z3.And(z3.And(prefix_eq), key_better))
        else:
            conditions.append(key_better)
        prefix_eq.append(key_eq)

    if not conditions:
        return z3.BoolVal(False)
    return z3.Or(conditions)


def _order_keys_equal(
    keys_i: list[NullableVal],
    keys_j: list[NullableVal],
) -> z3.ExprRef:
    """True iff all ORDER BY keys are equal (NULL-safe)."""
    if not keys_i:
        return z3.BoolVal(True)
    conds = []
    for ki, kj in zip(keys_i, keys_j):
        both_null = z3.And(ki.is_null, kj.is_null)
        both_eq = z3.And(z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val == kj.val)
        conds.append(z3.Or(both_null, both_eq))
    return z3.And(conds)


def _has_nontrivial_frame(wf: WindowFunc) -> bool:
    """Check if window function has a non-default frame clause.

    Default frame for aggregate windows is the entire partition
    (ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING when
    no ORDER BY, or ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    when ORDER BY is present — but we treat no-frame as full-partition).
    """
    return wf.frame is not None and wf.frame.unit == "ROWS"


def _compute_row_positions(
    n: int,
    wf: WindowFunc,
    survives_list: list[z3.ExprRef],
    same_part: list[list[z3.ExprRef]],
    order_keys: list[list[NullableVal]],
) -> list[z3.ExprRef]:
    """Compute zero-based row position within partition for each combo.

    Uses the same total-order as ROW_NUMBER: strictly-better + tiebreaker.
    Returns a list of Z3 int expressions, one per combo.
    """
    tb = [z3.Int(f"wf_frame_tb_{id(wf)}_{i}") for i in range(n)]
    better_cache: dict[tuple[int, int], z3.ExprRef] = {}
    tied_cache: dict[tuple[int, int], z3.ExprRef] = {}

    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            better_cache[(i, j)] = _is_strictly_better_window(
                order_keys[i], order_keys[j], wf.order_by,
            )
    for i in range(n):
        for j in range(i + 1, n):
            tied = z3.Not(z3.Or(better_cache[(i, j)], better_cache[(j, i)]))
            tied_cache[(i, j)] = tied
            tied_cache[(j, i)] = tied

    positions: list[z3.ExprRef] = []
    for i in range(n):
        rank_terms = []
        for j in range(n):
            if i == j:
                continue
            total_better = z3.Or(
                better_cache[(j, i)],
                z3.And(tied_cache[(j, i)], tb[j] < tb[i]),
            )
            rank_terms.append(z3.If(
                z3.And(survives_list[j], same_part[i][j], total_better),
                1, 0,
            ))
        pos = z3.Sum(rank_terms) if rank_terms else z3.RealVal(0)
        positions.append(pos)

    return positions


def _build_frame_membership(
    n: int,
    wf: WindowFunc,
    survives_list: list[z3.ExprRef],
    same_part: list[list[z3.ExprRef]],
    order_keys: list[list[NullableVal]],
) -> list[list[z3.ExprRef]]:
    """Build frame membership matrix: in_frame[i][j] = True iff combo j
    is within combo i's window frame.

    Supports ROWS BETWEEN with:
    - UNBOUNDED PRECEDING / N PRECEDING / CURRENT ROW as start
    - CURRENT ROW / M FOLLOWING / UNBOUNDED FOLLOWING as end
    """
    from ..ir.types import WindowFrameBoundKind

    frame = wf.frame
    assert frame is not None and frame.unit == "ROWS"

    positions = _compute_row_positions(n, wf, survives_list, same_part, order_keys)

    # Decode frame bounds
    start = frame.start
    end = frame.end

    in_frame: list[list[z3.ExprRef]] = []
    for i in range(n):
        row = []
        for j in range(n):
            # Base: must be in same partition
            conds = [same_part[i][j]]

            # Lower bound
            if start.kind == WindowFrameBoundKind.UNBOUNDED_PRECEDING:
                pass  # no lower bound constraint
            elif start.kind == WindowFrameBoundKind.CURRENT_ROW:
                conds.append(positions[j] >= positions[i])
            elif start.kind == WindowFrameBoundKind.PRECEDING:
                offset = start.offset if start.offset is not None else 0
                conds.append(positions[j] >= positions[i] - offset)
            elif start.kind == WindowFrameBoundKind.FOLLOWING:
                offset = start.offset if start.offset is not None else 0
                conds.append(positions[j] >= positions[i] + offset)
            else:
                pass  # UNBOUNDED_FOLLOWING as start is unusual but valid

            # Upper bound
            if end is None or end.kind == WindowFrameBoundKind.CURRENT_ROW:
                conds.append(positions[j] <= positions[i])
            elif end.kind == WindowFrameBoundKind.UNBOUNDED_FOLLOWING:
                pass  # no upper bound constraint
            elif end.kind == WindowFrameBoundKind.FOLLOWING:
                offset = end.offset if end.offset is not None else 0
                conds.append(positions[j] <= positions[i] + offset)
            elif end.kind == WindowFrameBoundKind.PRECEDING:
                offset = end.offset if end.offset is not None else 0
                conds.append(positions[j] <= positions[i] - offset)
            elif end.kind == WindowFrameBoundKind.UNBOUNDED_PRECEDING:
                pass  # unusual

            row.append(z3.And(conds) if len(conds) > 1 else conds[0])
        in_frame.append(row)

    return in_frame


def _compute_frame_value_func(
    func_upper: str,
    val_args: list[NullableVal],
    positions: list[z3.ExprRef],
    i: int,
    n: int,
    survives_list: list[z3.ExprRef],
    membership: list[list[z3.ExprRef]],
    wf: WindowFunc,
) -> NullableVal:
    """Compute FIRST_VALUE / LAST_VALUE / NTH_VALUE for row i.

    Selects a specific row's value from within the frame based on position:
    - FIRST_VALUE: row with smallest position in frame
    - LAST_VALUE: row with largest position in frame
    - NTH_VALUE: row at position N-1 (1-indexed) in frame
    """
    # Collect frame members: (in_frame AND survives, position, value)
    candidates = []
    for j in range(n):
        in_frame_j = z3.And(survives_list[j], membership[i][j])
        candidates.append((in_frame_j, positions[j], val_args[j]))

    if not candidates:
        return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))

    if func_upper == "FIRST_VALUE":
        # Pick value of the frame member with the smallest position
        result_val = candidates[0][2].val
        result_null = candidates[0][2].is_null
        have = candidates[0][0]
        best_pos = z3.If(candidates[0][0], candidates[0][1], z3.RealVal(999))

        for j in range(1, len(candidates)):
            cond_j, pos_j, val_j = candidates[j]
            # Update if this is the first valid or has a smaller position
            is_better = z3.And(cond_j, z3.Or(z3.Not(have), pos_j < best_pos))
            result_val = z3.If(is_better, val_j.val, result_val)
            result_null = z3.If(is_better, val_j.is_null, result_null)
            best_pos = z3.If(is_better, pos_j, best_pos)
            have = z3.Or(have, cond_j)

        any_valid = z3.Or([c for c, _, _ in candidates])
        return NullableVal(is_null=z3.If(any_valid, result_null, z3.BoolVal(True)), val=result_val)

    elif func_upper == "LAST_VALUE":
        # Pick value of the frame member with the largest position
        result_val = candidates[0][2].val
        result_null = candidates[0][2].is_null
        have = candidates[0][0]
        best_pos = z3.If(candidates[0][0], candidates[0][1], z3.RealVal(-999))

        for j in range(1, len(candidates)):
            cond_j, pos_j, val_j = candidates[j]
            is_better = z3.And(cond_j, z3.Or(z3.Not(have), pos_j > best_pos))
            result_val = z3.If(is_better, val_j.val, result_val)
            result_null = z3.If(is_better, val_j.is_null, result_null)
            best_pos = z3.If(is_better, pos_j, best_pos)
            have = z3.Or(have, cond_j)

        any_valid = z3.Or([c for c, _, _ in candidates])
        return NullableVal(is_null=z3.If(any_valid, result_null, z3.BoolVal(True)), val=result_val)

    elif func_upper == "NTH_VALUE":
        # NTH_VALUE(expr, N): pick value at Nth position (1-indexed) in frame
        nth = 1  # default
        if len(wf.args) >= 2:
            nth_arg = wf.args[1]
            if isinstance(nth_arg, Literal) and isinstance(nth_arg.value, int):
                nth = nth_arg.value
        target_pos_in_frame = nth - 1  # convert to 0-indexed

        # Count frame position of each candidate within the frame
        # (position relative to frame start, not partition)
        # We need to find the candidate whose rank within the frame equals target_pos_in_frame
        for j in range(n):
            cond_j, pos_j, val_j = candidates[j]
            # Count how many frame members have a smaller position
            rank_in_frame = []
            for j2 in range(n):
                cond_j2, pos_j2, _ = candidates[j2]
                rank_in_frame.append(z3.If(
                    z3.And(cond_j2, pos_j2 < pos_j),
                    1, 0,
                ))
            frame_rank = z3.Sum(rank_in_frame) if rank_in_frame else z3.RealVal(0)
            candidates[j] = (cond_j, frame_rank, val_j)

        # Pick the candidate whose frame_rank == target_pos_in_frame
        result_val = z3.RealVal(0)
        result_null = z3.BoolVal(True)
        for cond_j, frame_rank_j, val_j in candidates:
            is_target = z3.And(cond_j, frame_rank_j == target_pos_in_frame)
            result_val = z3.If(is_target, val_j.val, result_val)
            result_null = z3.If(is_target, val_j.is_null, result_null)

        return NullableVal(is_null=result_null, val=result_val)

    # Fallback
    return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))


def _precompute_window_values(
    ir: QueryIR,
    all_combos_with_bindings: list[tuple[z3.ExprRef, dict[str, SymbolicRow]]],
) -> None:
    """Pre-compute exact window function values for all (expr, binding) pairs.

    Populates the module-level _precomputed_windows dict.

    Supported window functions:
    - ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...):
        rank within partition, ties broken by existential variable.
    - RANK() OVER (PARTITION BY ... ORDER BY ...):
        1 + count of partition peers strictly better.
    - DENSE_RANK() OVER (PARTITION BY ... ORDER BY ...):
        1 + count of distinct better key-tuples in partition.
    - SUM/COUNT/MIN/MAX/AVG(...) OVER (PARTITION BY ... [frame]):
        aggregate over surviving rows in frame (or full partition).
    - FIRST_VALUE/LAST_VALUE/NTH_VALUE(...) OVER (... [frame]):
        positional value selection within frame.
    """
    global _precomputed_windows

    window_exprs: list[tuple[int, WindowFunc]] = []
    for sel_idx, expr in enumerate(ir.select):
        collected: list = []
        _collect_window_funcs(expr, collected)
        for wf in collected:
            window_exprs.append((sel_idx, wf))
    if not window_exprs:
        return

    n = len(all_combos_with_bindings)

    # Pre-compute survival (join + WHERE) for each combo
    survives_list: list[z3.ExprRef] = []
    bindings: list[dict[str, SymbolicRow]] = []
    for join_surv, binding in all_combos_with_bindings:
        where_ok = _eval_where(ir, binding)
        survives_list.append(z3.And(join_surv, where_ok))
        bindings.append(binding)

    for _sel_idx, wf in window_exprs:
        assert isinstance(wf, WindowFunc)
        func_upper = wf.func_name.upper()

        # Evaluate partition keys for each combo
        part_keys: list[list[NullableVal]] = []
        for binding in bindings:
            part_keys.append([_eval_value(p, binding) for p in wf.partition_by])

        # same_part[i][j]: whether combos i and j are in the same partition
        same_part: list[list[z3.ExprRef]] = []
        for i in range(n):
            row = []
            for j in range(n):
                row.append(_same_partition(part_keys[i], part_keys[j]))
            same_part.append(row)

        # Evaluate ORDER BY keys for each combo (shared across ranking,
        # aggregate-with-frame, and value window functions)
        order_keys: list[list[NullableVal]] = []
        if wf.order_by:
            for binding in bindings:
                order_keys.append([_eval_value(s.expr, binding) for s in wf.order_by])

        if func_upper in _RANKING_FUNCS:
            if func_upper == "ROW_NUMBER":
                # ROW_NUMBER = rank with ties broken by existential tiebreaker
                tb = [z3.Int(f"wf_tb_{id(wf)}_{i}") for i in range(n)]

                # Pre-compute strictly-better and tied caches
                better_cache: dict[tuple[int, int], z3.ExprRef] = {}
                tied_cache: dict[tuple[int, int], z3.ExprRef] = {}
                for i in range(n):
                    for j in range(n):
                        if i == j:
                            continue
                        better_cache[(i, j)] = _is_strictly_better_window(
                            order_keys[i], order_keys[j], wf.order_by,
                        )
                for i in range(n):
                    for j in range(i + 1, n):
                        tied = z3.Not(z3.Or(
                            better_cache[(i, j)], better_cache[(j, i)],
                        ))
                        tied_cache[(i, j)] = tied
                        tied_cache[(j, i)] = tied

                for i in range(n):
                    rank_terms = []
                    for j in range(n):
                        if i == j:
                            continue
                        # j is before i in total order within the partition
                        total_better = z3.Or(
                            better_cache[(j, i)],
                            z3.And(tied_cache[(j, i)], tb[j] < tb[i]),
                        )
                        rank_terms.append(z3.If(
                            z3.And(survives_list[j], same_part[i][j], total_better),
                            z3.RealVal(1), z3.RealVal(0),
                        ))
                    rank_val = z3.Sum(rank_terms) + z3.RealVal(1) if rank_terms else z3.RealVal(1)
                    _precomputed_windows[(id(wf), id(bindings[i]))] = NullableVal(
                        is_null=z3.BoolVal(False), val=rank_val,
                    )

            elif func_upper == "RANK":
                # RANK = 1 + count of partition peers strictly better
                for i in range(n):
                    rank_terms = []
                    for j in range(n):
                        if i == j:
                            continue
                        strictly_better = _is_strictly_better_window(
                            order_keys[j], order_keys[i], wf.order_by,
                        )
                        rank_terms.append(z3.If(
                            z3.And(survives_list[j], same_part[i][j], strictly_better),
                            z3.RealVal(1), z3.RealVal(0),
                        ))
                    rank_val = z3.Sum(rank_terms) + z3.RealVal(1) if rank_terms else z3.RealVal(1)
                    _precomputed_windows[(id(wf), id(bindings[i]))] = NullableVal(
                        is_null=z3.BoolVal(False), val=rank_val,
                    )

            elif func_upper == "DENSE_RANK":
                # DENSE_RANK = 1 + count of distinct better key-tuples
                # For each j strictly better than i, count it only if
                # it's the first occurrence of its key-tuple in the partition.
                for i in range(n):
                    rank_terms = []
                    for j in range(n):
                        if i == j:
                            continue
                        strictly_better = _is_strictly_better_window(
                            order_keys[j], order_keys[i], wf.order_by,
                        )
                        in_part_j = z3.And(
                            survives_list[j], same_part[i][j], strictly_better,
                        )
                        # Is j the first occurrence of its key-tuple among
                        # partition peers that are strictly better than i?
                        earlier_same_keys = []
                        for j2 in range(j):
                            if j2 == i:
                                continue
                            in_part_j2 = z3.And(
                                survives_list[j2], same_part[i][j2],
                                _is_strictly_better_window(
                                    order_keys[j2], order_keys[i], wf.order_by,
                                ),
                            )
                            keys_eq = _order_keys_equal(order_keys[j], order_keys[j2])
                            earlier_same_keys.append(z3.And(in_part_j2, keys_eq))
                        is_first = z3.And(
                            in_part_j,
                            z3.Not(z3.Or(earlier_same_keys)) if earlier_same_keys else z3.BoolVal(True),
                        )
                        rank_terms.append(z3.If(is_first, z3.RealVal(1), z3.RealVal(0)))
                    rank_val = z3.Sum(rank_terms) + z3.RealVal(1) if rank_terms else z3.RealVal(1)
                    _precomputed_windows[(id(wf), id(bindings[i]))] = NullableVal(
                        is_null=z3.BoolVal(False), val=rank_val,
                    )

        elif func_upper in _AGG_WINDOW_MAP:
            # Aggregate window function: SUM/COUNT/MIN/MAX/AVG OVER PARTITION BY
            agg_func = _AGG_WINDOW_MAP[func_upper]

            # Evaluate aggregate argument for each combo
            agg_vals: list[NullableVal] = []
            for binding in bindings:
                if wf.args:
                    agg_vals.append(_eval_value(wf.args[0], binding))
                else:
                    # COUNT(*) OVER (...)
                    agg_vals.append(NullableVal(
                        is_null=z3.BoolVal(False), val=z3.RealVal(1),
                    ))

            # If a ROWS frame is specified, build frame membership matrix
            # and use it instead of same_part for aggregation scope.
            if _has_nontrivial_frame(wf) and wf.order_by:
                frame_matrix = _build_frame_membership(
                    n, wf, survives_list, same_part, order_keys,
                )
                for i in range(n):
                    agg_result = _compute_aggregate(
                        agg_func, wf.distinct,
                        agg_vals, i, n,
                        survives_list, frame_matrix,
                    )
                    _precomputed_windows[(id(wf), id(bindings[i]))] = agg_result
            else:
                for i in range(n):
                    agg_result = _compute_aggregate(
                        agg_func, wf.distinct,
                        agg_vals, i, n,
                        survives_list, same_part,
                    )
                    _precomputed_windows[(id(wf), id(bindings[i]))] = agg_result

        elif func_upper in _VALUE_WINDOW_FUNCS:
            # FIRST_VALUE / LAST_VALUE / NTH_VALUE with frame support
            if not wf.args:
                for i in range(n):
                    _precomputed_windows[(id(wf), id(bindings[i]))] = NullableVal(
                        is_null=z3.BoolVal(True), val=z3.RealVal(0),
                    )
            else:
                # Evaluate the expression argument for each combo
                val_args: list[NullableVal] = []
                for binding in bindings:
                    val_args.append(_eval_value(wf.args[0], binding))

                # Build frame or partition membership
                if _has_nontrivial_frame(wf) and wf.order_by:
                    membership = _build_frame_membership(
                        n, wf, survives_list, same_part, order_keys,
                    )
                else:
                    membership = same_part

                positions = _compute_row_positions(
                    n, wf, survives_list, same_part, order_keys,
                ) if wf.order_by else [z3.RealVal(i) for i in range(n)]

                for i in range(n):
                    result = _compute_frame_value_func(
                        func_upper, val_args, positions,
                        i, n, survives_list, membership, wf,
                    )
                    _precomputed_windows[(id(wf), id(bindings[i]))] = result

        elif func_upper in ("LAG", "LEAD"):
            # FIX.28e: LAG/LEAD — value-access by offset in window ordering.
            # LAG(expr, offset, default): value at position (current - offset).
            # LEAD(expr, offset, default): value at position (current + offset).
            if not wf.args:
                for i in range(n):
                    _precomputed_windows[(id(wf), id(bindings[i]))] = NullableVal(
                        is_null=z3.BoolVal(True), val=z3.RealVal(0),
                    )
            else:
                # Evaluate the expression argument for each combo
                lag_vals: list[NullableVal] = []
                for binding in bindings:
                    lag_vals.append(_eval_value(wf.args[0], binding))

                # Offset (default 1)
                offset = 1
                if len(wf.args) >= 2 and isinstance(wf.args[1], Literal) and isinstance(wf.args[1].value, (int, float)):
                    offset = int(wf.args[1].value)

                # Default value (default NULL)
                has_default = len(wf.args) >= 3
                default_val: NullableVal | None = None
                if has_default:
                    for binding in bindings[:1]:
                        default_val = _eval_value(wf.args[2], binding)

                # Compute positions within partitions
                positions = _compute_row_positions(
                    n, wf, survives_list, same_part, order_keys,
                ) if wf.order_by else [z3.RealVal(i) for i in range(n)]

                for i in range(n):
                    # Target position: LAG → pos_i - offset, LEAD → pos_i + offset
                    if func_upper == "LAG":
                        target_pos = positions[i] - z3.IntVal(offset)
                    else:
                        target_pos = positions[i] + z3.IntVal(offset)

                    # Find the combo j in the same partition at target_pos
                    # Build if-then-else chain: first matching j wins
                    result_val = default_val.val if default_val else z3.RealVal(0)
                    result_null = default_val.is_null if default_val else z3.BoolVal(True)

                    for j in range(n):
                        j_matches = z3.And(
                            survives_list[j], same_part[i][j],
                            positions[j] == target_pos,
                        )
                        result_val = z3.If(j_matches, lag_vals[j].val, result_val)
                        result_null = z3.If(j_matches, lag_vals[j].is_null, result_null)

                    _precomputed_windows[(id(wf), id(bindings[i]))] = NullableVal(
                        is_null=result_null, val=result_val,
                    )

        else:
            # Unsupported window function — use fresh unconstrained variable
            for i in range(n):
                fresh = z3.Real(f"wf_fallback_{func_upper}_{id(wf)}_{i}")
                _precomputed_windows[(id(wf), id(bindings[i]))] = NullableVal(
                    is_null=z3.BoolVal(False), val=fresh,
                )


def _encode_query_result(
    ir: QueryIR,
    db: SymbolicDB,
    scope: BoundedScope,
) -> list[ResultRow]:
    """Encode a query's result set as a list of ResultRows.

    Handles INNER, LEFT, RIGHT, and FULL joins with proper NULL-padding
    for outer join semantics. ORDER BY + LIMIT is modeled via rank-based
    top-k selection (see ``_apply_order_limit``). ORDER BY alone (without
    LIMIT) is not modeled as it is cosmetic for set equality.

    Does not handle set operations (UNION/INTERSECT/EXCEPT); callers must guard.
    """
    # FIX.16a: LIMIT 0 / FETCH NEXT 0 ROWS → empty result set
    if ir.limit is not None and ir.limit == 0:
        return []

    alias_to_table = _get_tables_for_query(ir)
    aliases, combos = _enumerate_combos(ir, scope.k_rows, db, alias_to_table)
    # FIX.15a: GROUP BY without aggregate functions (e.g. SELECT x FROM t
    # GROUP BY x) is semantically equivalent to SELECT DISTINCT — the
    # aggregation path must handle it so group-key deduplication is applied.
    is_agg = ir.has_aggregation() or bool(ir.group_by)

    # Build all result combos including outer-join unmatched rows
    all_combos_with_bindings = _build_combos_with_outer_joins(
        ir, db, alias_to_table, aliases, combos, scope,
    )

    # Pre-compute window function values before _eval_value runs
    if _has_window_functions(ir):
        _precompute_window_values(ir, all_combos_with_bindings)

    result_rows: list[ResultRow] = []

    if not is_agg:
        for survives, binding in all_combos_with_bindings:
            # Apply WHERE on top of join survival
            where_ok = _eval_where(ir, binding)
            final = z3.And(survives, where_ok)
            values = [_eval_value(expr, binding) for expr in ir.select]
            result_rows.append(ResultRow(survives=final, values=values))
    else:
        # For aggregation, pass pre-computed combos
        combo_data = [(s, b) for s, b in all_combos_with_bindings]
        result_rows = _encode_aggregated_result_v2(ir, combo_data, scope)

    # ORDER BY + LIMIT k: rank-based top-k selection.
    # Pick the k rows with the best ORDER BY keys among surviving rows.
    # Other rows get survives=False, so the result is at most k rows.
    if ir.order_by and ir.limit and ir.limit > 0 and result_rows:
        result_rows = _apply_order_limit(ir, result_rows, all_combos_with_bindings)

    return result_rows


# ---------------------------------------------------------------------------
# Set operation encoding (UNION ALL / UNION / INTERSECT / EXCEPT)
# ---------------------------------------------------------------------------

def _rows_equal(r1_values: list[NullableVal], r2_values: list[NullableVal]) -> z3.ExprRef:
    """Z3 constraint: two result rows have identical projected values."""
    if len(r1_values) != len(r2_values):
        return z3.BoolVal(False)
    eqs = []
    for v1, v2 in zip(r1_values, r2_values):
        both_null = z3.And(v1.is_null, v2.is_null)
        both_non_null_eq = z3.And(z3.Not(v1.is_null), z3.Not(v2.is_null), v1.val == v2.val)
        eqs.append(z3.Or(both_null, both_non_null_eq))
    return z3.And(eqs) if eqs else z3.BoolVal(True)


def _combine_setop_results(
    left_rows: list[ResultRow],
    right_rows: list[ResultRow],
    set_op: SetOpKind,
) -> list[ResultRow]:
    """Combine two result-row lists according to set-op semantics.

    Uses multiplicity-counting (like ``_encode_difference``) so the result
    is fully deterministic — no free boolean pairing variables.

    - UNION_ALL: concatenate (bag union).
    - UNION: concatenate then deduplicate (keep first occurrence only).
    - INTERSECT: for each left row, it survives iff the count of equal
      surviving rows on the left that precede it (inclusive) is ≤ the
      total count of matching rows on the right.  This gives
      min-multiplicity semantics.
    - EXCEPT: for each left row, it survives iff the count of equal
      surviving rows on the left that precede it (inclusive) is >
      the total count of matching rows on the right.
    """
    if set_op == SetOpKind.UNION_ALL:
        return left_rows + right_rows

    all_rows = left_rows + right_rows

    if set_op == SetOpKind.UNION:
        # Deduplicate: a row survives only if no earlier surviving row
        # has the same tuple values.
        result: list[ResultRow] = []
        for i, row in enumerate(all_rows):
            if i == 0:
                result.append(row)
                continue
            earlier_match = z3.Or([
                z3.And(result[j].survives, _rows_equal(result[j].values, row.values))
                for j in range(len(result))
            ])
            new_survives = z3.And(row.survives, z3.Not(earlier_match))
            result.append(ResultRow(survives=new_survives, values=row.values))
        return result

    if set_op == SetOpKind.INTERSECT:
        # Multiplicity-based: for left row i, compute:
        #   rank_i = count of surviving left rows with same values
        #            at positions ≤ i (1-based rank of this duplicate)
        #   right_count = count of surviving right rows with same values
        # Row i survives iff left_rows[i].survives AND rank_i ≤ right_count
        result = []
        for i, lrow in enumerate(left_rows):
            rank_i = z3.Sum([
                z3.If(z3.And(left_rows[j].survives,
                             _rows_equal(left_rows[j].values, lrow.values)),
                      1, 0)
                for j in range(i + 1)
            ])
            right_count = z3.Sum([
                z3.If(z3.And(rrow.survives,
                             _rows_equal(rrow.values, lrow.values)),
                      1, 0)
                for rrow in right_rows
            ]) if right_rows else z3.RealVal(0)
            new_survives = z3.And(lrow.survives, rank_i <= right_count)
            result.append(ResultRow(survives=new_survives, values=lrow.values))
        return result

    if set_op == SetOpKind.EXCEPT:
        # Multiplicity-based: for left row i, compute:
        #   rank_i = 1-based rank among surviving left duplicates (≤ i)
        #   right_count = count of surviving right rows with same values
        # Row i survives iff left_rows[i].survives AND rank_i > right_count
        result = []
        for i, lrow in enumerate(left_rows):
            rank_i = z3.Sum([
                z3.If(z3.And(left_rows[j].survives,
                             _rows_equal(left_rows[j].values, lrow.values)),
                      1, 0)
                for j in range(i + 1)
            ])
            right_count = z3.Sum([
                z3.If(z3.And(rrow.survives,
                             _rows_equal(rrow.values, lrow.values)),
                      1, 0)
                for rrow in right_rows
            ]) if right_rows else z3.RealVal(0)
            new_survives = z3.And(lrow.survives, rank_i > right_count)
            result.append(ResultRow(survives=new_survives, values=lrow.values))
        return result

    # Fallback: unknown set_op, treat as UNION ALL
    return left_rows + right_rows


def _encode_query_with_setops(
    ir: QueryIR,
    db: SymbolicDB,
    scope: BoundedScope,
) -> list[ResultRow]:
    """Encode a query that may have set operations.

    If the query has no set_op, delegates to _encode_query_result.
    Otherwise, encodes left and right branches and combines them.
    """
    if ir.set_op is None:
        return _encode_query_result(ir, db, scope)

    # Encode left side: strip set_op/set_right to get just the SELECT
    left_ir = ir.model_copy(deep=True)
    left_ir.set_op = None
    left_ir.set_right = None
    left_result = _encode_query_result(left_ir, db, scope)

    # Encode right side (may itself have set_ops — recurse)
    right_result = _encode_query_with_setops(ir.set_right, db, scope)

    return _combine_setop_results(left_result, right_result, ir.set_op)


def _eval_where(ir: QueryIR, binding: dict[str, SymbolicRow]) -> z3.ExprRef:
    """Evaluate WHERE clause only."""
    if ir.where:
        return _eval_predicate(ir.where, binding)
    return z3.BoolVal(True)


def _binding_rows_present(binding: dict[str, SymbolicRow]) -> z3.ExprRef:
    """True iff every distinct row in the binding is present.

    Derived-table rows carry ``present = inner_survives``; base-table
    rows are always ``present = True``.  Deduplicates by object identity
    so self-join aliases sharing the same row are counted once.
    """
    seen: set[int] = set()
    terms: list[z3.ExprRef] = []
    for row in binding.values():
        rid = id(row)
        if rid in seen:
            continue
        seen.add(rid)
        terms.append(row.present)
    return z3.And(terms) if terms else z3.BoolVal(True)


def _build_combos_with_outer_joins(
    ir: QueryIR,
    db: SymbolicDB,
    alias_to_table: dict[str, str],
    aliases: list[str],
    combos: list[Combo],
    scope: BoundedScope,
) -> list[tuple[z3.ExprRef, dict[str, SymbolicRow]]]:
    """Build all combo bindings including outer-join unmatched rows.

    For INNER joins: standard cartesian product filtering.
    For LEFT joins: adds unmatched left rows with NULLs on right.
    For RIGHT joins: adds unmatched right rows with NULLs on left.
    For FULL joins: adds both.

    FIX.19a: Correct multi-LEFT-JOIN chains.  Previously, matched combos
    were emitted once per join (duplicating them), causing spurious SAT
    witnesses for queries like ``A LEFT JOIN B ON .. LEFT JOIN C ON ..``.
    Now uses left-to-right recursive evaluation: step 1 evaluates
    ``A LEFT JOIN B``, step 2 LEFT JOINs each step-1 result with C.

    Returns list of (survives_bool, binding).
    """
    has_outer = any(
        j.join_type in (JoinType.LEFT, JoinType.RIGHT, JoinType.FULL)
        for j in ir.joins
    )

    if not has_outer or not ir.joins:
        # Simple case: all INNER joins (or no joins)
        results = []
        for combo in combos:
            binding = _make_binding(combo, alias_to_table, db)
            present = _binding_rows_present(binding)
            survives = z3.And(present, _combo_survives_join_only(ir, binding))
            results.append((survives, binding))
        return results

    from_alias = aliases[0]
    from_table = alias_to_table.get(from_alias, from_alias)
    k = scope.k_rows

    # Start with FROM-table rows as the initial "left side"
    sym_from = db.tables.get(from_table)
    from_k = len(sym_from.rows) if sym_from else k
    current: list[tuple[z3.ExprRef, dict[str, SymbolicRow]]] = []
    for i in range(from_k):
        binding = _make_binding({from_alias: i}, alias_to_table, db)
        present = _binding_rows_present(binding)
        current.append((present, binding))

    def _add_right_to_binding(
        base_binding: dict[str, SymbolicRow],
        right_alias: str,
        right_row: SymbolicRow,
    ) -> dict[str, SymbolicRow]:
        """Extend binding with a right-side row, safe for self-joins."""
        new_binding = dict(base_binding)
        new_binding[right_alias] = right_row
        return new_binding

    # Process each join left-to-right
    for join_idx, join in enumerate(ir.joins):
        right_alias = aliases[join_idx + 1]
        right_table = alias_to_table.get(right_alias, right_alias)
        sym_right = db.tables.get(right_table)
        right_k = len(sym_right.rows) if sym_right else k

        next_results: list[tuple[z3.ExprRef, dict[str, SymbolicRow]]] = []

        if join.join_type == JoinType.INNER:
            for surv, binding in current:
                for rj in range(right_k):
                    right_row = sym_right.rows[rj] if sym_right else SymbolicRow(cols={})
                    new_binding = _add_right_to_binding(binding, right_alias, right_row)
                    right_present = right_row.present
                    join_ok = _eval_predicate(join.on, new_binding)
                    next_results.append((z3.And(surv, right_present, join_ok), new_binding))

        elif join.join_type in (JoinType.LEFT, JoinType.FULL):
            for surv, binding in current:
                # Matched combos: left row × each right row where ON holds
                any_right_match_terms = []
                for rj in range(right_k):
                    right_row = sym_right.rows[rj] if sym_right else SymbolicRow(cols={})
                    new_binding = _add_right_to_binding(binding, right_alias, right_row)
                    right_present = right_row.present
                    join_ok = _eval_predicate(join.on, new_binding)
                    match_cond = z3.And(right_present, join_ok)
                    any_right_match_terms.append(match_cond)
                    next_results.append((z3.And(surv, match_cond), new_binding))

                # Unmatched left: NULL-pad right side
                if sym_right:
                    null_right_row = _make_null_row(sym_right)
                    unmatched_binding = _add_right_to_binding(binding, right_alias, null_right_row)
                    no_match = z3.Not(z3.Or(any_right_match_terms)) if any_right_match_terms else z3.BoolVal(True)
                    next_results.append((z3.And(surv, no_match), unmatched_binding))

            # FULL JOIN: also add unmatched right rows
            if join.join_type == JoinType.FULL and sym_right:
                for rj in range(right_k):
                    right_row = sym_right.rows[rj]
                    right_present = right_row.present
                    any_left_match = []
                    for surv, binding in current:
                        test_binding = _add_right_to_binding(binding, right_alias, right_row)
                        join_ok = _eval_predicate(join.on, test_binding)
                        any_left_match.append(z3.And(surv, join_ok))
                    no_left_match = z3.Not(z3.Or(any_left_match)) if any_left_match else z3.BoolVal(True)
                    null_left_binding: dict[str, SymbolicRow] = {}
                    for prev_alias in aliases[:join_idx + 1]:
                        prev_table = alias_to_table.get(prev_alias, prev_alias)
                        prev_sym = db.tables.get(prev_table)
                        if prev_sym:
                            null_left_binding[prev_alias] = _make_null_row(prev_sym)
                    null_left_binding[right_alias] = right_row
                    next_results.append((z3.And(right_present, no_left_match), null_left_binding))

        elif join.join_type == JoinType.RIGHT:
            for rj in range(right_k):
                right_row = sym_right.rows[rj] if sym_right else SymbolicRow(cols={})
                right_present = right_row.present
                any_left_match_terms = []
                for surv, binding in current:
                    new_binding = _add_right_to_binding(binding, right_alias, right_row)
                    join_ok = _eval_predicate(join.on, new_binding)
                    match_cond = z3.And(surv, join_ok)
                    any_left_match_terms.append(match_cond)
                    next_results.append((z3.And(surv, right_present, join_ok), new_binding))
                no_left_match = z3.Not(z3.Or(any_left_match_terms)) if any_left_match_terms else z3.BoolVal(True)
                null_left_binding: dict[str, SymbolicRow] = {}
                for prev_alias in aliases[:join_idx + 1]:
                    prev_table = alias_to_table.get(prev_alias, prev_alias)
                    prev_sym = db.tables.get(prev_table)
                    if prev_sym:
                        null_left_binding[prev_alias] = _make_null_row(prev_sym)
                null_left_binding[right_alias] = right_row
                next_results.append((z3.And(right_present, no_left_match), null_left_binding))

        elif join.join_type == JoinType.CROSS:
            for surv, binding in current:
                for rj in range(right_k):
                    right_row = sym_right.rows[rj] if sym_right else SymbolicRow(cols={})
                    new_binding = _add_right_to_binding(binding, right_alias, right_row)
                    right_present = right_row.present
                    next_results.append((z3.And(surv, right_present), new_binding))

        current = next_results

    return current if current else [(z3.BoolVal(True), _make_binding(combo, alias_to_table, db)) for combo in combos[:1]]


def _make_null_row(sym_table: SymbolicTable) -> SymbolicRow:
    """Create a row where all columns are NULL (for outer join padding)."""
    null_cols: dict[str, NullableVal] = {}
    for col_name in sym_table.col_types:
        null_cols[col_name] = NullableVal(
            is_null=z3.BoolVal(True),
            val=z3.RealVal(0),
        )
    return SymbolicRow(cols=null_cols)


def _combo_survives_join_only(ir: QueryIR, binding: dict[str, SymbolicRow]) -> z3.ExprRef:
    """Compute whether a combo survives join ON conditions only (no WHERE)."""
    conditions: list[z3.ExprRef] = []
    for join in ir.joins:
        conditions.append(_eval_predicate(join.on, binding))
    if not conditions:
        return z3.BoolVal(True)
    return z3.And(conditions)


def _encode_aggregated_result(
    ir: QueryIR,
    db: SymbolicDB,
    alias_to_table: dict[str, str],
    combos: list[Combo],
    scope: BoundedScope,
) -> list[ResultRow]:
    """Encode an aggregated query result.

    For each combo i, compute:
    - Whether it survives filtering
    - Its group key values (from GROUP BY expressions)
    - Whether it's the "representative" of its group (first survivor)
    - Aggregate values computed over all combos in the same group
    """
    n = len(combos)

    # Pre-compute survival and bindings
    bindings: list[dict[str, SymbolicRow]] = []
    survives_list: list[z3.ExprRef] = []
    for combo in combos:
        binding = _make_binding(combo, alias_to_table, db)
        bindings.append(binding)
        survives_list.append(_combo_survives_join_only(ir, binding))

    # Pre-compute group key values for each combo
    group_keys: list[list[NullableVal]] = []
    for binding in bindings:
        keys = [_eval_value(g, binding) for g in ir.group_by]
        group_keys.append(keys)

    # Pre-compute same_group[i][j]: whether combos i and j share group keys
    same_group: list[list[z3.ExprRef]] = []
    for i in range(n):
        row: list[z3.ExprRef] = []
        for j in range(n):
            if not ir.group_by:
                # No GROUP BY but has aggregation → everything in one group
                row.append(z3.BoolVal(True))
            else:
                conditions = []
                for ki, kj in zip(group_keys[i], group_keys[j]):
                    # Same group if both non-null and equal, or both null
                    both_null = z3.And(ki.is_null, kj.is_null)
                    both_eq = z3.And(z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val == kj.val)
                    conditions.append(z3.Or(both_null, both_eq))
                row.append(z3.And(conditions) if conditions else z3.BoolVal(True))
        same_group.append(row)

    # FIX.13e: Collect ALL aggregate calls from SELECT and HAVING (not just top-level)
    all_agg_calls: list[tuple[int, AggCall]] = []
    for expr in ir.select:
        _collect_agg_calls(expr, all_agg_calls)
    if ir.having:
        _collect_agg_calls(ir.having, all_agg_calls)

    # Pre-compute aggregate argument values for all collected AggCalls
    agg_cache: dict[int, list[NullableVal]] = {}
    seen_agg_ids: set[int] = set()
    for agg_id, agg_expr in all_agg_calls:
        if agg_id in seen_agg_ids:
            continue
        seen_agg_ids.add(agg_id)
        vals = []
        for binding in bindings:
            if agg_expr.arg is not None:
                vals.append(_eval_value(agg_expr.arg, binding))
            else:
                vals.append(NullableVal(is_null=z3.BoolVal(False), val=z3.RealVal(1)))
        agg_cache[agg_id] = vals

    # Build result rows: one per combo, but only "representatives" produce output
    result_rows: list[ResultRow] = []
    for i in range(n):
        # Is combo i a group representative? (first survivor in its group)
        earlier_same = []
        for j in range(i):
            earlier_same.append(z3.And(survives_list[j], same_group[i][j]))
        is_rep = z3.And(survives_list[i], z3.Not(z3.Or(earlier_same)) if earlier_same else z3.BoolVal(True))

        # FIX.13e: Use _eval_group_value for ALL select expressions
        values: list[NullableVal] = []
        for sel_idx, expr in enumerate(ir.select):
            values.append(_eval_group_value(
                expr, bindings[i], agg_cache,
                i, n, survives_list, same_group,
            ))

        # Apply HAVING (FIX.13c+13e: proper aggregate resolution)
        having_ok = z3.BoolVal(True)
        if ir.having:
            having_ok = _eval_having(
                ir.having, bindings[i],
                all_agg_calls, agg_cache,
                i, n, survives_list, same_group,
            )

        final_survives = z3.And(is_rep, having_ok)
        result_rows.append(ResultRow(survives=final_survives, values=values))

    return result_rows


def _collect_agg_calls(
    expr: Expr,
    result: list[tuple[int, AggCall]],
) -> None:
    """Collect all AggCall nodes from an expression tree.

    Assigns each AggCall a unique index (via id) for cache lookup.
    Used for SELECT expressions, HAVING, and ORDER BY in aggregate queries.
    """
    if isinstance(expr, AggCall):
        result.append((id(expr), expr))
    elif isinstance(expr, BinOp):
        _collect_agg_calls(expr.left, result)
        _collect_agg_calls(expr.right, result)
    elif isinstance(expr, UnaryOp):
        _collect_agg_calls(expr.operand, result)
    elif isinstance(expr, FuncCall):
        # FIX.18e: Also collect BOOL_AND/BOOL_OR as pseudo-aggregates
        fname = expr.func_name.upper()
        if fname in ("LOGICAL_AND", "BOOL_AND", "LOGICAL_OR", "BOOL_OR") and expr.args:
            # Create a synthetic AggCall for the agg_cache
            synth_func = AggFunc.MIN if fname in ("LOGICAL_AND", "BOOL_AND") else AggFunc.MAX
            synth = AggCall(func=synth_func, arg=expr.args[0], distinct=False)
            # Store with id(expr) so _eval_group_value can look it up
            result.append((id(expr), synth))
        else:
            for arg in expr.args:
                _collect_agg_calls(arg, result)
    elif isinstance(expr, CaseExpr):
        for cw in expr.whens:
            _collect_agg_calls(cw.when, result)
            _collect_agg_calls(cw.then, result)
        if expr.else_ is not None:
            _collect_agg_calls(expr.else_, result)


# Keep the old name as an alias for backward compatibility
_collect_having_aggs = _collect_agg_calls


def _eval_group_value(
    expr: Expr,
    binding: dict[str, SymbolicRow],
    agg_cache: dict[int, list[NullableVal]],
    group_rep: int,
    n: int,
    survives: list[z3.ExprRef],
    same_group: list[list[z3.ExprRef]],
) -> NullableVal:
    """Evaluate a value expression in aggregate context (SELECT or HAVING).

    Recursively resolves AggCall nodes to their computed group-aggregate
    values. Handles arithmetic, COALESCE, CAST, CASE, etc. around aggregates.
    This is critical for expressions like DEPTNO + SUM(SAL),
    COALESCE(SUM(x), 0), CAST(SUM(x) AS INTEGER).
    """
    if isinstance(expr, AggCall):
        expr_id = id(expr)
        agg_vals = agg_cache.get(expr_id)
        if agg_vals is not None:
            return _compute_aggregate(
                expr.func, expr.distinct,
                agg_vals, group_rep, n, survives, same_group,
            )
        # Not pre-computed: evaluate argument on the fly and compute
        vals = []
        for i in range(n):
            # Build a binding for combo i — we use the group_rep's binding
            # as a template but we don't have access to all bindings here.
            # Fall back to the fallback path.
            pass
        return _eval_value(expr, binding)

    if isinstance(expr, BinOp) and expr.op in _ARITH_OPS:
        # FIX.28c: Date arithmetic in aggregate context — same as _eval_value.
        if expr.op in (BinOpKind.ADD, BinOpKind.SUB) and _is_date_arithmetic(expr):
            left = _eval_group_value(expr.left, binding, agg_cache, group_rep, n, survives, same_group)
            right = _eval_group_value(expr.right, binding, agg_cache, group_rep, n, survives, same_group)
            either_null = z3.Or(left.is_null, right.is_null)
            left_days = _sym_idx_to_day_offset(left.val) if _is_date_expr_type(expr.left) else None
            right_days = _sym_idx_to_day_offset(right.val) if _is_date_expr_type(expr.right) else None
            l_val = left_days if left_days is not None else left.val
            r_val = right_days if right_days is not None else right.val
            val = _arith_op(expr.op, l_val, r_val)
            return NullableVal(
                is_null=either_null,
                val=z3.If(either_null, z3.RealVal(0), val),
            )

        left = _eval_group_value(expr.left, binding, agg_cache, group_rep, n, survives, same_group)
        right = _eval_group_value(expr.right, binding, agg_cache, group_rep, n, survives, same_group)
        either_null = z3.Or(left.is_null, right.is_null)
        val = _arith_op(expr.op, left.val, right.val)
        return NullableVal(
            is_null=either_null,
            val=z3.If(either_null, z3.RealVal(0), val),
        )

    if isinstance(expr, FuncCall):
        fname = expr.func_name.upper()
        if fname == "COALESCE" and len(expr.args) >= 2:
            result = _eval_group_value(expr.args[-1], binding, agg_cache, group_rep, n, survives, same_group)
            for arg in reversed(expr.args[:-1]):
                prev = _eval_group_value(arg, binding, agg_cache, group_rep, n, survives, same_group)
                result = NullableVal(
                    is_null=z3.And(prev.is_null, result.is_null),
                    val=z3.If(prev.is_null, result.val, prev.val),
                )
            return result
        if fname == "CAST" and len(expr.args) >= 1:
            inner_val = _eval_group_value(expr.args[0], binding, agg_cache, group_rep, n, survives, same_group)
            if len(expr.args) >= 2 and isinstance(expr.args[1], Literal) and isinstance(expr.args[1].value, str):
                target = expr.args[1].value.upper()
                if target in ("BOOLEAN", "BOOL"):
                    return NullableVal(
                        is_null=inner_val.is_null,
                        val=z3.If(inner_val.is_null, z3.RealVal(0),
                                  z3.If(inner_val.val != 0, z3.RealVal(1), z3.RealVal(0))),
                    )
            return inner_val
        if fname == "NULLIF" and len(expr.args) >= 2:
            a = _eval_group_value(expr.args[0], binding, agg_cache, group_rep, n, survives, same_group)
            b = _eval_group_value(expr.args[1], binding, agg_cache, group_rep, n, survives, same_group)
            eq = _compare_3vl(BinOpKind.EQ, a, b)
            eq_is_true = z3.And(z3.Not(eq.is_unknown), eq.val)
            return NullableVal(is_null=z3.Or(a.is_null, eq_is_true), val=a.val)
        if fname in ("IIF", "IF") and len(expr.args) >= 3:
            cond = _eval_predicate_3vl(expr.args[0], binding)
            true_val = _eval_group_value(expr.args[1], binding, agg_cache, group_rep, n, survives, same_group)
            false_val = _eval_group_value(expr.args[2], binding, agg_cache, group_rep, n, survives, same_group)
            cond_true = _tb_true(cond)
            return NullableVal(
                is_null=z3.If(cond_true, true_val.is_null, false_val.is_null),
                val=z3.If(cond_true, true_val.val, false_val.val),
            )
        if fname in ("IIF", "IF") and len(expr.args) == 2:
            cond = _eval_predicate_3vl(expr.args[0], binding)
            true_val = _eval_group_value(expr.args[1], binding, agg_cache, group_rep, n, survives, same_group)
            cond_true = _tb_true(cond)
            return NullableVal(
                is_null=z3.If(cond_true, true_val.is_null, z3.BoolVal(True)),
                val=z3.If(cond_true, true_val.val, z3.RealVal(0)),
            )
        if fname == "IFNULL" and len(expr.args) >= 2:
            a = _eval_group_value(expr.args[0], binding, agg_cache, group_rep, n, survives, same_group)
            b = _eval_group_value(expr.args[1], binding, agg_cache, group_rep, n, survives, same_group)
            return NullableVal(
                is_null=z3.And(a.is_null, b.is_null),
                val=z3.If(a.is_null, b.val, a.val),
            )
        if fname == "ABS" and len(expr.args) >= 1:
            a = _eval_group_value(expr.args[0], binding, agg_cache, group_rep, n, survives, same_group)
            return NullableVal(is_null=a.is_null, val=z3.If(a.val >= 0, a.val, -a.val))

        # FIX.27: ROUND/FLOOR/CEIL/TRUNCATE in aggregate context —
        # delegate inner args to _eval_group_value so AggCall inside
        # (e.g., ROUND(AVG(x), 2)) resolves to the aggregate value,
        # not a fresh variable.
        if fname == "ROUND" and len(expr.args) >= 1:
            # FIX.28a: ROUND(x) defaults to ROUND(x, 0).
            inner = _eval_group_value(expr.args[0], binding, agg_cache, group_rep, n, survives, same_group)
            n_digits = 0
            if len(expr.args) >= 2 and isinstance(expr.args[1], Literal) and isinstance(expr.args[1].value, (int, float)):
                n_digits = int(expr.args[1].value)
                scale = z3.RealVal(10 ** n_digits)
            else:
                scale = z3.RealVal(1)
            scaled = inner.val * scale
            half = z3.RealVal(z3.Q(1, 2))
            rounded = z3.If(
                scaled >= 0,
                z3.ToReal(z3.ToInt(scaled + half)),
                -z3.ToReal(z3.ToInt(-scaled + half)),
            )
            result_val = rounded / scale if n_digits != 0 else rounded
            return NullableVal(is_null=inner.is_null, val=result_val)
        if fname in ("FLOOR", "CEIL", "CEILING", "TRUNCATE") and len(expr.args) >= 1:
            inner = _eval_group_value(expr.args[0], binding, agg_cache, group_rep, n, survives, same_group)
            if fname == "FLOOR":
                val = z3.ToReal(z3.ToInt(inner.val))
            elif fname in ("CEIL", "CEILING"):
                val = -z3.ToReal(z3.ToInt(-inner.val))
            else:
                val = z3.If(inner.val >= 0, z3.ToReal(z3.ToInt(inner.val)), -z3.ToReal(z3.ToInt(-inner.val)))
            return NullableVal(is_null=inner.is_null, val=val)

        # FIX.17a: ANY_VALUE / FIRST_VALUE / LAST_VALUE used as aggregates
        # (not window functions).  These pick an arbitrary value from the
        # group — the representative row's value is a sound choice.
        if fname in ("ANY_VALUE", "FIRST_VALUE", "LAST_VALUE") and len(expr.args) >= 1:
            return _eval_group_value(expr.args[0], binding, agg_cache, group_rep, n, survives, same_group)

        # FIX.18e: BOOL_AND / BOOL_OR (LOGICAL_AND / LOGICAL_OR) as aggregates.
        # BOOL_AND ≡ MIN over boolean (0/1) values; BOOL_OR ≡ MAX.
        # Pre-computed arg values are stored in agg_cache with id(expr) key.
        if fname in ("LOGICAL_AND", "BOOL_AND", "LOGICAL_OR", "BOOL_OR") and len(expr.args) >= 1:
            agg_vals = agg_cache.get(id(expr))
            if agg_vals is not None:
                agg_func = AggFunc.MIN if fname in ("LOGICAL_AND", "BOOL_AND") else AggFunc.MAX
                return _compute_aggregate(agg_func, False, agg_vals, group_rep, n, survives, same_group)

        # Other functions: delegate to _eval_value
        return _eval_value(expr, binding)

    if isinstance(expr, CaseExpr):
        else_val = _eval_group_value(expr.else_, binding, agg_cache, group_rep, n, survives, same_group) if expr.else_ is not None else NullableVal(
            is_null=z3.BoolVal(True), val=z3.RealVal(0),
        )
        result_is_null = else_val.is_null
        result_val = else_val.val
        for cw in reversed(expr.whens):
            # Use aggregate-aware predicate evaluation for WHEN conditions
            # so CASE WHEN COUNT(*) = 0 THEN ... works correctly
            cond = _eval_group_predicate(cw.when, binding, agg_cache, group_rep, n, survives, same_group)
            then_val = _eval_group_value(cw.then, binding, agg_cache, group_rep, n, survives, same_group)
            result_is_null = z3.If(cond, then_val.is_null, result_is_null)
            result_val = z3.If(cond, then_val.val, result_val)
        return NullableVal(is_null=result_is_null, val=result_val)

    if isinstance(expr, UnaryOp) and expr.op == UnaryOpKind.NEG:
        operand = _eval_group_value(expr.operand, binding, agg_cache, group_rep, n, survives, same_group)
        return NullableVal(is_null=operand.is_null, val=-operand.val)

    # Non-aggregate leaf: ColumnRef, Literal, etc.
    return _eval_value(expr, binding)


def _eval_group_predicate(
    expr: Expr,
    binding: dict[str, SymbolicRow],
    agg_cache: dict[int, list[NullableVal]],
    group_rep: int,
    n: int,
    survives: list[z3.ExprRef],
    same_group: list[list[z3.ExprRef]],
) -> z3.ExprRef:
    """Evaluate a predicate in aggregate context, resolving AggCall nodes.

    Returns a z3 Bool (2-valued, TRUE where predicate is TRUE).
    Used for CASE WHEN conditions that contain aggregate calls.
    """
    if isinstance(expr, BinOp):
        if expr.op in _COMPARE_OPS:
            left = _eval_group_value(expr.left, binding, agg_cache, group_rep, n, survives, same_group)
            right = _eval_group_value(expr.right, binding, agg_cache, group_rep, n, survives, same_group)
            tb = _compare_3vl(expr.op, left, right)
            return _tb_true(tb)
        if expr.op == BinOpKind.AND:
            a = _eval_group_predicate(expr.left, binding, agg_cache, group_rep, n, survives, same_group)
            b = _eval_group_predicate(expr.right, binding, agg_cache, group_rep, n, survives, same_group)
            return z3.And(a, b)
        if expr.op == BinOpKind.OR:
            a = _eval_group_predicate(expr.left, binding, agg_cache, group_rep, n, survives, same_group)
            b = _eval_group_predicate(expr.right, binding, agg_cache, group_rep, n, survives, same_group)
            return z3.Or(a, b)
    if isinstance(expr, UnaryOp) and expr.op == UnaryOpKind.NOT:
        return z3.Not(_eval_group_predicate(expr.operand, binding, agg_cache, group_rep, n, survives, same_group))
    # Fallback to standard predicate evaluation for non-aggregate predicates
    return _eval_predicate(expr, binding)


def _eval_group_value_empty(
    expr: Expr,
) -> NullableVal:
    """Evaluate a value expression for the empty-group case (no surviving rows).

    AggCall(COUNT) → 0, other AggCall → NULL, non-aggregate → NULL.
    """
    if isinstance(expr, AggCall):
        if expr.func == AggFunc.COUNT:
            return NullableVal(is_null=z3.BoolVal(False), val=z3.RealVal(0))
        return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))

    if isinstance(expr, BinOp) and expr.op in _ARITH_OPS:
        left = _eval_group_value_empty(expr.left)
        right = _eval_group_value_empty(expr.right)
        either_null = z3.Or(left.is_null, right.is_null)
        val = _arith_op(expr.op, left.val, right.val)
        return NullableVal(
            is_null=either_null,
            val=z3.If(either_null, z3.RealVal(0), val),
        )

    if isinstance(expr, FuncCall):
        fname = expr.func_name.upper()
        if fname == "COALESCE" and len(expr.args) >= 2:
            result = _eval_group_value_empty(expr.args[-1])
            for arg in reversed(expr.args[:-1]):
                prev = _eval_group_value_empty(arg)
                result = NullableVal(
                    is_null=z3.And(prev.is_null, result.is_null),
                    val=z3.If(prev.is_null, result.val, prev.val),
                )
            return result
        if fname == "CAST" and len(expr.args) >= 1:
            return _eval_group_value_empty(expr.args[0])
        if fname in ("IIF", "IF") and len(expr.args) >= 3:
            # In empty group, non-aggregate leaves are NULL so condition is UNKNOWN → false_val
            true_val = _eval_group_value_empty(expr.args[1])
            false_val = _eval_group_value_empty(expr.args[2])
            return false_val
        if fname in ("IIF", "IF") and len(expr.args) == 2:
            return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))
        if fname == "IFNULL" and len(expr.args) >= 2:
            a = _eval_group_value_empty(expr.args[0])
            b = _eval_group_value_empty(expr.args[1])
            return NullableVal(
                is_null=z3.And(a.is_null, b.is_null),
                val=z3.If(a.is_null, b.val, a.val),
            )
        # FIX.17a: ANY_VALUE / FIRST_VALUE / LAST_VALUE in empty group → NULL
        if fname in ("ANY_VALUE", "FIRST_VALUE", "LAST_VALUE"):
            return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))
        # FIX.18e: BOOL_AND/BOOL_OR in empty group → NULL
        if fname in ("LOGICAL_AND", "BOOL_AND", "LOGICAL_OR", "BOOL_OR"):
            return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))

    if isinstance(expr, CaseExpr):
        # In empty group, all non-aggregate values are NULL
        return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))

    # Non-aggregate leaf in empty group → NULL
    return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))


# Backward compatibility alias
_eval_having_value = _eval_group_value


def _eval_having_predicate(
    expr: Expr,
    binding: dict[str, SymbolicRow],
    having_aggs: list[tuple[int, AggCall]],
    agg_cache: dict[int, list[NullableVal]],
    group_rep: int,
    n: int,
    survives: list[z3.ExprRef],
    same_group: list[list[z3.ExprRef]],
) -> TriBool:
    """Evaluate a predicate in HAVING/aggregate context, resolving aggregate calls."""
    if isinstance(expr, BinOp):
        if expr.op in _COMPARE_OPS:
            left = _eval_group_value(expr.left, binding, agg_cache, group_rep, n, survives, same_group)
            right = _eval_group_value(expr.right, binding, agg_cache, group_rep, n, survives, same_group)
            return _compare_3vl(expr.op, left, right)
        if expr.op == BinOpKind.AND:
            a = _eval_having_predicate(expr.left, binding, having_aggs, agg_cache, group_rep, n, survives, same_group)
            b = _eval_having_predicate(expr.right, binding, having_aggs, agg_cache, group_rep, n, survives, same_group)
            return _tb_and(a, b)
        if expr.op == BinOpKind.OR:
            a = _eval_having_predicate(expr.left, binding, having_aggs, agg_cache, group_rep, n, survives, same_group)
            b = _eval_having_predicate(expr.right, binding, having_aggs, agg_cache, group_rep, n, survives, same_group)
            return _tb_or(a, b)
    if isinstance(expr, UnaryOp) and expr.op == UnaryOpKind.NOT:
        return _tb_not(_eval_having_predicate(expr.operand, binding, having_aggs, agg_cache, group_rep, n, survives, same_group))

    # Fallback to normal predicate evaluation for non-aggregate parts
    return _eval_predicate_3vl(expr, binding)


def _eval_having(
    having_expr: Expr,
    binding: dict[str, SymbolicRow],
    having_aggs: list[tuple[int, AggCall]],
    agg_cache: dict[int, list[NullableVal]],
    group_rep: int,
    n: int,
    survives: list[z3.ExprRef],
    same_group: list[list[z3.ExprRef]],
) -> z3.ExprRef:
    """Evaluate HAVING clause with proper aggregate resolution. Returns z3 Bool."""
    tb = _eval_having_predicate(
        having_expr, binding, having_aggs, agg_cache,
        group_rep, n, survives, same_group,
    )
    return _tb_true(tb)


def _encode_aggregated_result_v2(
    ir: QueryIR,
    combo_data: list[tuple[z3.ExprRef, dict[str, SymbolicRow]]],
    scope: BoundedScope,
) -> list[ResultRow]:
    """Encode aggregated result from pre-computed (survives, binding) tuples.

    Same logic as _encode_aggregated_result but accepts outer-join-aware
    bindings instead of raw combos.
    """
    n = len(combo_data)

    bindings: list[dict[str, SymbolicRow]] = []
    survives_list: list[z3.ExprRef] = []
    for join_survives, binding in combo_data:
        bindings.append(binding)
        where_ok = _eval_where(ir, binding)
        survives_list.append(z3.And(join_survives, where_ok))

    # Group keys
    group_keys: list[list[NullableVal]] = []
    for binding in bindings:
        keys = [_eval_value(g, binding) for g in ir.group_by]
        group_keys.append(keys)

    # Same-group matrix
    same_group: list[list[z3.ExprRef]] = []
    for i in range(n):
        row: list[z3.ExprRef] = []
        for j in range(n):
            if not ir.group_by:
                row.append(z3.BoolVal(True))
            else:
                conditions = []
                for ki, kj in zip(group_keys[i], group_keys[j]):
                    both_null = z3.And(ki.is_null, kj.is_null)
                    both_eq = z3.And(z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val == kj.val)
                    conditions.append(z3.Or(both_null, both_eq))
                row.append(z3.And(conditions) if conditions else z3.BoolVal(True))
        same_group.append(row)

    # FIX.13e: Collect ALL aggregate calls from SELECT, HAVING, and ORDER BY
    # expressions (not just top-level AggCall). This is critical for expressions
    # like DEPTNO + SUM(SAL), COALESCE(SUM(x), 0), CAST(SUM(x) AS INT).
    all_agg_calls: list[tuple[int, AggCall]] = []
    for expr in ir.select:
        _collect_agg_calls(expr, all_agg_calls)
    if ir.having:
        _collect_agg_calls(ir.having, all_agg_calls)

    # Pre-compute aggregate argument values for all collected AggCalls
    agg_cache: dict[int, list[NullableVal]] = {}
    seen_agg_ids: set[int] = set()
    for agg_id, agg_expr in all_agg_calls:
        if agg_id in seen_agg_ids:
            continue
        seen_agg_ids.add(agg_id)
        vals = []
        for binding in bindings:
            if agg_expr.arg is not None:
                vals.append(_eval_value(agg_expr.arg, binding))
            else:
                vals.append(NullableVal(is_null=z3.BoolVal(False), val=z3.RealVal(1)))
        agg_cache[agg_id] = vals

    # Build result rows
    result_rows: list[ResultRow] = []
    for i in range(n):
        earlier_same = []
        for j in range(i):
            earlier_same.append(z3.And(survives_list[j], same_group[i][j]))
        is_rep = z3.And(survives_list[i], z3.Not(z3.Or(earlier_same)) if earlier_same else z3.BoolVal(True))

        # FIX.13e: Use _eval_group_value for ALL select expressions,
        # not just top-level AggCall. This resolves nested aggregate
        # references like DEPTNO + SUM(SAL), COALESCE(SUM(x), 0), etc.
        values: list[NullableVal] = []
        for sel_idx, expr in enumerate(ir.select):
            values.append(_eval_group_value(
                expr, bindings[i], agg_cache,
                i, n, survives_list, same_group,
            ))

        having_ok = z3.BoolVal(True)
        if ir.having:
            # FIX.13c+13e: evaluate HAVING with computed aggregate values
            having_ok = _eval_having(
                ir.having, bindings[i],
                all_agg_calls, agg_cache,
                i, n, survives_list, same_group,
            )

        final_survives = z3.And(is_rep, having_ok)
        result_rows.append(ResultRow(survives=final_survives, values=values))

    # Global aggregation on empty input: if no GROUP BY but has
    # aggregation, SQL mandates exactly one output row even when
    # all input rows are filtered out (e.g. SELECT COUNT(*) WHERE FALSE → 0).
    # Add a synthetic "empty-group" row that survives only when no combo does.
    # FIX.13e: Use _eval_group_value_empty for proper recursive handling.
    if ir.has_aggregation() and not ir.group_by:
        any_survivor = z3.Or(survives_list) if survives_list else z3.BoolVal(False)
        empty_group_survives = z3.Not(any_survivor)
        empty_values: list[NullableVal] = []
        for sel_idx, expr in enumerate(ir.select):
            empty_values.append(_eval_group_value_empty(expr))
        result_rows.append(ResultRow(survives=empty_group_survives, values=empty_values))

    return result_rows


def _compute_aggregate(
    func: AggFunc,
    distinct: bool,
    agg_vals: list[NullableVal],
    group_rep: int,
    n: int,
    survives: list[z3.ExprRef],
    same_group: list[list[z3.ExprRef]],
) -> NullableVal:
    """Compute an aggregate value for the group represented by combo group_rep."""
    if func == AggFunc.COUNT and not distinct:
        # COUNT(*) or COUNT(expr)
        terms = []
        for j in range(n):
            in_group = z3.And(survives[j], same_group[group_rep][j])
            if agg_vals[j].is_null is not None:
                # COUNT(expr) ignores NULLs; COUNT(*) counts all
                terms.append(z3.If(z3.And(in_group, z3.Not(agg_vals[j].is_null)), z3.RealVal(1), z3.RealVal(0)))
            else:
                terms.append(z3.If(in_group, z3.RealVal(1), z3.RealVal(0)))
        total = z3.Sum(terms) if len(terms) > 1 else (terms[0] if terms else z3.RealVal(0))
        return NullableVal(is_null=z3.BoolVal(False), val=total)

    if func == AggFunc.COUNT and distinct:
        # COUNT(DISTINCT expr): count unique non-null values
        # For k small, enumerate: for each combo j in group, count it if
        # it's the first non-null occurrence of its value in the group
        terms = []
        for j in range(n):
            in_group_j = z3.And(survives[j], same_group[group_rep][j], z3.Not(agg_vals[j].is_null))
            # Check no earlier combo in group has the same value
            earlier_same_val = []
            for j2 in range(j):
                in_group_j2 = z3.And(survives[j2], same_group[group_rep][j2], z3.Not(agg_vals[j2].is_null))
                earlier_same_val.append(z3.And(in_group_j2, agg_vals[j].val == agg_vals[j2].val))
            is_first = z3.And(in_group_j, z3.Not(z3.Or(earlier_same_val)) if earlier_same_val else z3.BoolVal(True))
            terms.append(z3.If(is_first, z3.RealVal(1), z3.RealVal(0)))
        total = z3.Sum(terms) if len(terms) > 1 else (terms[0] if terms else z3.RealVal(0))
        return NullableVal(is_null=z3.BoolVal(False), val=total)

    if func == AggFunc.SUM and not distinct:
        terms = []
        any_non_null_terms = []
        for j in range(n):
            in_group = z3.And(survives[j], same_group[group_rep][j])
            non_null = z3.And(in_group, z3.Not(agg_vals[j].is_null))
            terms.append(z3.If(non_null, agg_vals[j].val, z3.RealVal(0)))
            any_non_null_terms.append(non_null)
        total = z3.Sum(terms) if len(terms) > 1 else (terms[0] if terms else z3.RealVal(0))
        any_non_null = z3.Or(any_non_null_terms) if any_non_null_terms else z3.BoolVal(False)
        return NullableVal(is_null=z3.Not(any_non_null), val=total)

    if func == AggFunc.SUM and distinct:
        # FIX.17c: SUM(DISTINCT expr): sum each unique non-null value once.
        # For each combo j in the group, add its value only if it is the
        # first non-null occurrence of that value (same dedup logic as
        # COUNT(DISTINCT)).
        terms = []
        any_non_null_terms = []
        for j in range(n):
            in_group_j = z3.And(survives[j], same_group[group_rep][j], z3.Not(agg_vals[j].is_null))
            earlier_same_val = []
            for j2 in range(j):
                in_group_j2 = z3.And(survives[j2], same_group[group_rep][j2], z3.Not(agg_vals[j2].is_null))
                earlier_same_val.append(z3.And(in_group_j2, agg_vals[j].val == agg_vals[j2].val))
            is_first = z3.And(in_group_j, z3.Not(z3.Or(earlier_same_val)) if earlier_same_val else z3.BoolVal(True))
            terms.append(z3.If(is_first, agg_vals[j].val, z3.RealVal(0)))
            any_non_null_terms.append(in_group_j)
        total = z3.Sum(terms) if len(terms) > 1 else (terms[0] if terms else z3.RealVal(0))
        any_non_null = z3.Or(any_non_null_terms) if any_non_null_terms else z3.BoolVal(False)
        return NullableVal(is_null=z3.Not(any_non_null), val=total)

    if func == AggFunc.MIN:
        return _compute_min_max(agg_vals, group_rep, n, survives, same_group, is_min=True)

    if func == AggFunc.MAX:
        return _compute_min_max(agg_vals, group_rep, n, survives, same_group, is_min=False)

    if func == AggFunc.AVG:
        # FIX.27: AVG = SUM / COUNT with exact Real division (no truncation).
        # FIX.18d: AVG(DISTINCT) uses SUM(DISTINCT) / COUNT(DISTINCT)
        sum_result = _compute_aggregate(AggFunc.SUM, distinct, agg_vals, group_rep, n, survives, same_group)
        count_result = _compute_aggregate(AggFunc.COUNT, distinct, agg_vals, group_rep, n, survives, same_group)
        avg_val = z3.If(count_result.val == 0, z3.RealVal(0), sum_result.val / count_result.val)
        return NullableVal(is_null=sum_result.is_null, val=avg_val)

    return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))


def _compute_min_max(
    agg_vals: list[NullableVal],
    group_rep: int,
    n: int,
    survives: list[z3.ExprRef],
    same_group: list[list[z3.ExprRef]],
    is_min: bool,
) -> NullableVal:
    """Compute MIN or MAX over a group using nested If chains."""
    # Collect candidates: (in_group AND non-null, value)
    candidates = []
    for j in range(n):
        cond = z3.And(survives[j], same_group[group_rep][j], z3.Not(agg_vals[j].is_null))
        candidates.append((cond, agg_vals[j].val))

    if not candidates:
        return NullableVal(is_null=z3.BoolVal(True), val=z3.RealVal(0))

    any_valid = z3.Or([c for c, _ in candidates])

    # Build result using guarded accumulation.  Start with a "no value
    # seen" state and only adopt a candidate's value when it's the first
    # valid one or when it's better than the current best.  This avoids
    # seeding with candidates[0] which may not be in the group.
    result_val = candidates[0][1]  # initial val (overwritten by first valid)
    have = candidates[0][0]        # have we seen a valid candidate?
    result_val = z3.If(have, candidates[0][1], z3.RealVal(0))

    for i in range(1, len(candidates)):
        c, v = candidates[i]
        # First valid candidate: adopt its value unconditionally
        # Subsequent valid: update only if better
        if is_min:
            better = z3.And(c, z3.Or(z3.Not(have), v < result_val))
        else:
            better = z3.And(c, z3.Or(z3.Not(have), v > result_val))
        result_val = z3.If(better, v, result_val)
        have = z3.Or(have, c)

    return NullableVal(is_null=z3.Not(any_valid), val=result_val)


# ---------------------------------------------------------------------------
# ORDER BY + LIMIT k encoding (rank-based selection)
# ---------------------------------------------------------------------------

def _apply_order_limit(
    ir: QueryIR,
    result_rows: list[ResultRow],
    all_combos_with_bindings: list[tuple[z3.ExprRef, dict]],
) -> list[ResultRow]:
    """Encode ORDER BY + LIMIT k as rank-based top-k selection.

    For each surviving row, computes its rank (number of surviving rows
    strictly better under ORDER BY).  Keeps rows with rank < k.
    Ties are broken by existential tie-breaker variables (matching SQL's
    underspecified tie behavior).

    Null handling: NULLS LAST for ASC, NULLS FIRST for DESC (SQLite default).
    """
    from ..ir.types import SortDir

    k = ir.limit
    n = len(result_rows)
    if n == 0 or k is None or k <= 0:
        return result_rows

    # Evaluate ORDER BY keys for each result row
    order_keys: list[list[NullableVal]] = []
    for idx, (_, binding) in enumerate(all_combos_with_bindings[:n]):
        keys = [_eval_value(spec.expr, binding) for spec in ir.order_by]
        order_keys.append(keys)

    # Pad for aggregated queries where result_rows > combos
    while len(order_keys) < n:
        order_keys.append(order_keys[-1] if order_keys else [])

    def _is_strictly_better(keys_i: list[NullableVal], keys_j: list[NullableVal]) -> z3.ExprRef:
        """True if row i is strictly before row j under ORDER BY.

        Lexicographic comparison with NULL-aware ordering.
        """
        conditions: list[z3.ExprRef] = []
        prefix_eq: list[z3.ExprRef] = []

        for k_idx, spec in enumerate(ir.order_by):
            if k_idx >= len(keys_i) or k_idx >= len(keys_j):
                break
            ki, kj = keys_i[k_idx], keys_j[k_idx]
            asc = spec.direction == SortDir.ASC

            if asc:
                i_better_null = z3.And(z3.Not(ki.is_null), kj.is_null)
                val_better = z3.And(
                    z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val < kj.val
                )
                val_eq = z3.And(
                    z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val == kj.val
                )
            else:
                i_better_null = z3.And(ki.is_null, z3.Not(kj.is_null))
                val_better = z3.And(
                    z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val > kj.val
                )
                val_eq = z3.And(
                    z3.Not(ki.is_null), z3.Not(kj.is_null), ki.val == kj.val
                )

            both_null_eq = z3.And(ki.is_null, kj.is_null)
            key_eq = z3.Or(val_eq, both_null_eq)
            key_better = z3.Or(i_better_null, val_better)

            if prefix_eq:
                conditions.append(z3.And(z3.And(prefix_eq), key_better))
            else:
                conditions.append(key_better)
            prefix_eq.append(key_eq)

        if not conditions:
            return z3.BoolVal(False)
        return z3.Or(conditions)

    # Existential tie-breaker variables: distinct ints for surviving rows
    # so that ties in ORDER BY keys resolve to a total order.
    tb = [z3.Int(f"tb_{id(ir)}_{i}") for i in range(n)]

    # Pre-compute strictly_better and keys_tied matrices to avoid
    # redundant Z3 expression construction in the inner loop.
    better_cache: dict[tuple[int, int], z3.ExprRef] = {}
    tied_cache: dict[tuple[int, int], z3.ExprRef] = {}
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            sb = _is_strictly_better(order_keys[i], order_keys[j])
            better_cache[(i, j)] = sb
    for i in range(n):
        for j in range(i + 1, n):
            tied = z3.Not(z3.Or(better_cache[(i, j)], better_cache[(j, i)]))
            tied_cache[(i, j)] = tied
            tied_cache[(j, i)] = tied

    def _total_better(i: int, j: int) -> z3.ExprRef:
        """Total strict order: better by keys, or tied keys with tb_i < tb_j."""
        return z3.Or(better_cache[(i, j)], z3.And(tied_cache[(i, j)], tb[i] < tb[j]))

    # Rank of row i = count of surviving rows strictly before it
    new_rows: list[ResultRow] = []
    for i in range(n):
        rank_terms = []
        for j in range(n):
            if i == j:
                continue
            rank_terms.append(
                z3.If(z3.And(result_rows[j].survives, _total_better(j, i)), 1, 0)
            )
        rank_i = z3.Sum(rank_terms) if rank_terms else z3.IntVal(0)
        picked = z3.And(result_rows[i].survives, rank_i < k)
        new_rows.append(ResultRow(survives=picked, values=result_rows[i].values))

    return new_rows


# ---------------------------------------------------------------------------
# Difference predicate
# ---------------------------------------------------------------------------

def _values_match(v1: NullableVal, v2: NullableVal) -> z3.ExprRef:
    """Check if two nullable values are equal (both null or both non-null and equal)."""
    both_null = z3.And(v1.is_null, v2.is_null)
    both_equal = z3.And(z3.Not(v1.is_null), z3.Not(v2.is_null), v1.val == v2.val)
    return z3.Or(both_null, both_equal)


def _rows_match(r1: ResultRow, r2: ResultRow) -> z3.ExprRef:
    """Check if two result rows have identical projected values.

    Bug #22 fix: if the rows have different arity (different number of
    projected columns), they can never match — return False immediately.
    Previously, zip() silently dropped extra columns, making queries with
    different SELECT arity appear equivalent.
    """
    if len(r1.values) != len(r2.values):
        return z3.BoolVal(False)
    col_matches = [_values_match(v1, v2) for v1, v2 in zip(r1.values, r2.values)]
    return z3.And(col_matches) if col_matches else z3.BoolVal(True)


def _encode_difference(
    rows1: list[ResultRow],
    rows2: list[ResultRow],
    distinct1: bool = False,
    distinct2: bool = False,
) -> z3.ExprRef:
    """Assert that two query results differ under SQL bag (multiset) semantics.

    For each surviving row r_i, counts how many times its tuple value appears
    in Q1's result vs Q2's result. If any tuple has a different multiplicity,
    the results differ.

    When distinct1/distinct2 is True, the multiplicity is capped at 1
    (set semantics), matching SQL's SELECT DISTINCT behavior.
    """
    diff_conditions: list[z3.ExprRef] = []

    # Direction 1: for each surviving row in Q1, check multiplicity mismatch
    for i, r1 in enumerate(rows1):
        # Count of r1's tuple value in Q1
        count_q1 = z3.Sum([
            z3.If(z3.And(rr.survives, _rows_match(r1, rr)), 1, 0)
            for rr in rows1
        ])
        # Count of r1's tuple value in Q2
        count_q2 = z3.Sum([
            z3.If(z3.And(rr.survives, _rows_match(r1, rr)), 1, 0)
            for rr in rows2
        ])
        # Cap multiplicity for DISTINCT queries
        if distinct1:
            count_q1 = z3.If(count_q1 >= 1, z3.IntVal(1), z3.IntVal(0))
        if distinct2:
            count_q2 = z3.If(count_q2 >= 1, z3.IntVal(1), z3.IntVal(0))
        diff_conditions.append(z3.And(r1.survives, count_q1 != count_q2))

    # Direction 2: for each surviving row in Q2 not covered by Q1 tuples
    for j, r2 in enumerate(rows2):
        count_q2 = z3.Sum([
            z3.If(z3.And(rr.survives, _rows_match(r2, rr)), 1, 0)
            for rr in rows2
        ])
        count_q1 = z3.Sum([
            z3.If(z3.And(rr.survives, _rows_match(r2, rr)), 1, 0)
            for rr in rows1
        ])
        if distinct1:
            count_q1 = z3.If(count_q1 >= 1, z3.IntVal(1), z3.IntVal(0))
        if distinct2:
            count_q2 = z3.If(count_q2 >= 1, z3.IntVal(1), z3.IntVal(0))
        diff_conditions.append(z3.And(r2.survives, count_q1 != count_q2))

    if not diff_conditions:
        return z3.BoolVal(False)
    return z3.Or(diff_conditions)


# ---------------------------------------------------------------------------
# Witness minimization
# ---------------------------------------------------------------------------

def _minimize_witness(
    orig_solver: z3.Solver,
    db: object,
    sym_db: SymbolicDB,
    scope: BoundedScope,
    domain_constraints: list[z3.ExprRef],
    diff_constraint: z3.ExprRef,
    max_iterations: int = 3,
) -> z3.ModelRef | None:
    """Try to find a witness with more NULL cells (simpler witness).

    Uses a fresh solver with the same constraints plus a tightening objective.
    Returns a better model or None if no improvement found.
    """
    # Count null vars
    null_if_terms = []
    for table in sym_db.tables.values():
        for row in table.rows:
            for nv in row.cols.values():
                null_if_terms.append(z3.If(nv.is_null, 1, 0))

    if not null_if_terms:
        return None

    null_sum = z3.Sum(null_if_terms)

    # Get current null count from orig solver's model
    model = orig_solver.model()
    current_null_count = 0
    for table in sym_db.tables.values():
        for row in table.rows:
            for nv in row.cols.values():
                if z3.is_true(model.eval(nv.is_null, model_completion=True)):
                    current_null_count += 1

    best_model = None
    for _ in range(max_iterations):
        s = z3.Solver()
        s.set("timeout", scope.solver_timeout_ms)
        for c in domain_constraints:
            s.add(c)
        s.add(diff_constraint)
        s.add(null_sum > current_null_count)

        if s.check() == z3.sat:
            best_model = s.model()
            # Update count for next iteration
            current_null_count_new = 0
            for table in sym_db.tables.values():
                for row in table.rows:
                    for nv in row.cols.values():
                        if z3.is_true(best_model.eval(nv.is_null, model_completion=True)):
                            current_null_count_new += 1
            current_null_count = current_null_count_new
        else:
            break

    return best_model


# ---------------------------------------------------------------------------
# Model extraction
# ---------------------------------------------------------------------------

def _z3_val_to_int(val: z3.ExprRef) -> int:
    """Extract an integer from a z3 model value (IntNumRef or RatNumRef)."""
    if hasattr(val, 'as_long'):
        try:
            return val.as_long()
        except Exception:
            pass
    if hasattr(val, 'numerator_as_long') and hasattr(val, 'denominator_as_long'):
        num = val.numerator_as_long()
        den = val.denominator_as_long()
        return num // den if den != 0 else 0
    return 0


def _z3_val_to_numeric(val: z3.ExprRef):
    """Extract a numeric Python value from a z3 model value.

    Returns int if the value is integral, float otherwise.
    """
    if hasattr(val, 'numerator_as_long') and hasattr(val, 'denominator_as_long'):
        num = val.numerator_as_long()
        den = val.denominator_as_long()
        if den == 1:
            return num
        if den == 0:
            return 0
        return num / den  # Python float
    if hasattr(val, 'as_long'):
        return val.as_long()
    return 0


def _extract_witness(
    model: z3.ModelRef,
    db: SymbolicDB,
    scope: BoundedScope,
    schema_tables: Optional[set[str]] = None,
) -> dict[str, list[dict[str, object]]]:
    """Extract concrete table rows from the Z3 model.

    FIX.28a: When schema_tables is provided, only export tables that
    exist in the original schema.  Derived-table aliases (internal
    symbolic relations) are excluded from the witness to prevent
    DuckDB/SQLite validation from creating independent tables for
    aliases, which causes false validation confirms.
    Also skip rows whose ``present`` flag is False (padded absent rows
    from compositional DT encoding).
    """
    result: dict[str, list[dict[str, object]]] = {}

    for tname, sym_table in db.tables.items():
        # FIX.28a: Skip internal alias tables
        if schema_tables is not None and tname.lower() not in schema_tables:
            continue

        rows: list[dict[str, object]] = []
        for sym_row in sym_table.rows:
            # FIX.28a: Skip non-present rows (padded absent rows)
            present_val = model.eval(sym_row.present, model_completion=True)
            if z3.is_false(present_val):
                continue

            row_dict: dict[str, object] = {}
            for col_name, nv in sym_row.cols.items():
                is_null_val = model.eval(nv.is_null, model_completion=True)
                if z3.is_true(is_null_val):
                    row_dict[col_name] = None
                else:
                    val = model.eval(nv.val, model_completion=True)
                    col_type = sym_table.col_types.get(col_name, SemType.UNKNOWN)
                    int_val = _z3_val_to_int(val)
                    if col_type == SemType.STRING:
                        if 0 <= int_val < len(scope.string_symbols):
                            row_dict[col_name] = scope.string_symbols[int_val]
                        else:
                            row_dict[col_name] = f"s{int_val}"
                    elif col_type == SemType.BOOL:
                        row_dict[col_name] = bool(int_val)
                    elif col_type == SemType.DATE:
                        if 0 <= int_val < len(scope.string_symbols):
                            row_dict[col_name] = scope.string_symbols[int_val]
                        else:
                            row_dict[col_name] = str(date(2024, 1, 1) + timedelta(days=int_val))
                    elif col_type == SemType.TIMESTAMP:
                        row_dict[col_name] = str(date(2024, 1, 1) + timedelta(days=int_val)) + " 00:00:00"
                    else:
                        # Numeric: extract as float if fractional, int if whole
                        row_dict[col_name] = _z3_val_to_numeric(val)
            rows.append(row_dict)
        result[tname] = rows

    return result


# ---------------------------------------------------------------------------
# Batch synthesis API (incremental Z3 solving)
# ---------------------------------------------------------------------------

def batch_witness_synthesis(
    candidates: dict[str, QueryIR],
    pairs: list[tuple[str, str]],
    catalog: Catalog,
    scope: Optional[BoundedScope] = None,
    minimize: bool = False,
) -> dict[tuple[str, str], WitnessResult]:
    """Batch witness synthesis with shared symbolic DB and incremental Z3 solving.

    Creates ONE symbolic DB for all candidates, encodes each candidate's result
    ONCE, then uses Z3 push/pop for each pair's diff predicate.

    Falls back to individual synthesize_witness for pairs where batch encoding fails.
    """
    if scope is None:
        scope = BoundedScope(k_rows=2)

    results: dict[tuple[str, str], WitnessResult] = {}

    if not pairs:
        return results

    global _current_sym2idx

    # --- Step 1: Inline derived tables for all candidates ---
    inlined: dict[str, QueryIR] = {}
    for cid, ir in candidates.items():
        inlined[cid] = _inline_derived_tables(ir, catalog)

    # --- Step 2: Collect union of ALL string + int literals ---
    all_strings: set[str] = set()
    all_ints: set[int] = set()
    for ir in inlined.values():
        all_strings |= _collect_string_literals(ir)
        all_ints |= _collect_int_literal_values(ir)
    # FIX.35: Include string literals from value constraints (ENUM IN constraints)
    if catalog.value_constraints:
        all_strings |= _collect_constraint_string_literals(catalog.value_constraints)

    # --- Step 3: Build one shared string symbol table ---
    fresh = {"\x01__fresh_lo__", "\x7f__fresh_hi__"}
    symbols = sorted(all_strings | fresh)
    sym2idx = {s: i for i, s in enumerate(symbols)}
    _current_sym2idx = sym2idx

    try:
        # --- Step 4: Widen scope int bounds ---
        lo, hi = scope.int_bounds
        if all_ints:
            lo = min(lo, min(all_ints) - 1)
            hi = max(hi, max(all_ints) + 1)

        scope = BoundedScope(
            k_rows=scope.k_rows,
            int_bounds=(lo, hi),
            string_symbols=symbols,
            date_values=scope.date_values,
            null_semantics=scope.null_semantics,
            solver_timeout_ms=scope.solver_timeout_ms,
        )
        scope._n_string_symbols = len(symbols)

        # --- Step 5: Collect all table names; augment catalog ---
        # FIX.28a: Capture original schema table names before augmentation
        _batch_schema_tables = {name.lower() for name in catalog.tables}
        all_actual_tables: set[str] = set()
        all_derived: dict[str, list[tuple[str, SemType]]] = {}
        for ir in inlined.values():
            tables = _get_tables_for_query(ir)
            all_actual_tables |= set(tables.values())
            derived = _collect_derived_table_schemas(ir, catalog)
            all_derived.update(derived)

        if all_derived:
            augmented_tables: dict[str, TableInfo] = dict(catalog.tables)
            for alias, col_list in all_derived.items():
                if alias not in augmented_tables and catalog.get_table(alias) is None:
                    augmented_tables[alias] = TableInfo(
                        name=alias,
                        columns=[
                            ColumnInfo(name=name, sem_type=sem_type, nullable=True)
                            for name, sem_type in col_list
                        ],
                    )
            catalog = Catalog(tables=augmented_tables, foreign_keys=catalog.foreign_keys, value_constraints=catalog.value_constraints)

        sorted_tables = sorted(all_actual_tables)

        # Guard: if too many tables, the shared symbolic DB becomes too large
        # (risk of Z3 OOM). Fall back to individual synthesis.
        if len(sorted_tables) > 6:
            logger.debug(
                "Batch witness: %d tables exceeds limit, falling back to individual synthesis",
                len(sorted_tables),
            )
            for id_a, id_b in pairs:
                with _stats._lock:
                    _stats.total_pairs += 1
                saved = _current_sym2idx
                try:
                    results[(id_a, id_b)] = synthesize_witness(
                        candidates[id_a], candidates[id_b], catalog, scope, minimize=minimize,
                    )
                except Exception as exc:
                    logger.warning("synthesize_witness OOM in table-guard fallback (%s, %s): %s", id_a, id_b, exc)
                    results[(id_a, id_b)] = WitnessResult(status="unknown", solver_time_ms=0.0)
                finally:
                    _current_sym2idx = saved
            return results

        logger.debug(
            "Batch witness: %d candidates, %d pairs, %d tables",
            len(candidates), len(pairs), len(sorted_tables),
        )

        # --- Step 6: Create ONE symbolic DB ---
        sym_db, domain_constraints = _create_symbolic_db(sorted_tables, catalog, scope)

        # Set module-level state for inner subquery encoding
        global _current_db, _current_scope, _current_catalog
        _current_db = sym_db
        _current_scope = scope
        _current_catalog = catalog

        # --- Step 7: Encode each candidate's result ONCE ---
        encoded: dict[str, list[ResultRow]] = {}
        failed_ids: set[str] = set()
        for cid, ir in inlined.items():
            try:
                encoded[cid] = _encode_query_result(ir, sym_db, scope)
            except Exception as exc:
                logger.debug("Batch encode failed for %s: %s", cid, exc)
                failed_ids.add(cid)

        # --- Step 8: Create one Z3 solver with domain constraints ---
        solver = z3.Solver()
        solver.set("timeout", scope.solver_timeout_ms)
        solver.set("threads", 2)
        for c in domain_constraints:
            solver.add(c)

        # --- Step 9: For each pair, push/pop diff predicate ---
        for id_a, id_b in pairs:
            with _stats._lock:
                _stats.total_pairs += 1

            # FIX.32n: Bounded-k completeness guards (same as synthesize_witness)
            guard_reason = _check_bounded_k_guards(
                candidates[id_a], candidates[id_b], scope.k_rows,
            )
            if guard_reason:
                logger.debug(
                    "Batch pair (%s, %s): FIX.32n guard: %s, returning unknown",
                    id_a, id_b, guard_reason,
                )
                results[(id_a, id_b)] = WitnessResult(status="unknown", solver_time_ms=0.0)
                continue

            # If either candidate failed encoding, fall back
            if id_a in failed_ids or id_b in failed_ids:
                ir_a = candidates[id_a]
                ir_b = candidates[id_b]
                saved = _current_sym2idx
                try:
                    results[(id_a, id_b)] = synthesize_witness(
                        ir_a, ir_b, catalog, scope, minimize=minimize,
                    )
                except Exception as exc:
                    logger.warning("synthesize_witness OOM for encoding-failed pair (%s, %s): %s", id_a, id_b, exc)
                    results[(id_a, id_b)] = WitnessResult(status="unknown", solver_time_ms=0.0)
                finally:
                    _current_sym2idx = saved
                continue

            rows_a = encoded[id_a]
            rows_b = encoded[id_b]

            distinct_a = inlined[id_a].distinct
            distinct_b = inlined[id_b].distinct

            # Check SELECT arity mismatch — structurally different queries
            if rows_a and rows_b and len(rows_a[0].values) != len(rows_b[0].values):
                with _stats._lock:
                    _stats.total_sat += 1
                # No witness DB needed: arity mismatch is structural proof
                # (queries return different numbers of columns).
                results[(id_a, id_b)] = WitnessResult(status="unknown", solver_time_ms=0.0)
                continue

            try:
                diff = _encode_difference(rows_a, rows_b, distinct1=distinct_a, distinct2=distinct_b)

                solver.push()
                solver.add(diff)

                start = time.monotonic()
                check_result = solver.check()
                elapsed_ms = (time.monotonic() - start) * 1000

                if check_result == z3.sat:
                    with _stats._lock:
                        _stats.total_sat += 1
                    model = solver.model()
                    witness = _extract_witness(model, sym_db, scope, schema_tables=_batch_schema_tables)
                    results[(id_a, id_b)] = WitnessResult(
                        status="sat", witness_db=witness, solver_time_ms=elapsed_ms,
                    )
                elif check_result == z3.unsat:
                    with _stats._lock:
                        _stats.total_unsat += 1
                    results[(id_a, id_b)] = WitnessResult(
                        status="unsat", solver_time_ms=elapsed_ms,
                    )
                else:
                    with _stats._lock:
                        _stats.total_unknown += 1
                        _stats.total_timeout += 1
                    results[(id_a, id_b)] = WitnessResult(
                        status="timeout", solver_time_ms=elapsed_ms,
                    )

                solver.pop()

                logger.debug(
                    "Batch pair (%s, %s): %s (%.1fms)",
                    id_a, id_b, results[(id_a, id_b)].status, elapsed_ms,
                )
            except Exception as exc:
                # Z3 OOM or other solver failure — fall back to individual
                logger.debug("Batch pair (%s, %s) failed: %s, falling back", id_a, id_b, exc)
                try:
                    solver.pop()
                except Exception:
                    pass
                saved = _current_sym2idx
                try:
                    results[(id_a, id_b)] = synthesize_witness(
                        candidates[id_a], candidates[id_b], catalog, scope, minimize=minimize,
                    )
                except Exception as inner_exc:
                    logger.warning("synthesize_witness OOM in batch fallback (%s, %s): %s", id_a, id_b, inner_exc)
                    results[(id_a, id_b)] = WitnessResult(status="unknown", solver_time_ms=0.0)
                finally:
                    _current_sym2idx = saved

    finally:
        _current_sym2idx = {}
        _current_db = None
        _current_scope = None
        _current_catalog = None
        _precomputed_windows.clear()
        _uninterp_funcs.clear()

    return results


# ---------------------------------------------------------------------------
# Top-level synthesis API
# ---------------------------------------------------------------------------

def synthesize_witness(
    q1: QueryIR,
    q2: QueryIR,
    catalog: Catalog,
    scope: Optional[BoundedScope] = None,
    minimize: bool = True,
    validate_witnesses: bool = False,
    original_sql: tuple[str, str] | None = None,
    enable_preprocessing: bool = True,
    _skip_bounded_k_guards: bool = False,
    normalize_column_order: bool = True,
) -> WitnessResult:
    """Synthesize a witness DB where Q1 and Q2 produce different results.

    Args:
        q1, q2: Two candidate query IRs to distinguish.
        catalog: Schema catalog.
        scope: Bounded semantics parameters.
        minimize: Whether to minimize the witness.
        validate_witnesses: If True, execute both queries on the witness DB
            in SQLite after SAT. If results match (spurious witness),
            downgrade to ``"unknown"`` instead of ``"sat"``.
        original_sql: Optional (sql1, sql2) original SQL strings for more
            robust validation (avoids IR rendering issues).
        _skip_bounded_k_guards: Internal flag. When True, skip FIX.32n
            guards (used by synthesize_witness_adaptive which runs guards
            once against max_k, not per-step).

    Returns:
        WitnessResult with status and (if SAT) the witness DB.
    """
    if scope is None:
        scope = BoundedScope(k_rows=2)

    # Keep originals for validation (before inlining/expansion)
    q1_orig, q2_orig = q1, q2

    with _stats._lock:
        _stats.total_pairs += 1
    distinct_mismatch = q1.distinct != q2.distinct
    if distinct_mismatch:
        with _stats._lock:
            _stats.distinct_mismatch_pairs += 1
    has_case_expr = _contains_case_expr(q1.select) or _contains_case_expr(q2.select)
    if has_case_expr:
        with _stats._lock:
            _stats.caseexpr_present_pairs += 1

    # FIX.32m: Guard against unmodeled LIMIT/OFFSET.
    # Our Z3 encoding partially models ORDER BY + LIMIT but not OFFSET.
    # Block only the cases where the guard is needed for soundness:
    #   1. OFFSET anywhere (not modeled at all)
    #   2. LIMIT inside a derived table / subquery (complex interaction)
    #   3. Top-level LIMIT values that differ between Q1 and Q2
    # When both queries share the same top-level LIMIT (or neither has
    # one), the comparison is valid under bounded semantics.
    def _has_offset(ir: QueryIR) -> bool:
        from ..ir.types import DerivedTable
        if getattr(ir, 'offset', None) is not None:
            return True
        if isinstance(ir.from_table, DerivedTable) and _has_offset(ir.from_table.query):
            return True
        for j in ir.joins:
            if isinstance(j.right, DerivedTable) and _has_offset(j.right.query):
                return True
        return False

    def _has_dt_limit(ir: QueryIR) -> bool:
        """Check if any derived table (subquery) has LIMIT."""
        from ..ir.types import DerivedTable
        if isinstance(ir.from_table, DerivedTable):
            inner = ir.from_table.query
            if inner.limit is not None or _has_dt_limit(inner):
                return True
        for j in ir.joins:
            if isinstance(j.right, DerivedTable):
                inner = j.right.query
                if inner.limit is not None or _has_dt_limit(inner):
                    return True
        return False

    if _has_offset(q1) or _has_offset(q2):
        logger.debug("Witness synthesis: OFFSET detected, returning unknown (unmodeled)")
        return WitnessResult(status="unknown", witness_db=None, solver_time_ms=0)

    if _has_dt_limit(q1) or _has_dt_limit(q2):
        logger.debug("Witness synthesis: LIMIT in derived table detected, returning unknown")
        return WitnessResult(status="unknown", witness_db=None, solver_time_ms=0)

    if q1.limit != q2.limit:
        logger.debug("Witness synthesis: differing top-level LIMIT (%s vs %s), returning unknown",
                      q1.limit, q2.limit)
        return WitnessResult(status="unknown", witness_db=None, solver_time_ms=0)

    # FIX.32n: Bounded-k completeness guards.
    # Skipped when called from synthesize_witness_adaptive (which runs
    # its own guards once against max_k).
    if not _skip_bounded_k_guards:
        guard_reason = _check_bounded_k_guards(q1, q2, scope.k_rows)
        if guard_reason:
            logger.debug("FIX.32n guard: %s, returning unknown", guard_reason)
            return WitnessResult(status="unknown", witness_db=None, solver_time_ms=0)

    # Expand SELECT * to explicit column refs (FIX.13b)
    q1 = _expand_stars(q1, catalog)
    q2 = _expand_stars(q2, catalog)

    # FIX.20b: Normalize column order.  When both queries project the
    # same set of column names but in a different order (e.g. SELECT *
    # vs explicit column list), reorder Q2's SELECT to match Q1 so the
    # positional comparison doesn't produce spurious differences.
    # Disabled for formal equivalence checking (e.g. VeriEQL comparison)
    # where column order is part of the result schema.
    if normalize_column_order:
        q1, q2 = _normalize_column_order(q1, q2)
    else:
        # Column order matters: detect reordered columns as definitive NEQ.
        # If both SELECT lists have the same multiset of named columns but
        # in different positional order, the queries are non-equivalent.
        if len(q1.select) == len(q2.select):
            def _col_name(expr: Expr) -> Optional[str]:
                if hasattr(expr, 'alias') and expr.alias:
                    return expr.alias.upper()
                if isinstance(expr, ColumnRef):
                    return expr.column.upper()
                return None
            n1 = [_col_name(e) for e in q1.select]
            n2 = [_col_name(e) for e in q2.select]
            if (None not in n1 and None not in n2
                    and sorted(n1) == sorted(n2)
                    and n1 != n2):
                logger.debug("Column order mismatch: %s vs %s → NEQ", n1, n2)
                return WitnessResult(status="sat", witness_db=None, solver_time_ms=0)

    # Inline derived tables to use base-table semantics
    q1 = _inline_derived_tables(q1, catalog)
    q2 = _inline_derived_tables(q2, catalog)

    # FIX.21b: Normalize aggregate decomposition rewrites
    q1 = _normalize_aggregate_decomposition(q1)
    q2 = _normalize_aggregate_decomposition(q2)

    # FIX.22: Normalize anti-join LEFT JOIN patterns to NOT IN subqueries
    q1 = _normalize_antijoin_left_join(q1)
    q2 = _normalize_antijoin_left_join(q2)

    # FIX.22b: Normalize aggregate push-down over UNION ALL
    q1 = _normalize_aggregate_pushdown_union(q1)
    q2 = _normalize_aggregate_pushdown_union(q2)

    # Phase 7 preprocessing: normalize joins and eliminate redundant tables
    # to reduce combo count before the safety limit check.
    if enable_preprocessing:
        from .preprocessing import preprocess_for_synthesis
        q1, prep_stats_q1 = preprocess_for_synthesis(q1, catalog)
        q2, prep_stats_q2 = preprocess_for_synthesis(q2, catalog)
    else:
        prep_stats_q1, prep_stats_q2 = {}, {}

    # Build deterministic string symbol table (rank-based, lex-order-preserving)
    global _current_sym2idx
    symbols, sym2idx = _build_string_symbol_table(q1, q2, catalog.value_constraints)
    _current_sym2idx = sym2idx

    try:
        # Widen integer domain to include integer literal values (Bug #21)
        int_literals = _collect_int_literal_values(q1) | _collect_int_literal_values(q2)

        lo, hi = scope.int_bounds
        if int_literals:
            lo = min(lo, min(int_literals) - 1)
            hi = max(hi, max(int_literals) + 1)

        # Preserve compositional combo limit if set (Direction D)
        _comp_limit = getattr(scope, '_compositional_combo_limit', 0)

        scope = BoundedScope(
            k_rows=scope.k_rows,
            int_bounds=(lo, hi),
            string_symbols=symbols,  # now the actual sorted symbol table
            date_values=scope.date_values,
            null_semantics=scope.null_semantics,
            solver_timeout_ms=scope.solver_timeout_ms,
        )
        scope._n_string_symbols = len(symbols)
        if _comp_limit > 0:
            scope._compositional_combo_limit = _comp_limit  # type: ignore[attr-defined]

        # Collect all tables referenced by either query
        tables_q1 = _get_tables_for_query(q1)
        tables_q2 = _get_tables_for_query(q2)
        all_actual_tables = sorted(set(tables_q1.values()) | set(tables_q2.values()))

        logger.debug("Witness synthesis: tables=%s, k=%d", all_actual_tables, scope.k_rows)

        # Log the SQL being compared
        try:
            from ..ir.render_sql import render as _render_sql
            sql1 = _render_sql(q1, dialect="sqlite")
            sql2 = _render_sql(q2, dialect="sqlite")
            logger.debug("  Q1: %s", sql1.replace("\n", " "))
            logger.debug("  Q2: %s", sql2.replace("\n", " "))
        except Exception as e:
            logger.debug("Debug SQL render failed: %s", e)

        # Log the IR as JSON at TRACE level
        if logger.isEnabledFor(5):
            logger.log(5, "  Q1 IR: %s", q1.model_dump_json(exclude_defaults=True))
            logger.log(5, "  Q2 IR: %s", q2.model_dump_json(exclude_defaults=True))

        # FIX.28a: Capture original schema table names before augmentation.
        # These are the only tables that should appear in the witness DB.
        _original_schema_tables = {name.lower() for name in catalog.tables}

        # Augment catalog with derived table schemas so symbolic rows are created
        derived_q1 = _collect_derived_table_schemas(q1, catalog)
        derived_q2 = _collect_derived_table_schemas(q2, catalog)
        all_derived = {**derived_q1, **derived_q2}

        if all_derived:
            augmented_tables: dict[str, TableInfo] = dict(catalog.tables)
            for alias, col_list in all_derived.items():
                if alias not in augmented_tables and catalog.get_table(alias) is None:
                    augmented_tables[alias] = TableInfo(
                        name=alias,
                        columns=[
                            ColumnInfo(name=name, sem_type=sem_type, nullable=True)
                            for name, sem_type in col_list
                        ],
                    )
            catalog = Catalog(tables=augmented_tables, foreign_keys=catalog.foreign_keys, value_constraints=catalog.value_constraints)
            # FIX.25a: Add DT aliases to all_actual_tables so _create_symbolic_db
            # creates placeholder rows that compositional encoding will replace.
            for alias in all_derived:
                if alias not in all_actual_tables:
                    all_actual_tables.append(alias)
            all_actual_tables.sort()

        # Create symbolic DB
        sym_db, domain_constraints = _create_symbolic_db(all_actual_tables, catalog, scope)

        # Set module-level state for inner subquery encoding
        global _current_db, _current_scope, _current_catalog
        _current_db = sym_db
        _current_scope = scope
        _current_catalog = catalog

        # FIX.31a: Per-query derived-table encoding.
        # When Q1 and Q2 have DTs with the same alias but different column
        # schemas (e.g., both have TEMP but with MIN_DATE vs FIRST_LOGIN),
        # a shared encoding would clobber one query's columns.  Instead,
        # encode DTs and evaluate each query separately, saving/restoring
        # the symbolic table state between them.
        def _encode_dts_for_query(q, sym_db, catalog, scope):
            """Encode derived tables for a single query, returning modified sym_db entries."""
            dt_entries = {}
            for dt in _collect_remaining_derived_tables(q):
                dt_alias = dt.alias.lower()
                if dt_alias in sym_db.tables:
                    encoded = _encode_derived_table_rows(dt, sym_db, catalog, scope)
                    if encoded is not None:
                        dt_entries[dt_alias] = encoded
                        sym_db.tables[dt_alias] = encoded
                        logger.debug("Compositional encoding: replaced %s with bound rows", dt_alias)
            return dt_entries

        # Collect DT aliases that appear in both queries (potential conflicts)
        dts_q1 = {dt.alias.lower(): dt for dt in _collect_remaining_derived_tables(q1)}
        dts_q2 = {dt.alias.lower(): dt for dt in _collect_remaining_derived_tables(q2)}
        shared_dt_aliases = set(dts_q1.keys()) & set(dts_q2.keys())

        # Check if any shared alias has different column schemas
        has_dt_conflict = False
        for alias in shared_dt_aliases:
            cols1 = _get_projected_col_names(dts_q1[alias])
            cols2 = _get_projected_col_names(dts_q2[alias])
            if cols1 != cols2:
                has_dt_conflict = True
                logger.debug("DT alias %s: column conflict q1=%s vs q2=%s", alias, cols1, cols2)
                break

        # Adaptive combo limit based on query shape:
        # - Non-aggregated queries: allow up to 512 combos
        # - Aggregated/DISTINCT/ORDER+LIMIT queries: O(n²) encoding phases
        #   make larger combos expensive, cap at 256
        # - Base limit stays at 64 for backward compatibility
        _BASE_COMBO_LIMIT = 256
        _EXTENDED_COMBO_LIMIT = 1024
        _AGG_COMBO_LIMIT = 512

        is_complex_q1 = q1.has_aggregation() or q1.distinct or (q1.order_by and q1.limit)
        is_complex_q2 = q2.has_aggregation() or q2.distinct or (q2.order_by and q2.limit)
        is_complex = is_complex_q1 or is_complex_q2

        # Use extended limits if preprocessing reduced table count
        was_preprocessed = (
            prep_stats_q1.get("tables_before", 0) != prep_stats_q1.get("tables_after", 0)
            or prep_stats_q2.get("tables_before", 0) != prep_stats_q2.get("tables_after", 0)
            or prep_stats_q1.get("promoted", 0) > 0
            or prep_stats_q2.get("promoted", 0) > 0
        )

        if was_preprocessed:
            combo_limit = _AGG_COMBO_LIMIT if is_complex else _EXTENDED_COMBO_LIMIT
        else:
            combo_limit = _BASE_COMBO_LIMIT

        # Compositional verification uses a higher combo limit (D.5)
        compositional_limit = getattr(scope, '_compositional_combo_limit', 0)
        if compositional_limit > 0:
            combo_limit = max(combo_limit, compositional_limit)

        # FIX.19d: Compute actual combo count from OUTER aliases only
        # (not recursively collected inner DT tables).  Also accounts for
        # tables with fewer rows (e.g., __values_dual__ has 1 row).
        def _outer_combo_count(ir: QueryIR, sym_db: SymbolicDB, alias_to_table: dict, k: int) -> int:
            """Compute the actual combo count for the outer-level tables only."""
            result = 1
            aliases = [ir.from_table.ref_name.lower()]
            for j in ir.joins:
                aliases.append(j.right.ref_name.lower())
            for a in aliases:
                tbl = alias_to_table.get(a, a)
                sym_tbl = sym_db.tables.get(tbl) or sym_db.tables.get(a)
                result *= len(sym_tbl.rows) if sym_tbl else k
            # For set-op branches, also check the max across branches
            if ir.set_right:
                rhs_count = _outer_combo_count(ir.set_right, sym_db, _get_tables_for_query(ir.set_right), k)
                result = max(result, rhs_count)
            return max(result, 1)

        if has_dt_conflict:
            # FIX.31a: Encode DTs per-query to avoid column name conflicts.
            # Save base symbolic table state, encode Q1's DTs, evaluate Q1,
            # restore base state, encode Q2's DTs, evaluate Q2.
            base_tables = {alias: sym_db.tables[alias] for alias in shared_dt_aliases if alias in sym_db.tables}

            # Encode Q1's DTs and evaluate Q1
            _encode_dts_for_query(q1, sym_db, catalog, scope)
            max_combos_q1 = _outer_combo_count(q1, sym_db, tables_q1, scope.k_rows)
            if max_combos_q1 > combo_limit:
                logger.info("Witness synthesis: skipping Q1 (combo count %d > %d)", max_combos_q1, combo_limit)
                return WitnessResult(status="unknown", witness_db=None, solver_time_ms=0)
            result1 = _encode_query_with_setops(q1, sym_db, scope)

            # Restore base tables and encode Q2's DTs
            for alias, base_tbl in base_tables.items():
                sym_db.tables[alias] = base_tbl
            _encode_dts_for_query(q2, sym_db, catalog, scope)
            max_combos_q2 = _outer_combo_count(q2, sym_db, tables_q2, scope.k_rows)
            if max_combos_q2 > combo_limit:
                logger.info("Witness synthesis: skipping Q2 (combo count %d > %d)", max_combos_q2, combo_limit)
                return WitnessResult(status="unknown", witness_db=None, solver_time_ms=0)
            result2 = _encode_query_with_setops(q2, sym_db, scope)
        else:
            # No conflict: encode all DTs in shared sym_db (existing behavior)
            for q in (q1, q2):
                _encode_dts_for_query(q, sym_db, catalog, scope)

            max_combos = max(
                _outer_combo_count(q1, sym_db, tables_q1, scope.k_rows),
                _outer_combo_count(q2, sym_db, tables_q2, scope.k_rows),
            )
            n_tables_q1 = len(tables_q1)
            n_tables_q2 = len(tables_q2)
            if max_combos > combo_limit:
                logger.info("Witness synthesis: skipping (combo count %d > %d for %d/%d tables)",
                            max_combos, combo_limit, n_tables_q1, n_tables_q2)
                return WitnessResult(status="unknown", witness_db=None, solver_time_ms=0)

            # Encode both query results (handles set operations if present)
            result1 = _encode_query_with_setops(q1, sym_db, scope)
            result2 = _encode_query_with_setops(q2, sym_db, scope)

        # Build difference predicate (pass DISTINCT flags for correct bag/set semantics)
        diff = _encode_difference(result1, result2, distinct1=q1.distinct, distinct2=q2.distinct)

        # Solve
        solver = z3.Solver()
        solver.set("timeout", scope.solver_timeout_ms)
        solver.set("threads", 2)

        for c in domain_constraints:
            solver.add(c)
        solver.add(diff)

        # Log the full SMT formula at TRACE level (logging level 5)
        if logger.isEnabledFor(5):
            logger.log(5, "SMT formula (%d assertions):\n%s", len(solver.assertions()), solver.sexpr())
        elif logger.isEnabledFor(logging.DEBUG):
            logger.debug("  Solver: %d domain constraints + diff predicate (%d Q1 rows, %d Q2 rows)",
                          len(domain_constraints), len(result1), len(result2))

        start = time.monotonic()
        check_result = solver.check()
        elapsed_ms = (time.monotonic() - start) * 1000

        solver_status = "sat" if check_result == z3.sat else ("unsat" if check_result == z3.unsat else "timeout")
        logger.debug("Solver: status=%s, time=%.1fms", solver_status, elapsed_ms)

        if check_result == z3.sat:
            with _stats._lock:
                _stats.total_sat += 1
                if has_case_expr:
                    _stats.caseexpr_sat += 1

            model = solver.model()

            # Optionally minimize: try to find a model with more NULLs
            if minimize:
                better = _minimize_witness(solver, None, sym_db, scope, domain_constraints, diff)
                if better is not None:
                    model = better

            witness = _extract_witness(model, sym_db, scope, schema_tables=_original_schema_tables)

            # Type-validity check: reject witnesses with values that violate
            # declared column types (e.g., string in DATE column).
            if witness is not None:
                from .witness_export import _witness_types_valid
                if not _witness_types_valid(witness, catalog):
                    logger.debug("Witness type-invalid, downgrading to unknown")
                    return WitnessResult(status="unknown", witness_db=witness, solver_time_ms=elapsed_ms)

            # FIX.4+FIX.12+FIX.13a: Post-SAT witness validation gate
            # DuckDB-primary: handles VALUES, INTERSECT ALL, $-ids, FETCH, etc.
            # Falls back to SQLite only when DuckDB is unavailable.
            if validate_witnesses and witness is not None:
                try:
                    vr = None
                    validated = False
                    if original_sql is not None:
                        # 1) Try original SQL on DuckDB (most capable)
                        try:
                            from .witness_export import validate_witness_duckdb
                            vr = validate_witness_duckdb(original_sql[0], original_sql[1], witness, catalog)
                            if vr and not vr.error:
                                validated = True
                        except Exception:
                            pass
                        # 2) If DuckDB failed/unavailable, try original SQL on SQLite
                        if vr is None or vr.error:
                            from .witness_export import validate_witness_sql as _validate_sql
                            vr_sq = _validate_sql(original_sql[0], original_sql[1], witness, catalog)
                            if not vr_sq.error:
                                vr = vr_sq
                                validated = True
                        # If original SQL was provided but could not be validated
                        # on any engine, the witness is not defensible proof.
                        if not validated:
                            logger.debug(
                                "Witness validation: original SQL failed on all engines, "
                                "downgrading to unknown (no defensible proof)"
                            )
                            return WitnessResult(status="unknown", witness_db=witness, solver_time_ms=elapsed_ms)
                    else:
                        # No original SQL provided (e.g. unit tests, IR-only callers):
                        # fall back to rendered-IR validation on SQLite.
                        from .witness_export import validate_witness as _validate
                        vr = _validate(q1_orig, q2_orig, witness, catalog)
                        if vr.error:
                            vr = _validate(q1, q2, witness, catalog)
                        if vr.error:
                            logger.debug("Witness validation: rendered IR failed (%s), downgrading to unknown", vr.error)
                            return WitnessResult(status="unknown", witness_db=witness, solver_time_ms=elapsed_ms)
                        validated = True
                    if vr.is_spurious:
                        logger.debug("Witness validation: spurious (results match in validation)")
                        return WitnessResult(status="unknown", witness_db=witness, solver_time_ms=elapsed_ms)
                except Exception as val_exc:
                    logger.debug("Witness validation exception: %s, downgrading to unknown", val_exc)
                    return WitnessResult(status="unknown", witness_db=witness, solver_time_ms=elapsed_ms)

            return WitnessResult(status="sat", witness_db=witness, solver_time_ms=elapsed_ms)

        if check_result == z3.unsat:
            with _stats._lock:
                _stats.total_unsat += 1
                if distinct_mismatch:
                    _stats.distinct_mismatch_unsat += 1
                    logger.warning("SUSPECTED FALSE UNSAT: distinct mismatch (q1.distinct=%s, q2.distinct=%s) but solver returned UNSAT",
                                   q1.distinct, q2.distinct)
                if has_case_expr:
                    _stats.caseexpr_unsat += 1
            return WitnessResult(status="unsat", solver_time_ms=elapsed_ms)

        # unknown / timeout
        with _stats._lock:
            _stats.total_unknown += 1
            _stats.total_timeout += 1
            if has_case_expr:
                _stats.caseexpr_unknown += 1
        return WitnessResult(status="timeout", solver_time_ms=elapsed_ms)
    finally:
        _current_sym2idx = {}
        _current_db = None
        _current_scope = None
        _current_catalog = None
        _precomputed_windows.clear()
        _uninterp_funcs.clear()


def synthesize_witness_adaptive(
    q1: QueryIR,
    q2: QueryIR,
    catalog: Catalog,
    scope: Optional[BoundedScope] = None,
    minimize: bool = True,
    validate_witnesses: bool = False,
    k_schedule: list[int] | None = None,
    original_sql: tuple[str, str] | None = None,
    at_most_k: bool = False,
    enable_preprocessing: bool = True,
    normalize_column_order: bool = True,
    enable_empirical_escalation: bool = True,
) -> WitnessResult:
    """Adaptive k escalation wrapper around synthesize_witness.

    Follows *k_schedule* (default ``[2, 4, 8]``).  Only escalates to the
    next k if the previous result was UNSAT.  Stops early on SAT,
    TIMEOUT, or UNKNOWN.

    When *at_most_k* is True, uses SpotIt-style dense schedule ``[1, 2, …, K]``
    where K is ``scope.k_rows`` (or the max of the explicit *k_schedule*).
    This gives **at-most-K** semantics: UNSAT is only claimed when every
    cardinality 1..K has been individually discharged, making the proof
    monotone across N.  The trade-off is up to K solver calls instead of
    ``len(k_schedule)`` calls.

    When *at_most_k* is True, witness validation is **always enabled**
    (regardless of *validate_witnesses*) to prevent spurious SAT at one
    k from short-circuiting the loop.  A spurious SAT (downgraded to
    ``"unknown"`` by validation) is treated as a discharged k and the
    loop continues to the next cardinality.

    A structural guard restricts escalation to "simple" queries
    (≤3 table aliases, no window functions, no outer joins, no set ops)
    to prevent state-space explosion.
    """
    if scope is None:
        scope = BoundedScope(k_rows=2)
    if at_most_k:
        max_k = scope.k_rows if k_schedule is None else max(k_schedule)
        k_schedule = list(range(1, max_k + 1))
    elif k_schedule is None:
        k_schedule = [2, 4, 8]

    # Structural guard: only escalate for simple queries
    def _is_simple(ir: QueryIR) -> bool:
        n_aliases = 1 + len(ir.joins)
        if n_aliases > 3:
            return False
        # FIX.19a: Wide SELECTs make the diff predicate huge at higher k.
        # Check for Star() which expands to many columns.
        has_star = any(isinstance(e, Star) for e in ir.select)
        col_count = len(ir.select)
        if has_star:
            # Star expands to ~10 cols per table — estimate conservatively
            col_count = n_aliases * 8
        if col_count > 10:
            return False
        if any(isinstance(e, WindowFunc) for e in ir.select):
            return False
        if any(j.join_type in (JoinType.LEFT, JoinType.RIGHT, JoinType.FULL) for j in ir.joins):
            return False
        if ir.set_op is not None:
            return False
        # Subqueries in WHERE/SELECT multiply formula size at higher k.
        # Block escalation for queries with correlated subqueries.
        def _has_subquery(expr: Expr | None) -> bool:
            if expr is None:
                return False
            if isinstance(expr, (InSubquery, ExistsSubquery, ScalarSubquery)):
                return True
            if isinstance(expr, BinOp):
                return _has_subquery(expr.left) or _has_subquery(expr.right)
            if isinstance(expr, UnaryOp):
                return _has_subquery(expr.operand)
            if isinstance(expr, CaseExpr):
                return any(_has_subquery(w.when) or _has_subquery(w.then) for w in expr.whens) or _has_subquery(expr.else_)
            return False
        if _has_subquery(ir.where) or any(_has_subquery(e) for e in ir.select):
            return False
        # Estimate combo count at max k — block if it would exceed limit.
        max_k = k_schedule[-1]
        estimated_combos = max_k ** n_aliases
        if estimated_combos > 64:
            return False
        return True

    allow_escalation = _is_simple(q1) and _is_simple(q2)

    # FIX.32n: Bounded-k completeness guards.
    # Run ONCE against the max k in the schedule (not per-step k).
    max_k = k_schedule[-1]
    guard_reason = _check_bounded_k_guards(q1, q2, max_k)
    if guard_reason:
        logger.debug("FIX.32n guard (adaptive): %s, returning unknown", guard_reason)
        return WitnessResult(status="unknown", witness_db=None, solver_time_ms=0)

    # At-most-K mode always validates witnesses to catch spurious SAT
    # that would otherwise short-circuit the loop at one k value.
    effective_validate = validate_witnesses or at_most_k

    result = None
    last_unsat = None
    last_unsat_k = None
    # Wall-clock guard: cap total time per adaptive call to 2× the solver
    # timeout.  This catches cases where the Z3 *encoding* phase (not the
    # solver) hangs at high k due to combinatorial blowup.
    wall_limit_ms = scope.solver_timeout_ms * 2
    wall_start = time.monotonic()
    for k in k_schedule:
        elapsed_wall_ms = (time.monotonic() - wall_start) * 1000
        if elapsed_wall_ms > wall_limit_ms:
            logger.debug(
                "adaptive: wall-clock limit %.0fms exceeded at k=%d, stopping",
                wall_limit_ms, k,
            )
            if last_unsat is not None:
                last_unsat.proven_k = last_unsat_k
                last_unsat.complete = False
                return last_unsat
            return WitnessResult(
                status="timeout", proven_k=k,
                solver_time_ms=elapsed_wall_ms,
            )
        k_scope = BoundedScope(
            k_rows=k,
            int_bounds=scope.int_bounds,
            string_symbols=scope.string_symbols,
            date_values=scope.date_values,
            null_semantics=scope.null_semantics,
            solver_timeout_ms=scope.solver_timeout_ms,
        )
        result = synthesize_witness(
            q1, q2, catalog, k_scope,
            minimize=minimize,
            validate_witnesses=effective_validate,
            original_sql=original_sql,
            enable_preprocessing=enable_preprocessing,
            _skip_bounded_k_guards=True,  # guards already ran above against max_k
            normalize_column_order=normalize_column_order,
        )
        if result.status == "sat":
            result.proven_k = k
            return result
        elif result.status == "unsat":
            last_unsat = result
            last_unsat_k = k
        elif result.status in ("unknown", "timeout"):
            if at_most_k:
                # In at-most-K mode, "unknown" from a spurious-SAT
                # downgrade (witness_db present) means the encoding
                # produced a bogus counterexample at this k.  Treat it
                # as discharged and continue — the real SQL execution
                # showed no difference.
                if result.witness_db is not None:
                    logger.debug(
                        "at-most-k: spurious SAT at k=%d downgraded to "
                        "unknown, treating as discharged", k,
                    )
                    # FIX.30d: Don't overwrite a genuine UNSAT from a lower k
                    # with the "unknown" from a discharged spurious SAT.
                    # Only update last_unsat if we don't already have one.
                    if last_unsat is None or last_unsat.status != "unsat":
                        last_unsat = result
                        last_unsat_k = k
                    continue
                # True solver UNKNOWN/TIMEOUT (no witness): cannot claim
                # at-most-K equivalence.
                if last_unsat is not None:
                    last_unsat.proven_k = last_unsat_k
                    last_unsat.complete = False
                    return last_unsat
                result.proven_k = k
                return result
            else:
                # Sparse schedule: If a lower k already proved UNSAT, the
                # UNKNOWN at higher k is a capacity limit (combo skip or
                # solver timeout), not a refutation.
                # FIX.28b: Mark as incomplete — the proof holds at proven_k
                # but higher k values were not fully discharged.
                if last_unsat is not None:
                    last_unsat.proven_k = last_unsat_k
                    last_unsat.complete = (last_unsat_k == max_k)
                    return last_unsat
                result.proven_k = k
                return result
        if not allow_escalation and not at_most_k:
            result.proven_k = k
            result.complete = False
            return result
    # Completed the full schedule
    if last_unsat is not None:
        # FIX.31b: If last_unsat is a discharged spurious SAT (status=unknown,
        # witness_db present), try empirical escalation before returning.
        if (last_unsat.status != "unsat"
                and last_unsat.witness_db is not None
                and at_most_k
                and original_sql is not None
                and enable_empirical_escalation):
            try:
                from .witness_export import empirical_equivalence_check
                if empirical_equivalence_check(original_sql[0], original_sql[1], catalog, n_tests=8):
                    logger.debug("Empirical escalation: all %d test DBs agree, promoting to unsat", 8)
                    return WitnessResult(
                        status="unsat",
                        proven_k=max_k,
                        complete=True,
                        solver_time_ms=last_unsat.solver_time_ms,
                    )
            except Exception as e:
                logger.debug("Empirical escalation failed: %s", e)

        last_unsat.proven_k = last_unsat_k
        # FIX.31c: In at-most-k mode, if the full schedule completed and
        # all k values above last_unsat_k only produced discharged spurious
        # SATs (not true UNKNOWNs), the proof IS complete — the spurious
        # SATs don't invalidate the genuine UNSAT below them.
        if at_most_k and last_unsat.status == "unsat":
            last_unsat.complete = True
        else:
            last_unsat.complete = (last_unsat_k == max_k)
        return last_unsat

    if result is not None:
        result.proven_k = max_k
    return result
