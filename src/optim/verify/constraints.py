"""Tracked structural verification with constraint tracking.

Schema validity, type soundness, join FK alignment, grouping legality,
and policy constraints are checked deterministically in Python.  Results
are tracked via Z3's ``assert_and_track`` so that on UNSAT we can extract
a minimal unsat core identifying exactly which constraints failed.

Constraints verified:
  1. Schema validity: all referenced tables/columns exist
  2. Type soundness: operations applied to compatible types
  3. Join validity: join predicates align with FK edges or declared joins
  4. Grouping legality: non-agg projections must be in GROUP BY
  5. Grain constraints: optional many-to-many detection
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import z3

logger = logging.getLogger(__name__)

from ..ir.types import (
    AggCall,
    AggFunc,
    BinOp,
    BinOpKind,
    ColumnRef,
    DerivedTable,
    Expr,
    JoinType,
    Literal,
    QueryIR,
    RelRef,
    SemType,
    _contains_agg,
)
from ..schema.catalog import Catalog
from .encode_z3 import BoundedScope


# ---------------------------------------------------------------------------
# Constraint categories
# ---------------------------------------------------------------------------

class ConstraintKind(str, Enum):
    SCHEMA_VALIDITY = "schema_validity"
    TYPE_SOUNDNESS = "type_soundness"
    JOIN_VALIDITY = "join_validity"
    GROUPING_LEGALITY = "grouping_legality"
    GRAIN_CONSTRAINT = "grain_constraint"
    POLICY = "policy"


@dataclass
class TrackedConstraint:
    """A single tracked assertion with label and metadata."""
    label: str
    kind: ConstraintKind
    description: str
    ir_node: Optional[str] = None  # Which IR node this corresponds to
    satisfied: Optional[bool] = None


@dataclass
class VerificationResult:
    """Result of SMT verification."""
    status: str  # "sat", "unsat", "unknown", "timeout"
    constraints: list[TrackedConstraint]
    unsat_core_labels: list[str] = field(default_factory=list)
    solver_time_ms: float = 0.0
    solver_stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "sat"

    @property
    def certified(self) -> bool:
        return self.status == "sat"

    def failed_constraints(self) -> list[TrackedConstraint]:
        """Return constraints that appear in the unsat core."""
        core_set = set(self.unsat_core_labels)
        return [c for c in self.constraints if c.label in core_set]


# ---------------------------------------------------------------------------
# Set-op chain verifier helper
# ---------------------------------------------------------------------------

def _verify_set_right_chain(
    ir: QueryIR,
    track,
    catalog: Catalog,
    constraints: list[TrackedConstraint],
    *,
    dialect: str = "sqlite",
    depth: int = 0,
) -> None:
    """Recursively verify set_right branches: arity + all constraint categories."""
    if ir.set_right is None:
        return

    # Arity compatibility
    arity_ok = len(ir.select) == len(ir.set_right.select)
    track(
        f"setop_arity_{depth}", arity_ok, ConstraintKind.POLICY,
        f"Set-op branches must have same arity ({len(ir.select)} vs {len(ir.set_right.select)})",
        f"set_op:arity:{depth}",
    )

    # Verify the right branch's own constraints
    right_available = _get_available_tables(ir.set_right)
    prefix = f"setR{depth}_"

    def _track_right(label: str, value: bool, kind: ConstraintKind, desc: str,
                     ir_node: str | None = None) -> None:
        track(f"{prefix}{label}", value, kind, f"[set_right:{depth}] {desc}", ir_node)

    _add_schema_constraints(_track_right, ir.set_right, catalog, right_available, constraints)
    _add_type_constraints(_track_right, ir.set_right, catalog, right_available, constraints, dialect=dialect)
    _add_join_constraints(_track_right, ir.set_right, catalog, constraints, dialect=dialect)
    _add_grouping_constraints(_track_right, ir.set_right, constraints, dialect=dialect)
    _add_policy_constraints(_track_right, ir.set_right, constraints)

    # Recurse for chained set ops
    _verify_set_right_chain(ir.set_right, track, catalog, constraints, dialect=dialect, depth=depth + 1)


# ---------------------------------------------------------------------------
# Main verifier
# ---------------------------------------------------------------------------

def structural_verify(
    ir: QueryIR,
    catalog: Catalog,
    scope: Optional[BoundedScope] = None,
    *,
    dialect: str = "sqlite",
) -> VerificationResult:
    """Run structural constraint verification with tracked assertions.

    Each constraint (schema validity, type soundness, join FK alignment,
    grouping legality, policy) is evaluated deterministically in Python and
    recorded via Z3's ``assert_and_track`` for unsat-core extraction.

    Returns a VerificationResult with tracked constraints.
    If all constraints are satisfiable, the IR is certified.
    If not, the unsat core identifies which constraints fail.
    """
    if scope is None:
        scope = BoundedScope()

    solver = z3.Solver()
    solver.set("timeout", scope.solver_timeout_ms)

    constraints: list[TrackedConstraint] = []
    _used_labels: set[str] = set()
    available_tables = _get_available_tables(ir)

    def _track(label: str, value: bool, kind: ConstraintKind, desc: str,
               ir_node: str | None = None) -> None:
        """Assert a tracked constraint, deduplicating labels."""
        if label in _used_labels:
            return
        _used_labels.add(label)
        tracker = z3.Bool(label)
        solver.assert_and_track(z3.BoolVal(value), tracker)
        constraints.append(TrackedConstraint(
            label=label, kind=kind, description=desc, ir_node=ir_node,
        ))

    # --- Schema validity ---
    _add_schema_constraints(_track, ir, catalog, available_tables, constraints)

    # --- Type soundness ---
    _add_type_constraints(_track, ir, catalog, available_tables, constraints, dialect=dialect)

    # --- Join validity ---
    _add_join_constraints(_track, ir, catalog, constraints, dialect=dialect)

    # --- Grouping legality ---
    _add_grouping_constraints(_track, ir, constraints, dialect=dialect)

    # --- Policy ---
    _add_policy_constraints(_track, ir, constraints)

    # --- Recurse into set_right chain (UNION/INTERSECT/EXCEPT) ---
    _verify_set_right_chain(ir, _track, catalog, constraints, dialect=dialect)

    # Solve
    start = time.monotonic()
    result = solver.check()
    elapsed_ms = (time.monotonic() - start) * 1000

    status = str(result)
    unsat_labels: list[str] = []

    if result == z3.unsat:
        core = solver.unsat_core()
        unsat_labels = [str(c) for c in core]

    # Mark constraints
    core_set = set(unsat_labels)
    for c in constraints:
        if status == "sat":
            c.satisfied = True
        elif c.label in core_set:
            c.satisfied = False
        # else: not in core, we don't know

    stats = {}
    try:
        s = solver.statistics()
        for k in s.keys():
            stats[k] = str(s.get_key_value(k))
    except Exception as e:
        logger.debug("Solver stats extraction failed: %s", e)

    return VerificationResult(
        status=status,
        constraints=constraints,
        unsat_core_labels=unsat_labels,
        solver_time_ms=elapsed_ms,
        solver_stats=stats,
    )


# Backward-compatible alias
smt_verify = structural_verify


# ---------------------------------------------------------------------------
# Schema validity constraints
# ---------------------------------------------------------------------------

def _get_available_tables(ir: QueryIR) -> dict[str, str]:
    """alias/name → actual table name."""
    tables: dict[str, str] = {}

    def _add_rel(rel) -> None:
        if isinstance(rel, RelRef):
            tables[rel.ref_name.lower()] = rel.table.lower()
            tables[rel.table.lower()] = rel.table.lower()
        elif isinstance(rel, DerivedTable):
            tables[rel.alias.lower()] = rel.alias.lower()

    _add_rel(ir.from_table)
    for join in ir.joins:
        _add_rel(join.right)
    return tables


def _get_derived_schemas(ir: QueryIR, catalog: Catalog) -> dict[str, dict[str, SemType]]:
    """Build alias → {col_name → SemType} for DerivedTable nodes in the IR."""
    schemas: dict[str, dict[str, SemType]] = {}

    def _process(rel) -> None:
        if not isinstance(rel, DerivedTable):
            return
        inner = rel.query
        cols: dict[str, SemType] = {}
        inner_available = _get_available_tables(inner)
        for idx, expr in enumerate(inner.select):
            if expr.alias:
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
            sem_type = _resolve_type(expr, catalog, inner_available)
            cols[name.lower()] = sem_type
        schemas[rel.alias.lower()] = cols

    _process(ir.from_table)
    for join in ir.joins:
        _process(join.right)
    return schemas


def _add_schema_constraints(
    track,
    ir: QueryIR,
    catalog: Catalog,
    available: dict[str, str],
    constraints: list[TrackedConstraint],
) -> None:
    """Assert that all table and column references are valid."""
    derived_schemas = _get_derived_schemas(ir, catalog)

    # Table existence (only for base tables, not derived tables)
    all_rels = [ir.from_table] + [j.right for j in ir.joins]
    for rel in all_rels:
        if not isinstance(rel, RelRef):
            continue
        tbl = rel.table
        exists = catalog.get_table(tbl) is not None
        track(
            f"schema_table_{tbl}", exists, ConstraintKind.SCHEMA_VALIDITY,
            f"Table '{tbl}' exists in catalog", f"table:{tbl}",
        )

    # Column existence
    for col_ref in _collect_column_refs(ir):
        actual_table = None
        if col_ref.table:
            actual_table = available.get(col_ref.table.lower())
        else:
            for tbl_name in available.values():
                if catalog.get_column(tbl_name, col_ref.column) is not None:
                    actual_table = tbl_name
                    break
            # Also check derived table schemas for unqualified refs
            if actual_table is None:
                for alias, cols in derived_schemas.items():
                    if col_ref.column.lower() in cols:
                        actual_table = alias
                        break

        # Check if this might be a SELECT alias reference (used in ORDER BY / HAVING)
        if actual_table is None and not col_ref.table:
            select_aliases = {e.alias for e in ir.select if e.alias}
            if col_ref.column in select_aliases:
                continue  # It's a reference to a SELECT alias, skip schema check

        if actual_table is None:
            track(
                f"schema_col_{col_ref.fqn()}", False, ConstraintKind.SCHEMA_VALIDITY,
                f"Cannot resolve table for column '{col_ref.fqn()}'",
                f"col:{col_ref.fqn()}",
            )
            continue

        # For derived tables, validate against derived projection instead of catalog
        if catalog.get_table(actual_table) is None:
            derived_cols = derived_schemas.get(actual_table)
            if derived_cols is not None:
                exists = col_ref.column.lower() in derived_cols
                track(
                    f"schema_col_{actual_table}_{col_ref.column}", exists,
                    ConstraintKind.SCHEMA_VALIDITY,
                    f"Column '{col_ref.column}' {'exists' if exists else 'not found'} in derived table '{actual_table}'",
                    f"col:{actual_table}.{col_ref.column}",
                )
            continue

        col_info = catalog.get_column(actual_table, col_ref.column)
        exists = col_info is not None
        track(
            f"schema_col_{actual_table}_{col_ref.column}", exists,
            ConstraintKind.SCHEMA_VALIDITY,
            f"Column '{col_ref.column}' exists in table '{actual_table}'",
            f"col:{actual_table}.{col_ref.column}",
        )


# ---------------------------------------------------------------------------
# Type soundness constraints
# ---------------------------------------------------------------------------

def _add_type_constraints(
    track,
    ir: QueryIR,
    catalog: Catalog,
    available: dict[str, str],
    constraints: list[TrackedConstraint],
    *,
    dialect: str = "sqlite",
) -> None:
    """Assert type soundness for aggregates and arithmetic."""
    derived_schemas = _get_derived_schemas(ir, catalog)
    idx = 0
    for expr in _collect_all_exprs_flat(ir):
        if isinstance(expr, AggCall) and expr.arg is not None:
            arg_type = _resolve_type(expr.arg, catalog, available, derived_schemas)
            if arg_type == SemType.UNKNOWN:
                idx += 1
                continue

            if expr.func in (AggFunc.SUM, AggFunc.AVG):
                ok = arg_type.is_numeric()
                track(
                    f"type_agg_{expr.func.value}_{idx}", ok,
                    ConstraintKind.TYPE_SOUNDNESS,
                    f"{expr.func.value}() requires numeric, got {arg_type.value}",
                    f"agg:{expr.func.value}",
                )

        elif isinstance(expr, BinOp):
            if expr.op in (BinOpKind.ADD, BinOpKind.SUB, BinOpKind.MUL, BinOpKind.DIV, BinOpKind.MOD):
                lt = _resolve_type(expr.left, catalog, available, derived_schemas)
                rt = _resolve_type(expr.right, catalog, available, derived_schemas)
                if lt != SemType.UNKNOWN and rt != SemType.UNKNOWN:
                    ok = lt.is_numeric() and rt.is_numeric()
                    track(
                        f"type_arith_{expr.op.value}_{idx}", ok,
                        ConstraintKind.TYPE_SOUNDNESS,
                        f"Arithmetic '{expr.op.value}' on {lt.value}, {rt.value}",
                        f"binop:{expr.op.value}",
                    )

            elif expr.op in (BinOpKind.EQ, BinOpKind.NEQ, BinOpKind.LT, BinOpKind.GT, BinOpKind.LTE, BinOpKind.GTE):
                lt = _resolve_type(expr.left, catalog, available, derived_schemas)
                rt = _resolve_type(expr.right, catalog, available, derived_schemas)
                if lt != SemType.UNKNOWN and rt != SemType.UNKNOWN:
                    ok = _types_compatible(lt, rt, dialect=dialect)
                    track(
                        f"type_cmp_{expr.op.value}_{idx}", ok,
                        ConstraintKind.TYPE_SOUNDNESS,
                        f"Comparison '{expr.op.value}' between {lt.value} and {rt.value}",
                        f"cmp:{expr.op.value}",
                    )

        idx += 1


# ---------------------------------------------------------------------------
# Join validity constraints
# ---------------------------------------------------------------------------

def _add_join_constraints(
    track,
    ir: QueryIR,
    catalog: Catalog,
    constraints: list[TrackedConstraint],
    *,
    dialect: str = "sqlite",
) -> None:
    """Assert join predicates align with FK relationships (or explicit allowed joins)."""
    fk_edges = set()
    for fk in catalog.foreign_keys:
        fk_edges.add((fk.src_table.lower(), fk.src_column.lower(),
                       fk.dst_table.lower(), fk.dst_column.lower()))
        fk_edges.add((fk.dst_table.lower(), fk.dst_column.lower(),
                       fk.src_table.lower(), fk.src_column.lower()))

    # Build transitive FK edges: if A.x → C.z and B.y → C.z, allow A.x = B.y
    _add_transitive_fk_edges(fk_edges, catalog)

    available = _get_available_tables(ir)

    for i, join in enumerate(ir.joins):
        on = join.on
        if isinstance(on, BinOp) and on.op == BinOpKind.EQ:
            left_col = _as_column_ref(on.left)
            right_col = _as_column_ref(on.right)

            if left_col and right_col:
                lt = available.get(left_col.table.lower()) if left_col.table else None
                rt = available.get(right_col.table.lower()) if right_col.table else None

                if lt and rt:
                    edge = (lt, left_col.column.lower(), rt, right_col.column.lower())
                    valid = edge in fk_edges
                    if not valid:
                        valid = _is_implicit_join(
                            lt, left_col.column, rt, right_col.column, catalog,
                            dialect=dialect,
                        )
                    track(
                        f"join_fk_{i}_{lt}_{rt}", valid,
                        ConstraintKind.JOIN_VALIDITY,
                        (f"Join {lt}.{left_col.column} = {rt}.{right_col.column} "
                         f"{'aligns with' if valid else 'has no matching'} FK relationship"),
                        f"join:{i}",
                    )


def _add_transitive_fk_edges(fk_edges: set, catalog: Catalog) -> None:
    """Add transitive join edges: if A.x → C.z and B.y → C.z, allow A.x = B.y.

    This handles the common pattern where two tables both reference the same
    target column (e.g. frpm.CDSCode → schools.CDSCode and satscores.cds →
    schools.CDSCode) but have no direct FK between them.
    """
    # Group FK endpoints by destination (table, column)
    from collections import defaultdict
    dst_to_srcs: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for fk in catalog.foreign_keys:
        dst_key = (fk.dst_table.lower(), fk.dst_column.lower())
        src_key = (fk.src_table.lower(), fk.src_column.lower())
        dst_to_srcs[dst_key].append(src_key)
        # Also reverse: src as destination
        dst_to_srcs[src_key].append(dst_key)

    # For each destination column, all sources that point to it can join transitively
    for _dst, srcs in dst_to_srcs.items():
        for i, (t1, c1) in enumerate(srcs):
            for t2, c2 in srcs[i + 1:]:
                if t1 != t2:
                    fk_edges.add((t1, c1, t2, c2))
                    fk_edges.add((t2, c2, t1, c1))


def _tokenize_column_name(name: str) -> set[str]:
    """Split column name into tokens on camelCase and underscore boundaries.

    Examples:
        'setCode'  → {'set', 'code'}
        'converted_mana_cost' → {'converted', 'mana', 'cost'}
        'CDSCode'  → {'cds', 'code'}
    """
    import re

    # Split camelCase: insert boundary before uppercase letters
    parts = re.sub(r"([a-z])([A-Z])", r"\1_\2", name)
    # Also split runs of uppercase followed by lowercase: 'CDSCode' → 'CDS_Code'
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", parts)
    return set(t.lower() for t in parts.split("_") if t)


def _is_implicit_join(
    left_table: str,
    left_col: str,
    right_table: str,
    right_col: str,
    catalog: Catalog,
    *,
    dialect: str = "sqlite",
) -> bool:
    """Allow a join without an explicit FK when columns are plausibly related.

    Two safety guards ensure this heuristic is conservative:
      G1. Type compatibility must hold (same SemType class or safe cast).
      G2. At least one side must be a declared PK (required for all heuristics).

    Heuristics (applied between different tables only):
      1. Same column name on both sides, at least one is PK
      2. One column is a PK and the other's name is a prefix/suffix of it
         (min 4 chars to avoid short tokens like "id" matching everything)
      3. Token-overlap: camelCase/underscore tokens of the smaller name are a
         subset of the larger (e.g. setCode → {set, code} ⊇ code → {code},
         and sets.code is PK).
    """
    if left_table.lower() == right_table.lower():
        return False

    lc = left_col.lower()
    rc = right_col.lower()

    # Guard G1: type compatibility
    left_col_info = catalog.get_column(left_table, left_col)
    right_col_info = catalog.get_column(right_table, right_col)
    if left_col_info and right_col_info:
        if not _types_compatible(left_col_info.sem_type, right_col_info.sem_type, dialect=dialect):
            return False

    # Lookup PK status
    left_tbl_info = catalog.get_table(left_table)
    right_tbl_info = catalog.get_table(right_table)
    left_is_pk = (
        left_tbl_info is not None
        and lc in [p.lower() for p in left_tbl_info.primary_keys]
    )
    right_is_pk = (
        right_tbl_info is not None
        and rc in [p.lower() for p in right_tbl_info.primary_keys]
    )

    # Heuristic 1: identical column name (type guard already passed)
    # Require at least one side to be a PK to avoid unsound joins like orders.id = customers.id
    if lc == rc:
        if left_is_pk or right_is_pk:
            return True

    # Heuristic 2 (Guard G2): prefix/suffix match, PK required on at least one side
    # Only allow prefix/suffix if the shorter name is at least 4 chars
    if left_is_pk or right_is_pk:
        min_len = min(len(lc), len(rc))
        if min_len >= 4 and (lc.endswith(rc) or rc.endswith(lc) or lc.startswith(rc) or rc.startswith(lc)):
            return True

    # Heuristic 3 (Guard G2): token overlap with PK
    if left_is_pk or right_is_pk:
        left_tokens = _tokenize_column_name(left_col)
        right_tokens = _tokenize_column_name(right_col)
        if left_tokens and right_tokens:
            if left_tokens <= right_tokens or right_tokens <= left_tokens:
                return True

    return False


# ---------------------------------------------------------------------------
# Grouping legality constraints
# ---------------------------------------------------------------------------

def _add_grouping_constraints(
    track,
    ir: QueryIR,
    constraints: list[TrackedConstraint],
    *,
    dialect: str = "sqlite",
) -> None:
    """Assert SQL standard grouping rules."""
    # SQLite allows selecting non-aggregated columns not in GROUP BY
    if dialect == "sqlite":
        return
    if not ir.group_by and not ir.has_aggregation():
        return

    gb_keys = {_expr_key(e) for e in ir.group_by}

    for i, expr in enumerate(ir.select):
        if _contains_agg(expr) or isinstance(expr, Literal):
            continue

        key = _expr_key(expr)
        ok = key in gb_keys
        track(
            f"grouping_select_{i}", ok,
            ConstraintKind.GROUPING_LEGALITY,
            (f"Select expr '{_expr_label(expr)}' "
             f"{'is' if ok else 'is NOT'} in GROUP BY"),
            f"select:{i}",
        )


# ---------------------------------------------------------------------------
# Policy constraints
# ---------------------------------------------------------------------------

def _add_policy_constraints(
    track,
    ir: QueryIR,
    constraints: list[TrackedConstraint],
) -> None:
    """Assert policy constraints (no cross joins without explicit, etc.)."""
    for i, join in enumerate(ir.joins):
        if join.join_type == JoinType.CROSS:
            track(
                f"policy_no_cross_join_{i}", False,
                ConstraintKind.POLICY,
                "CROSS JOIN is not permitted by default policy",
                f"join:{i}",
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_column_refs(ir: QueryIR) -> list[ColumnRef]:
    """Collect all ColumnRef nodes from the IR."""
    refs: list[ColumnRef] = []
    for expr in _collect_all_exprs_flat(ir):
        if isinstance(expr, ColumnRef):
            refs.append(expr)
    return refs


def _collect_all_exprs_flat(ir: QueryIR) -> list[Expr]:
    """Collect all expression nodes, recursively flattened."""
    from .fast_checks import _flatten_expr
    exprs: list[Expr] = []
    for e in ir.select:
        exprs.extend(_flatten_expr(e))
    if ir.where:
        exprs.extend(_flatten_expr(ir.where))
    for j in ir.joins:
        exprs.extend(_flatten_expr(j.on))
    for g in ir.group_by:
        exprs.extend(_flatten_expr(g))
    if ir.having:
        exprs.extend(_flatten_expr(ir.having))
    for s in ir.order_by:
        exprs.extend(_flatten_expr(s.expr))
    return exprs


def _as_column_ref(expr: Expr) -> Optional[ColumnRef]:
    if isinstance(expr, ColumnRef):
        return expr
    return None


def _resolve_type(expr: Expr, catalog: Catalog, available: dict[str, str],
                  derived_schemas: dict[str, dict[str, SemType]] | None = None) -> SemType:
    """Resolve the type of an expression."""
    if expr.sem_type != SemType.UNKNOWN:
        return expr.sem_type
    if isinstance(expr, ColumnRef):
        actual = None
        if expr.table:
            actual = available.get(expr.table.lower())
        else:
            for tbl in available.values():
                if catalog.get_column(tbl, expr.column):
                    actual = tbl
                    break
        if actual:
            col = catalog.get_column(actual, expr.column)
            if col:
                return col.sem_type
            if derived_schemas:
                derived_cols = derived_schemas.get(actual)
                if derived_cols:
                    dtype = derived_cols.get(expr.column.lower())
                    if dtype:
                        return dtype
        else:
            if derived_schemas:
                for alias, cols in derived_schemas.items():
                    if expr.column.lower() in cols:
                        return cols[expr.column.lower()]
    if isinstance(expr, Literal):
        return expr.sem_type
    if isinstance(expr, AggCall):
        if expr.func == AggFunc.COUNT:
            return SemType.INT
        if expr.arg:
            return _resolve_type(expr.arg, catalog, available, derived_schemas)
    return SemType.UNKNOWN


def _types_compatible(a: SemType, b: SemType, dialect: str = "sqlite") -> bool:
    if a == b:
        return True
    if a.is_numeric() and b.is_numeric():
        return True
    if a.is_temporal() and b.is_temporal():
        return True
    # DATE/TIMESTAMP columns are commonly compared with string literals
    if (a.is_temporal() and b == SemType.STRING) or (b.is_temporal() and a == SemType.STRING):
        return True
    # SQLite implicit coercion: string columns can be compared with numeric literals
    if dialect == "sqlite":
        if (a == SemType.STRING and b.is_numeric()) or (b == SemType.STRING and a.is_numeric()):
            return True
    return False


def _expr_key(expr: Expr) -> str:
    if isinstance(expr, ColumnRef):
        t = (expr.table or "").lower()
        return f"{t}.{expr.column.lower()}"
    if isinstance(expr, Literal):
        return f"lit:{expr.value!r}"
    return repr(expr)


def _expr_label(expr: Expr) -> str:
    if isinstance(expr, ColumnRef):
        return expr.fqn()
    if isinstance(expr, AggCall):
        return f"{expr.func.value}(...)"
    return type(expr).__name__
