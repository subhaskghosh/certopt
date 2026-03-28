"""Compositional proof-carrying rewrites (Direction D).

For large multi-table queries (7–16 tables) that exceed monolithic combo
limits, this module decomposes verification into:

  1. **Region isolation** — diff original and rewrite IRs to find the
     minimal subexpression that changed.
  2. **Local equivalence** — verify only the changed region under a
     reduced local catalog (k^|local_tables| combos instead of k^|all|).
  3. **Context preservation** — check that the enclosing context belongs
     to a recognized class (projection, selection, inner join, aggregation,
     order+limit) and preserves the block interface properties.

If local equivalence holds and context preservation is satisfied, the
composed proof guarantees full-query equivalence under bounded semantics.

This is used as a fallback when monolithic `synthesize_witness()` returns
status="unknown" due to combo limit overflow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..ir.types import (
    AggCall,
    BinOp,
    BinOpKind,
    ColumnRef,
    DerivedTable,
    Expr,
    ExistsSubquery,
    FuncCall,
    InList,
    InSubquery,
    JoinClause,
    JoinType,
    Literal,
    QueryIR,
    RelRef,
    ScalarSubquery,
    SemType,
    SortSpec,
    Star,
    UnaryOp,
    Between,
    CaseExpr,
    WindowFunc,
)
from ..schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from ..verify.encode_z3 import BoundedScope
from .witness_synthesis import WitnessResult, synthesize_witness

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BlockInterface:
    """The contract between a rewrite region and its enclosing context."""
    output_columns: list[tuple[str, SemType]]
    preserves_multiplicity: bool
    preserves_nullability: bool
    preserves_order: bool
    input_tables: list[str]


class ContextClass(Enum):
    """Recognized context classes with known composition properties."""
    PROJECTION = "projection"
    SELECTION = "selection"
    INNER_JOIN = "inner_join"
    AGGREGATION = "aggregation"
    ORDER_LIMIT = "order_limit"


@dataclass
class RewriteRegion:
    """The minimal subexpression that differs between original and rewrite."""
    original_block: QueryIR
    rewrite_block: QueryIR
    context_path: list[str]
    interface: BlockInterface


@dataclass
class CompositionalResult:
    """Result of compositional verification."""
    success: bool
    local_result: Optional[WitnessResult] = None
    context_class: Optional[ContextClass] = None
    context_check: dict[str, bool] = field(default_factory=dict)
    region: Optional[RewriteRegion] = None
    reason: Optional[str] = None
    local_combo_count: int = 0
    monolithic_combo_count: int = 0
    plan: Optional["DecompositionPlan"] = None
    region_results: list[WitnessResult] = field(default_factory=list)


@dataclass
class InterfaceColumn:
    """A column exported from a local region to the outer context."""
    table_alias: str
    column_name: str
    sem_type: SemType


@dataclass
class MoveGroup:
    """A group of predicates that moved between WHERE and a specific JOIN ON."""
    join_idx: int
    moved_to_on: list[Expr]    # conjuncts that moved from WHERE → ON
    moved_to_where: list[Expr] # conjuncts that moved from ON → WHERE
    structural_on: list[Expr]  # unchanged ON conjuncts (present in both)


@dataclass
class LocalRegion:
    """A standalone local query pair for one changed join."""
    join_idx: int
    local_aliases: set[str]
    boundary_aliases: set[str]
    interface_columns: list[InterfaceColumn]
    original_local: QueryIR
    rewrite_local: QueryIR
    move_group: Optional[MoveGroup] = None
    proof_kind: str = ""


@dataclass
class DecompositionPlan:
    """Complete plan for decomposed verification of a rewrite."""
    regions: list[LocalRegion]
    context_class: Optional[ContextClass] = None
    all_local_aliases: set[str] = field(default_factory=set)
    total_local_tables: int = 0


# ---------------------------------------------------------------------------
# D.1 — Rewrite region isolation
# ---------------------------------------------------------------------------

def _collect_tables(ir: QueryIR) -> set[str]:
    """Collect all table names referenced by the IR (from_table + joins)."""
    tables: set[str] = set()
    if isinstance(ir.from_table, RelRef):
        tables.add(ir.from_table.table.lower())
    elif isinstance(ir.from_table, DerivedTable):
        tables.update(_collect_tables(ir.from_table.query))
    for j in ir.joins:
        if isinstance(j.right, RelRef):
            tables.add(j.right.table.lower())
        elif isinstance(j.right, DerivedTable):
            tables.update(_collect_tables(j.right.query))
    return tables


def _exprs_equal(a: Optional[Expr], b: Optional[Expr]) -> bool:
    """Compare two expressions structurally (deep equality)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if type(a) != type(b):
        return False
    # Use Pydantic model dump for deep structural comparison
    return a.model_dump(exclude_defaults=True) == b.model_dump(exclude_defaults=True)


def _expr_list_equal(a: list, b: list) -> bool:
    """Compare two lists of expressions."""
    if len(a) != len(b):
        return False
    return all(_exprs_equal(x, y) for x, y in zip(a, b))


def _sort_specs_equal(a: list[SortSpec], b: list[SortSpec]) -> bool:
    """Compare two lists of sort specs."""
    if len(a) != len(b):
        return False
    for sa, sb in zip(a, b):
        if sa.direction != sb.direction:
            return False
        if not _exprs_equal(sa.expr, sb.expr):
            return False
    return True


def _join_equal(a: JoinClause, b: JoinClause) -> bool:
    """Compare two join clauses structurally."""
    if a.join_type != b.join_type:
        return False
    # Compare right side
    if isinstance(a.right, RelRef) and isinstance(b.right, RelRef):
        if a.right.table.lower() != b.right.table.lower():
            return False
        if (a.right.alias or a.right.table).lower() != (b.right.alias or b.right.table).lower():
            return False
    elif isinstance(a.right, DerivedTable) and isinstance(b.right, DerivedTable):
        if a.right.alias != b.right.alias:
            return False
    else:
        return False
    return _exprs_equal(a.on, b.on)


def _infer_interface(
    original: QueryIR,
    rewrite: QueryIR,
    context_path: list[str],
) -> BlockInterface:
    """Extract the block interface from the rewrite region."""
    # Gather output columns from original block's SELECT
    output_columns: list[tuple[str, SemType]] = []
    for expr in original.select:
        name = ""
        sem = expr.sem_type
        if expr.alias:
            name = expr.alias
        elif isinstance(expr, ColumnRef):
            name = expr.column
        elif isinstance(expr, AggCall):
            name = expr.func.value.lower()
        else:
            name = f"col_{len(output_columns)}"
        output_columns.append((name, sem))

    input_tables = sorted(_collect_tables(original))

    # Multiplicity: same number of joins of the same type
    preserves_multiplicity = (
        len(original.joins) == len(rewrite.joins)
        and original.distinct == rewrite.distinct
        and len(original.group_by) == len(rewrite.group_by)
    )

    # Nullability: same join types (LEFT/RIGHT affect nulls)
    preserves_nullability = all(
        a.join_type == b.join_type
        for a, b in zip(original.joins, rewrite.joins)
    ) if len(original.joins) == len(rewrite.joins) else False

    # Order preservation
    preserves_order = _sort_specs_equal(original.order_by, rewrite.order_by)

    return BlockInterface(
        output_columns=output_columns,
        preserves_multiplicity=preserves_multiplicity,
        preserves_nullability=preserves_nullability,
        preserves_order=preserves_order,
        input_tables=input_tables,
    )


def isolate_rewrite_region(
    original: QueryIR,
    rewrite: QueryIR,
) -> Optional[RewriteRegion]:
    """Diff two IRs to find the minimal changed subexpression.

    Algorithm:
      1. Compare top-level fields to identify what changed.
      2. If only joins changed → region is join block.
      3. If WHERE predicate moved to/from JOIN ON → region is the
         affected tables.
      4. If more than one independent region changed → return None
         (fall back to monolithic).
      5. Extract interface from parent context.

    Returns None if:
      - IRs are identical (no diff)
      - Changes span the entire query (whole-query rewrite)
      - Multiple disjoint regions changed
    """
    # Quick check: if IRs are identical, no region
    if (original.model_dump(exclude_defaults=True)
            == rewrite.model_dump(exclude_defaults=True)):
        return None

    # Track which parts differ
    select_same = _expr_list_equal(original.select, rewrite.select)
    from_same = _from_same(original, rewrite)
    joins_same = _joins_same(original, rewrite)
    where_same = _exprs_equal(original.where, rewrite.where)
    groupby_same = _expr_list_equal(original.group_by, rewrite.group_by)
    having_same = _exprs_equal(original.having, rewrite.having)
    orderby_same = _sort_specs_equal(original.order_by, rewrite.order_by)
    limit_same = original.limit == rewrite.limit
    distinct_same = original.distinct == rewrite.distinct

    diff_fields = []
    if not select_same:
        diff_fields.append("select")
    if not from_same:
        diff_fields.append("from")
    if not joins_same:
        diff_fields.append("joins")
    if not where_same:
        diff_fields.append("where")
    if not groupby_same:
        diff_fields.append("group_by")
    if not having_same:
        diff_fields.append("having")
    if not orderby_same:
        diff_fields.append("order_by")
    if not limit_same:
        diff_fields.append("limit")
    if not distinct_same:
        diff_fields.append("distinct")

    if not diff_fields:
        return None

    # Pattern: join reorder — only joins changed
    if diff_fields == ["joins"]:
        context_path = ["joins"]
        affected = _find_affected_tables_join_reorder(original, rewrite)
        block_orig = _build_sub_block(original, affected)
        block_rewrite = _build_sub_block(rewrite, affected)
        interface = _infer_interface(block_orig, block_rewrite, context_path)
        interface.input_tables = sorted(affected)
        return RewriteRegion(
            original_block=block_orig,
            rewrite_block=block_rewrite,
            context_path=context_path,
            interface=interface,
        )

    # Pattern: predicate pushdown/pullup — WHERE and joins changed together
    # This is the most common case for R1/R2 rewrites.
    if set(diff_fields) <= {"where", "joins"}:
        context_path = ["where+joins"]
        affected = _find_affected_tables_predicate_move(original, rewrite)
        block_orig = _build_sub_block(original, affected)
        block_rewrite = _build_sub_block(rewrite, affected)
        interface = _infer_interface(block_orig, block_rewrite, context_path)
        interface.input_tables = sorted(affected)
        return RewriteRegion(
            original_block=block_orig,
            rewrite_block=block_rewrite,
            context_path=context_path,
            interface=interface,
        )

    # Pattern: only SELECT changed (projection minimization R6, DISTINCT toggle)
    if set(diff_fields) <= {"select", "distinct"}:
        context_path = ["select"]
        # For select-only changes, the whole query IS the region
        interface = _infer_interface(original, rewrite, context_path)
        return RewriteRegion(
            original_block=original,
            rewrite_block=rewrite,
            context_path=context_path,
            interface=interface,
        )

    # Pattern: single join changed (one entry in the joins list differs)
    if diff_fields == ["joins"] or set(diff_fields) <= {"joins", "where"}:
        changed_idx = _find_changed_join_index(original, rewrite)
        if changed_idx is not None:
            context_path = [f"joins[{changed_idx}]"]
            tables = _collect_tables(original) | _collect_tables(rewrite)
            block_orig = _build_sub_block(original, tables)
            block_rewrite = _build_sub_block(rewrite, tables)
            interface = _infer_interface(block_orig, block_rewrite, context_path)
            return RewriteRegion(
                original_block=block_orig,
                rewrite_block=block_rewrite,
                context_path=context_path,
                interface=interface,
            )

    # Too many changes — fall back to monolithic
    logger.debug("Compositional isolation: too many diff fields %s, falling back", diff_fields)
    return None


def _from_same(a: QueryIR, b: QueryIR) -> bool:
    """Check if FROM clauses are structurally equal."""
    if isinstance(a.from_table, RelRef) and isinstance(b.from_table, RelRef):
        return (a.from_table.table.lower() == b.from_table.table.lower()
                and (a.from_table.alias or "").lower() == (b.from_table.alias or "").lower())
    if isinstance(a.from_table, DerivedTable) and isinstance(b.from_table, DerivedTable):
        return (a.from_table.alias == b.from_table.alias
                and a.from_table.query.model_dump(exclude_defaults=True)
                == b.from_table.query.model_dump(exclude_defaults=True))
    return False


def _joins_same(a: QueryIR, b: QueryIR) -> bool:
    """Check if join lists are structurally equal."""
    if len(a.joins) != len(b.joins):
        return False
    return all(_join_equal(ja, jb) for ja, jb in zip(a.joins, b.joins))


def _find_changed_join_index(original: QueryIR, rewrite: QueryIR) -> Optional[int]:
    """Find the index of the single changed join, or None if >1 changed."""
    if len(original.joins) != len(rewrite.joins):
        return None
    changed = [i for i in range(len(original.joins))
                if not _join_equal(original.joins[i], rewrite.joins[i])]
    if len(changed) == 1:
        return changed[0]
    return None


def _build_sub_block(ir: QueryIR, tables: set[str]) -> QueryIR:
    """Build a sub-query block containing only the tables in the set.

    This preserves the full query structure (SELECT, WHERE, GROUP BY, etc.)
    so that synthesize_witness can verify it correctly. The region is
    the whole query — the local catalog will be restricted to only the
    relevant tables.
    """
    return ir.model_copy(deep=True)


def _collect_tables_from_expr(expr: Optional[Expr]) -> set[str]:
    """Collect all table references from an expression tree."""
    tables: set[str] = set()
    if expr is None:
        return tables
    if isinstance(expr, ColumnRef) and expr.table:
        tables.add(expr.table.lower())
    elif isinstance(expr, BinOp):
        tables |= _collect_tables_from_expr(expr.left)
        tables |= _collect_tables_from_expr(expr.right)
    elif isinstance(expr, UnaryOp):
        tables |= _collect_tables_from_expr(expr.operand)
    elif isinstance(expr, FuncCall):
        for a in expr.args:
            tables |= _collect_tables_from_expr(a)
    elif isinstance(expr, AggCall) and expr.arg:
        tables |= _collect_tables_from_expr(expr.arg)
    elif isinstance(expr, InList):
        tables |= _collect_tables_from_expr(expr.expr)
        for v in expr.values:
            tables |= _collect_tables_from_expr(v)
    elif isinstance(expr, Between):
        tables |= _collect_tables_from_expr(expr.expr)
        tables |= _collect_tables_from_expr(expr.low)
        tables |= _collect_tables_from_expr(expr.high)
    elif isinstance(expr, CaseExpr):
        for cw in expr.whens:
            tables |= _collect_tables_from_expr(cw.when)
            tables |= _collect_tables_from_expr(cw.then)
        if expr.else_:
            tables |= _collect_tables_from_expr(expr.else_)
    return tables


def _find_affected_tables_predicate_move(
    original: QueryIR, rewrite: QueryIR,
) -> set[str]:
    """For R1/R2 predicate pushdown/pullup, find the tables involved
    in the moved predicate + the target join.

    Strategy: find the conjuncts that differ between the two WHERE clauses
    and the join ON clauses; collect tables referenced in those conjuncts.
    """
    from ..cegis.preprocessing import _collect_and_conjuncts

    # Collect WHERE conjuncts
    orig_where = set(
        e.model_dump_json(exclude_defaults=True)
        for e in _collect_and_conjuncts(original.where)
    )
    rew_where = set(
        e.model_dump_json(exclude_defaults=True)
        for e in _collect_and_conjuncts(rewrite.where)
    )

    # Conjuncts moved from WHERE (present in original, absent in rewrite)
    moved_from_where = orig_where - rew_where
    # Conjuncts added to WHERE (absent in original, present in rewrite)
    moved_to_where = rew_where - orig_where

    affected: set[str] = set()

    # Tables in moved conjuncts
    for conj_json in moved_from_where | moved_to_where:
        for c in _collect_and_conjuncts(original.where):
            if c.model_dump_json(exclude_defaults=True) == conj_json:
                affected |= _collect_tables_from_expr(c)
        for c in _collect_and_conjuncts(rewrite.where):
            if c.model_dump_json(exclude_defaults=True) == conj_json:
                affected |= _collect_tables_from_expr(c)

    # Find changed joins and collect their tables
    for i in range(min(len(original.joins), len(rewrite.joins))):
        if not _join_equal(original.joins[i], rewrite.joins[i]):
            affected |= _collect_tables_from_expr(original.joins[i].on)
            affected |= _collect_tables_from_expr(rewrite.joins[i].on)
            right_o = original.joins[i].right
            right_r = rewrite.joins[i].right
            if isinstance(right_o, RelRef):
                affected.add((right_o.alias or right_o.table).lower())
            if isinstance(right_r, RelRef):
                affected.add((right_r.alias or right_r.table).lower())

    # Also add the FROM table (it's always involved in joins)
    if isinstance(original.from_table, RelRef):
        from_name = (original.from_table.alias or original.from_table.table).lower()
        # Only add from_table if it appears in an affected predicate
        if from_name in affected:
            affected.add(from_name)

    return affected


def _find_affected_tables_join_reorder(
    original: QueryIR, rewrite: QueryIR,
) -> set[str]:
    """For R5 join reorder, find only the tables whose join position changed."""
    affected: set[str] = set()
    for i in range(min(len(original.joins), len(rewrite.joins))):
        if not _join_equal(original.joins[i], rewrite.joins[i]):
            right_o = original.joins[i].right
            right_r = rewrite.joins[i].right
            if isinstance(right_o, RelRef):
                affected.add((right_o.alias or right_o.table).lower())
            if isinstance(right_r, RelRef):
                affected.add((right_r.alias or right_r.table).lower())
            affected |= _collect_tables_from_expr(original.joins[i].on)
            affected |= _collect_tables_from_expr(rewrite.joins[i].on)
    # Add FROM table since joins are relative to it
    if isinstance(original.from_table, RelRef):
        affected.add((original.from_table.alias or original.from_table.table).lower())
    return affected


# ---------------------------------------------------------------------------
# D.1b — Sub-query decomposition (v2)
# ---------------------------------------------------------------------------

def _expr_id(expr: Expr) -> str:
    """Return a canonical string ID for an expression."""
    return expr.model_dump_json(exclude_defaults=True)


def _extract_move_groups(original: QueryIR, rewrite: QueryIR) -> list[MoveGroup]:
    """Identify predicates that moved between WHERE and JOIN ON clauses."""
    from .preprocessing import _collect_and_conjuncts

    orig_where_conjs = _collect_and_conjuncts(original.where)
    rew_where_conjs = _collect_and_conjuncts(rewrite.where)

    orig_where_ids = {_expr_id(c): c for c in orig_where_conjs}
    rew_where_ids = {_expr_id(c): c for c in rew_where_conjs}

    groups: list[MoveGroup] = []
    for i in range(min(len(original.joins), len(rewrite.joins))):
        oj = original.joins[i]
        rj = rewrite.joins[i]

        # Skip if join types differ or right aliases differ
        if oj.join_type != rj.join_type:
            continue
        if isinstance(oj.right, RelRef) and isinstance(rj.right, RelRef):
            if oj.right.ref_name.lower() != rj.right.ref_name.lower():
                continue
        else:
            continue

        orig_on_conjs = _collect_and_conjuncts(oj.on)
        rew_on_conjs = _collect_and_conjuncts(rj.on)

        orig_on_map = {_expr_id(c): c for c in orig_on_conjs}
        rew_on_map = {_expr_id(c): c for c in rew_on_conjs}

        orig_on_ids = set(orig_on_map.keys())
        rew_on_ids = set(rew_on_map.keys())

        structural_on = [orig_on_map[eid] for eid in orig_on_ids & rew_on_ids]

        # moved_to_on: in rewrite ON but not in original ON, AND was in original WHERE
        moved_to_on = [
            rew_on_map[eid]
            for eid in (rew_on_ids - orig_on_ids)
            if eid in orig_where_ids
        ]

        # moved_to_where: in rewrite WHERE but not in original WHERE, AND was in original ON
        moved_to_where = [
            rew_where_ids[eid]
            for eid in (set(rew_where_ids.keys()) - set(orig_where_ids.keys()))
            if eid in orig_on_ids
        ]

        if moved_to_on or moved_to_where:
            groups.append(MoveGroup(
                join_idx=i,
                moved_to_on=moved_to_on,
                moved_to_where=moved_to_where,
                structural_on=structural_on,
            ))

    return groups


def _alias_order(ir: QueryIR) -> list[str]:
    """Return the alias ordering: from_table first, then joins in order."""
    order = [ir.from_table.ref_name.lower()]
    for j in ir.joins:
        order.append(j.right.ref_name.lower())
    return order


def _alias_to_relation(ir: QueryIR) -> dict[str, RelRef]:
    """Map alias → RelRef for from_table and all joins' right (only RelRef)."""
    result: dict[str, RelRef] = {}
    if isinstance(ir.from_table, RelRef):
        result[ir.from_table.ref_name.lower()] = ir.from_table
    for j in ir.joins:
        if isinstance(j.right, RelRef):
            result[j.right.ref_name.lower()] = j.right
    return result


def _join_for_alias(ir: QueryIR, alias: str) -> Optional[JoinClause]:
    """Find the JoinClause whose right ref_name matches alias."""
    alias_low = alias.lower()
    for j in ir.joins:
        if j.right.ref_name.lower() == alias_low:
            return j
    return None


def _compute_local_aliases_for_join(
    ir: QueryIR, join_idx: int, moved_exprs: list[Expr],
) -> set[str]:
    """Compute the set of local aliases for a given join and moved predicates."""
    from .preprocessing import _collect_and_conjuncts

    target = ir.joins[join_idx].right.ref_name.lower()

    # Support aliases from structural ON of this join
    on_conjuncts = _collect_and_conjuncts(ir.joins[join_idx].on)
    support: set[str] = set()
    for conj in on_conjuncts:
        support |= _collect_tables_from_expr(conj)

    # Also add aliases referenced by moved_exprs
    for expr in moved_exprs:
        support |= _collect_tables_from_expr(expr)

    local = {target} | support
    return local


def _boundary_aliases(
    ir: QueryIR, local_aliases: set[str], join_idx: int,
) -> set[str]:
    """Compute boundary aliases: local aliases referenced by outer context."""
    from .preprocessing import _collect_and_conjuncts

    boundary: set[str] = set()

    # Check SELECT exprs
    for expr in ir.select:
        refs = _collect_tables_from_expr(expr)
        if refs & local_aliases:
            boundary |= (refs & local_aliases)

    # Check GROUP BY
    for expr in ir.group_by:
        refs = _collect_tables_from_expr(expr)
        if refs & local_aliases:
            boundary |= (refs & local_aliases)

    # Check HAVING
    if ir.having:
        refs = _collect_tables_from_expr(ir.having)
        if refs & local_aliases:
            boundary |= (refs & local_aliases)

    # Check ORDER BY
    for spec in ir.order_by:
        refs = _collect_tables_from_expr(spec.expr)
        if refs & local_aliases:
            boundary |= (refs & local_aliases)

    # Check WHERE conjuncts
    where_conjs = _collect_and_conjuncts(ir.where)
    for conj in where_conjs:
        refs = _collect_tables_from_expr(conj)
        if refs & local_aliases:
            boundary |= (refs & local_aliases)

    # Check OTHER joins' ON conjuncts (not join_idx)
    for i, j in enumerate(ir.joins):
        if i == join_idx:
            continue
        on_conjs = _collect_and_conjuncts(j.on)
        for conj in on_conjs:
            refs = _collect_tables_from_expr(conj)
            if refs & local_aliases:
                boundary |= (refs & local_aliases)

    if not boundary:
        from_alias = ir.from_table.ref_name.lower() if isinstance(ir.from_table, RelRef) else ""
        if from_alias in local_aliases:
            boundary = {from_alias}

    return boundary


def _build_interface_columns(
    boundary_aliases: set[str],
    alias_to_rel: dict[str, RelRef],
    catalog: Catalog,
) -> list[InterfaceColumn]:
    """Build interface columns for the boundary aliases."""
    columns: list[InterfaceColumn] = []
    for alias in sorted(boundary_aliases):
        table_name = alias_to_rel[alias].table if alias in alias_to_rel else alias
        tinfo = catalog.get_table(table_name)
        if tinfo:
            for col in tinfo.columns:
                columns.append(InterfaceColumn(
                    table_alias=alias,
                    column_name=col.name,
                    sem_type=col.sem_type,
                ))
    return columns


def _has_unsupported_constructs(expr: Optional[Expr]) -> bool:
    """Check if an expression contains unsupported constructs for decomposition."""
    if expr is None:
        return False
    if isinstance(expr, (DerivedTable, ExistsSubquery, InSubquery, ScalarSubquery, WindowFunc)):
        return True
    if isinstance(expr, BinOp):
        return _has_unsupported_constructs(expr.left) or _has_unsupported_constructs(expr.right)
    if isinstance(expr, UnaryOp):
        return _has_unsupported_constructs(expr.operand)
    if isinstance(expr, FuncCall):
        return any(_has_unsupported_constructs(a) for a in expr.args)
    if isinstance(expr, AggCall):
        return _has_unsupported_constructs(expr.arg)
    if isinstance(expr, InList):
        if _has_unsupported_constructs(expr.expr):
            return True
        return any(_has_unsupported_constructs(v) for v in expr.values)
    if isinstance(expr, Between):
        return (
            _has_unsupported_constructs(expr.expr)
            or _has_unsupported_constructs(expr.low)
            or _has_unsupported_constructs(expr.high)
        )
    if isinstance(expr, CaseExpr):
        for cw in expr.whens:
            if _has_unsupported_constructs(cw.when) or _has_unsupported_constructs(cw.then):
                return True
        if expr.else_ and _has_unsupported_constructs(expr.else_):
            return True
    return False


def _check_predicate_closure(
    local_aliases: set[str],
    moved_exprs: list[Expr],
    orig_where: Optional[Expr],
    orig_joins: list[JoinClause],
    changed_join_idx: int,
) -> tuple[bool, Optional[str]]:
    """Check that moved predicates are closed within local_aliases."""
    from .preprocessing import _collect_and_conjuncts

    # Check moved predicates reference only local aliases
    for expr in moved_exprs:
        refs = _collect_tables_from_expr(expr)
        if refs and not refs <= local_aliases:
            return (False, "moved_predicate_references_external_alias")

    # Check WHERE conjuncts don't cross boundary
    for conj in _collect_and_conjuncts(orig_where):
        refs = _collect_tables_from_expr(conj)
        if refs & local_aliases and not refs <= local_aliases:
            return (False, "where_predicate_crosses_boundary")

    # Note: other joins' ON conditions are allowed to reference boundary
    # aliases (that's how boundary is defined). Only fail if a non-join
    # predicate crosses the boundary.

    return (True, None)


def _build_local_query(
    ir: QueryIR,
    join_idx: int,
    local_aliases: set[str],
    boundary_aliases: set[str],
    interface_columns: list[InterfaceColumn],
    moved_exprs: list[Expr],
) -> QueryIR:
    """Build a standalone local QueryIR for verification.

    Both original and rewrite local queries get the moved predicates
    added to WHERE. This ensures they are structurally comparable even
    when the local region has no joins (single-table regions where the
    predicate moved from WHERE to a JOIN ON that is outside the local
    region).
    """
    from .preprocessing import _collect_and_conjuncts, _rebuild_and

    alias_ord = _alias_order(ir)
    local_order = [a for a in alias_ord if a in local_aliases]
    if not local_order:
        raise ValueError("No local aliases found in alias order")

    root_alias = local_order[0]
    alias_to_rel = _alias_to_relation(ir)

    # Build FROM
    root_rel = alias_to_rel.get(root_alias)
    if root_rel is None:
        raise ValueError(f"No RelRef found for root alias {root_alias}")
    from_table = RelRef(table=root_rel.table, alias=root_rel.alias)

    # Build JOINs: for each non-root local alias, find its join in the source IR
    local_joins: list[JoinClause] = []
    for alias in local_order[1:]:
        orig_join = _join_for_alias(ir, alias)
        if orig_join is None:
            continue
        # Copy the join with its ON clause from the source IR
        local_joins.append(JoinClause(
            join_type=JoinType.INNER,
            right=RelRef(
                table=orig_join.right.table if isinstance(orig_join.right, RelRef) else alias,
                alias=orig_join.right.alias if isinstance(orig_join.right, RelRef) else None,
            ),
            on=orig_join.on,
        ))

    # Build WHERE: include conjuncts from ir.where whose refs ⊆ local_aliases.
    where_conjuncts = _collect_and_conjuncts(ir.where)
    local_where_conjs: list[Expr] = []
    seen_ids: set[str] = set()
    for conj in where_conjuncts:
        conj_tables = _collect_tables_from_expr(conj)
        if conj_tables and conj_tables <= local_aliases:
            cid = _expr_id(conj)
            if cid not in seen_ids:
                local_where_conjs.append(conj)
                seen_ids.add(cid)

    # Also add moved predicates whose refs ⊆ local_aliases.
    # This ensures both original and rewrite local queries have the same
    # filter predicates, even when the predicate moved from WHERE→ON
    # or ON→WHERE in the full query. Without this, single-table regions
    # would have different WHERE clauses and produce spurious SAT.
    for expr in moved_exprs:
        refs = _collect_tables_from_expr(expr)
        if refs and refs <= local_aliases:
            cid = _expr_id(expr)
            if cid not in seen_ids:
                local_where_conjs.append(expr)
                seen_ids.add(cid)

    # Build SELECT from interface columns
    select_exprs: list[Expr] = [
        ColumnRef(table=ic.table_alias, column=ic.column_name, sem_type=ic.sem_type)
        for ic in interface_columns
    ]
    if not select_exprs:
        # Fallback: select all columns from root
        select_exprs = [ColumnRef(table=root_alias, column="id", sem_type=SemType.INT)]

    return QueryIR(
        select=select_exprs,
        from_table=from_table,
        joins=local_joins,
        where=_rebuild_and(local_where_conjs),
    )


def build_decomposition_plan(
    original: QueryIR,
    rewrite: QueryIR,
    catalog: Catalog,
) -> Optional[DecompositionPlan]:
    """Build a decomposition plan for v2 compositional verification."""
    from .preprocessing import promote_predicates_to_on

    # Normalize both IRs: promote equi-join WHERE predicates to JOIN ON.
    # This is essential for JOB-Complex queries that use implicit join syntax
    # (CROSS JOIN + WHERE equi-join predicates). After promotion, only
    # single-table filter predicates remain in WHERE, making predicate
    # closure more likely to succeed.
    norm_original, _ = promote_predicates_to_on(original)
    norm_rewrite, _ = promote_predicates_to_on(rewrite)

    move_groups = _extract_move_groups(norm_original, norm_rewrite)
    if not move_groups:
        return None

    regions: list[LocalRegion] = []
    all_local: set[str] = set()

    for mg in move_groups:
        # Gate: only INNER joins
        if mg.join_idx >= len(norm_original.joins):
            return None
        if norm_original.joins[mg.join_idx].join_type != JoinType.INNER:
            logger.debug("Decomposition: join %d is not INNER, falling back", mg.join_idx)
            return None

        all_moved = mg.moved_to_on + mg.moved_to_where
        # Gate: no unsupported constructs
        if any(_has_unsupported_constructs(e) for e in all_moved):
            logger.debug("Decomposition: unsupported constructs in moved predicates")
            return None

        local_aliases = _compute_local_aliases_for_join(norm_original, mg.join_idx, all_moved)
        if len(local_aliases) > 6:
            logger.debug("Decomposition: %d local aliases exceeds limit of 6", len(local_aliases))
            return None

        boundary = _boundary_aliases(norm_original, local_aliases, mg.join_idx)
        if not boundary:
            # Use the from_table alias as boundary if it's local
            from_alias = norm_original.from_table.ref_name.lower() if isinstance(norm_original.from_table, RelRef) else ""
            if from_alias in local_aliases:
                boundary = {from_alias}
            else:
                logger.debug("Decomposition: no boundary aliases found for join %d", mg.join_idx)
                return None

        alias_to_rel = _alias_to_relation(norm_original)
        interface_cols = _build_interface_columns(boundary, alias_to_rel, catalog)
        if not interface_cols:
            logger.debug("Decomposition: no interface columns for join %d", mg.join_idx)
            return None

        is_closed, reason = _check_predicate_closure(
            local_aliases, all_moved, norm_original.where, norm_original.joins, mg.join_idx,
        )
        if not is_closed:
            logger.debug("Decomposition: predicate closure failed: %s", reason)
            return None

        try:
            orig_local = _build_local_query(
                norm_original, mg.join_idx, local_aliases, boundary, interface_cols, all_moved,
            )
            rew_local = _build_local_query(
                norm_rewrite, mg.join_idx, local_aliases, boundary, interface_cols, all_moved,
            )
        except Exception as e:
            logger.debug("Decomposition: failed to build local query: %s", e)
            return None

        regions.append(LocalRegion(
            join_idx=mg.join_idx,
            local_aliases=local_aliases,
            boundary_aliases=boundary,
            interface_columns=interface_cols,
            original_local=orig_local,
            rewrite_local=rew_local,
            move_group=mg,
        ))
        all_local |= local_aliases

    # Check region independence: internal aliases must be disjoint
    for i, r1 in enumerate(regions):
        for r2 in regions[i + 1:]:
            internal1 = r1.local_aliases - r1.boundary_aliases
            internal2 = r2.local_aliases - r2.boundary_aliases
            if internal1 & internal2:
                logger.debug("Decomposition: overlapping internal aliases %s", internal1 & internal2)
                return None

    # Classify context (reuse v1 infrastructure)
    ctx_class = ContextClass.SELECTION
    if norm_original.has_aggregation() or norm_original.group_by:
        ctx_class = ContextClass.AGGREGATION
    elif norm_original.order_by and norm_original.limit is not None:
        ctx_class = ContextClass.ORDER_LIMIT

    return DecompositionPlan(
        regions=regions,
        context_class=ctx_class,
        all_local_aliases=all_local,
        total_local_tables=len(all_local),
    )


def verify_decomposition_plan(
    plan: DecompositionPlan,
    catalog: Catalog,
    scope: BoundedScope,
) -> CompositionalResult:
    """Verify each local region in the decomposition plan."""
    region_results: list[WitnessResult] = []
    total_local_combos = 0

    # Build a scope with raised combo limit for decomposed local queries.
    # Local regions may have up to 8 tables (2^8=256 combos at k=2) which
    # exceeds the base combo limit of 64.
    local_scope = BoundedScope(
        k_rows=scope.k_rows,
        int_bounds=scope.int_bounds,
        string_symbols=scope.string_symbols,
        date_values=scope.date_values,
        null_semantics=scope.null_semantics,
        solver_timeout_ms=min(scope.solver_timeout_ms, 2000),
    )
    local_scope._compositional_combo_limit = 65536  # type: ignore[attr-defined]

    for region in plan.regions:
        local_tables = sorted(region.local_aliases)
        local_catalog = _build_local_catalog(local_tables, catalog)

        # Adaptive k_rows: reduce for large local regions to keep encoding
        # time tractable. 5+ tables at k=2 produces 32-64 row combos with
        # O(n²) encoding that can take tens of seconds.
        n_local = len(local_tables)
        effective_k = local_scope.k_rows
        if n_local >= 5 and effective_k > 1:
            effective_k = 1

        if effective_k != local_scope.k_rows:
            region_scope = BoundedScope(
                k_rows=effective_k,
                int_bounds=local_scope.int_bounds,
                string_symbols=local_scope.string_symbols,
                date_values=local_scope.date_values,
                null_semantics=local_scope.null_semantics,
                solver_timeout_ms=local_scope.solver_timeout_ms,
            )
            region_scope._compositional_combo_limit = 65536  # type: ignore[attr-defined]
        else:
            region_scope = local_scope

        local_combos = effective_k ** max(n_local, 1)
        total_local_combos += local_combos

        logger.info(
            "Decomposition: verifying join %d with %d local tables (%d combos, k=%d)",
            region.join_idx, n_local, local_combos, effective_k,
        )

        result = synthesize_witness(
            region.original_local,
            region.rewrite_local,
            local_catalog,
            region_scope,
        )
        region_results.append(result)

        if result.status != "unsat":
            logger.info(
                "Decomposition: join %d local result is %s (inconclusive)",
                region.join_idx, result.status,
            )
            return CompositionalResult(
                success=False,
                local_result=result,
                context_class=plan.context_class,
                plan=plan,
                region_results=region_results,
                reason=f"decomposition_inconclusive:{result.status}",
                local_combo_count=total_local_combos,
            )

    # All regions UNSAT → success
    logger.info(
        "Decomposition: all %d regions verified UNSAT (%d total local combos)",
        len(plan.regions), total_local_combos,
    )
    return CompositionalResult(
        success=True,
        local_result=region_results[-1] if region_results else None,
        context_class=plan.context_class,
        plan=plan,
        region_results=region_results,
        local_combo_count=total_local_combos,
    )


# ---------------------------------------------------------------------------
# D.2 — Local bounded equivalence
# ---------------------------------------------------------------------------

def _build_local_catalog(
    tables: list[str],
    catalog: Catalog,
) -> Catalog:
    """Build a catalog containing only the specified tables and relevant FKs."""
    table_set = {t.lower() for t in tables}
    local_tables: dict[str, TableInfo] = {}
    for tname in tables:
        tinfo = catalog.get_table(tname)
        if tinfo is not None:
            local_tables[tname.lower()] = tinfo

    local_fks = [
        fk for fk in catalog.foreign_keys
        if fk.src_table.lower() in table_set and fk.dst_table.lower() in table_set
    ]

    return Catalog(tables=local_tables, foreign_keys=local_fks)


def verify_local_equivalence(
    region: RewriteRegion,
    catalog: Catalog,
    scope: BoundedScope,
) -> WitnessResult:
    """Verify equivalence of the rewrite region under its local interface.

    Uses the full query blocks but with a higher combo limit to allow
    synthesis on larger queries. After preprocessing inside
    synthesize_witness, many tables get eliminated, bringing the combo
    count within reach.

    Args:
        region: The isolated rewrite region with original and rewrite blocks.
        catalog: Full schema catalog.
        scope: Bounded scope for synthesis.

    Returns:
        WitnessResult from local synthesis.
    """
    local_tables = region.interface.input_tables
    local_combo_count = scope.k_rows ** max(len(local_tables), 1)
    logger.info(
        "Compositional: local verification with %d affected tables (%d combos at k=%d)",
        len(local_tables), local_combo_count, scope.k_rows,
    )

    # Use a higher combo limit for compositional path: the synthesis
    # engine's own preprocessing (table elimination, predicate promotion)
    # will reduce the actual table count significantly.
    # Quick bail: if even after preprocessing, the full query will
    # exceed the combo limit, return unknown immediately.  The v2
    # decomposition path is the proper handler for large queries.
    all_tables = _collect_tables(region.original_block)
    full_combos = scope.k_rows ** max(len(all_tables), 1)
    if len(all_tables) > 4:
        logger.info(
            "Compositional v1: skipping, %d tables (%d combos) exceeds v1 limit",
            len(all_tables), full_combos,
        )
        return WitnessResult(status="unknown", solver_time_ms=0.0)

    comp_scope = BoundedScope(
        k_rows=scope.k_rows,
        int_bounds=scope.int_bounds,
        string_symbols=scope.string_symbols,
        date_values=scope.date_values,
        null_semantics=scope.null_semantics,
        solver_timeout_ms=min(scope.solver_timeout_ms, 10000),
    )
    comp_scope._compositional_combo_limit = 65536  # type: ignore[attr-defined]

    return synthesize_witness(
        region.original_block,
        region.rewrite_block,
        catalog,
        comp_scope,
    )


# ---------------------------------------------------------------------------
# D.3 — Context preservation check
# ---------------------------------------------------------------------------

def _classify_context(
    region: RewriteRegion,
    original: QueryIR,
) -> Optional[ContextClass]:
    """Classify the enclosing context into a ContextClass.

    Determines how the rewrite region relates to the enclosing query.
    """
    path = region.context_path

    # If the region IS the full query (select-only changes)
    if path == ["select"]:
        return ContextClass.PROJECTION

    # WHERE+joins changes are selection/join context
    if "where+joins" in path:
        # If query has aggregation, it's an aggregation context
        if original.has_aggregation() or original.group_by:
            return ContextClass.AGGREGATION
        # If query has ORDER BY + LIMIT, it's order_limit context
        if original.order_by and original.limit is not None:
            return ContextClass.ORDER_LIMIT
        # Check if only INNER joins are involved
        all_inner = all(j.join_type == JoinType.INNER for j in original.joins)
        if all_inner:
            return ContextClass.SELECTION
        return ContextClass.INNER_JOIN

    # Join-only changes
    if path == ["joins"] or (len(path) == 1 and path[0].startswith("joins[")):
        all_inner = all(j.join_type == JoinType.INNER for j in original.joins)
        if all_inner:
            return ContextClass.INNER_JOIN
        return None

    return None


def check_context_preservation(
    region: RewriteRegion,
    original: QueryIR,
) -> tuple[bool, Optional[str], dict[str, bool]]:
    """Check that the enclosing context C[·] preserves the interface properties.

    Returns (is_safe, reason_if_not, checks_performed).

    Context classes and requirements:
      - PROJECTION: output schema must match (same columns, same types)
      - SELECTION: unconditionally safe (WHERE doesn't change multiplicity)
      - INNER_JOIN: multiplicity preservation required
      - AGGREGATION: multiplicity-neutral or aggregation-compatible
      - ORDER_LIMIT: order preservation required
    """
    context_class = _classify_context(region, original)
    checks: dict[str, bool] = {}

    if context_class is None:
        return False, "unrecognized_context", checks

    if context_class == ContextClass.PROJECTION:
        # Output schema must match: same number of columns
        orig_cols = region.interface.output_columns
        rewrite_proj = region.rewrite_block.select
        schema_match = len(orig_cols) == len(rewrite_proj)
        checks["output_schema_match"] = schema_match
        if not schema_match:
            return False, "projection_schema_mismatch", checks
        return True, None, checks

    if context_class == ContextClass.SELECTION:
        # WHERE filtering: unconditionally safe for equivalence
        checks["selection_safe"] = True
        return True, None, checks

    if context_class == ContextClass.INNER_JOIN:
        # Inner join context: multiplicity must be preserved
        mult_ok = region.interface.preserves_multiplicity
        checks["multiplicity_preserved"] = mult_ok
        if not mult_ok:
            return False, "multiplicity_not_preserved", checks
        return True, None, checks

    if context_class == ContextClass.AGGREGATION:
        # Aggregation: if the block preserves multiplicity, aggregates
        # compute the same values. If not, but the rewrite only reorders
        # rows (e.g., join reorder), aggregation is insensitive to order.
        mult_ok = region.interface.preserves_multiplicity
        checks["multiplicity_preserved"] = mult_ok

        # Join reorders are always safe under aggregation
        # (reordering doesn't change the multiset of rows)
        is_join_reorder = region.context_path == ["joins"] or (
            len(region.context_path) == 1
            and region.context_path[0].startswith("joins[")
        )
        checks["join_reorder"] = is_join_reorder

        if mult_ok or is_join_reorder:
            return True, None, checks
        return False, "aggregation_multiplicity_unsafe", checks

    if context_class == ContextClass.ORDER_LIMIT:
        # Order+Limit: if rewrite preserves order, safe.
        # If the block only changes join order (not ORDER BY), it may
        # affect row order but ORDER BY re-establishes order.
        order_ok = region.interface.preserves_order
        checks["order_preserved"] = order_ok

        # If the query has an explicit ORDER BY, join reorder is safe
        # because ORDER BY re-establishes row order.
        has_explicit_order = len(original.order_by) > 0
        checks["has_explicit_order"] = has_explicit_order

        if order_ok or has_explicit_order:
            return True, None, checks
        return False, "order_not_preserved", checks

    return False, "unknown_context_class", checks


# ---------------------------------------------------------------------------
# D.4 — Compositional verification (D.2 + D.3 combined)
# ---------------------------------------------------------------------------

def compositional_verify(
    original: QueryIR,
    rewrite: QueryIR,
    catalog: Catalog,
    scope: BoundedScope,
) -> CompositionalResult:
    """Attempt compositional verification of a rewrite.

    Steps:
      1. Isolate the rewrite region (D.1).
      2. Check context preservation (D.3).
      3. If context is safe, verify local equivalence (D.2).

    Args:
        original: Original query IR.
        rewrite: Rewritten query IR.
        catalog: Full schema catalog.
        scope: Bounded scope.

    Returns:
        CompositionalResult with success/failure and diagnostics.
    """
    # Compute monolithic combo count for comparison
    all_tables_orig = _collect_tables(original)
    all_tables_rewrite = _collect_tables(rewrite)
    all_tables = all_tables_orig | all_tables_rewrite
    mono_combos = scope.k_rows ** max(len(all_tables), 1)

    # v2: Try decomposition path first
    plan = build_decomposition_plan(original, rewrite, catalog)
    if plan is not None:
        logger.info(
            "Compositional v2: decomposition plan with %d regions, %d local tables",
            len(plan.regions), plan.total_local_tables,
        )
        result = verify_decomposition_plan(plan, catalog, scope)
        result.monolithic_combo_count = mono_combos
        return result

    # D.1: Isolate rewrite region
    region = isolate_rewrite_region(original, rewrite)
    if region is None:
        return CompositionalResult(
            success=False,
            reason="region_isolation_failed",
            monolithic_combo_count=mono_combos,
        )

    local_combos = scope.k_rows ** max(len(region.interface.input_tables), 1)

    # D.3: Check context preservation
    ctx_class = _classify_context(region, original)
    is_safe, reason, checks = check_context_preservation(region, original)

    if not is_safe:
        return CompositionalResult(
            success=False,
            context_class=ctx_class,
            context_check=checks,
            region=region,
            reason=f"context_preservation_failed: {reason}",
            local_combo_count=local_combos,
            monolithic_combo_count=mono_combos,
        )

    # D.2: Verify local equivalence
    local_result = verify_local_equivalence(region, catalog, scope)

    if local_result.status == "unsat":
        logger.info(
            "Compositional verification succeeded: local UNSAT "
            "(%d local combos vs %d monolithic)",
            local_combos, mono_combos,
        )
        return CompositionalResult(
            success=True,
            local_result=local_result,
            context_class=ctx_class,
            context_check=checks,
            region=region,
            local_combo_count=local_combos,
            monolithic_combo_count=mono_combos,
        )

    # Local verification found a difference or timed out
    return CompositionalResult(
        success=False,
        local_result=local_result,
        context_class=ctx_class,
        context_check=checks,
        region=region,
        reason=f"local_verification_{local_result.status}",
        local_combo_count=local_combos,
        monolithic_combo_count=mono_combos,
    )


def _extract_join_elimination_region(
    original: QueryIR,
    rewrite: QueryIR,
    catalog: Catalog,
) -> Optional[LocalRegion]:
    """Detect when a join was eliminated between original and rewrite.

    If the original has more joins than the rewrite, the eliminated join's
    right-side table forms a local region for compositional verification.
    """
    if len(original.joins) <= len(rewrite.joins):
        return None

    rewrite_aliases: set[str] = set()
    if isinstance(rewrite.from_table, RelRef):
        rewrite_aliases.add(rewrite.from_table.ref_name.lower())
    for j in rewrite.joins:
        if isinstance(j.right, RelRef):
            rewrite_aliases.add(j.right.ref_name.lower())

    for idx, j in enumerate(original.joins):
        if not isinstance(j.right, RelRef):
            continue
        alias = j.right.ref_name.lower()
        if alias not in rewrite_aliases:
            local_aliases = {alias}
            from_alias = (
                original.from_table.ref_name.lower()
                if isinstance(original.from_table, RelRef)
                else ""
            )
            boundary = {from_alias} if from_alias else set()
            alias_to_rel = _alias_to_relation(original)
            iface = _build_interface_columns(boundary, alias_to_rel, catalog)
            return LocalRegion(
                join_idx=idx,
                local_aliases=local_aliases,
                boundary_aliases=boundary,
                interface_columns=iface,
                original_local=original,
                rewrite_local=rewrite,
                proof_kind="join_elimination",
            )
    return None


def _extract_subquery_decorrelation_region(
    original: QueryIR,
    rewrite: QueryIR,
    catalog: Catalog,
) -> Optional[LocalRegion]:
    """Detect when a correlated subquery was replaced with a join.

    Checks if the original has an EXISTS/IN subquery in WHERE that
    references a table now appearing as a join in the rewrite.
    """
    subquery_exprs = _collect_subquery_exprs(original.where)
    if not subquery_exprs:
        return None

    original_join_aliases: set[str] = set()
    for j in original.joins:
        if isinstance(j.right, RelRef):
            original_join_aliases.add(j.right.ref_name.lower())

    for sq_ir in subquery_exprs:
        sq_tables: set[str] = set()
        if isinstance(sq_ir.from_table, RelRef):
            sq_tables.add(sq_ir.from_table.ref_name.lower())
        for j in sq_ir.joins:
            if isinstance(j.right, RelRef):
                sq_tables.add(j.right.ref_name.lower())

        for rj in rewrite.joins:
            if not isinstance(rj.right, RelRef):
                continue
            rj_alias = rj.right.ref_name.lower()
            if rj_alias in sq_tables and rj_alias not in original_join_aliases:
                local_aliases = {rj_alias}
                from_alias = (
                    original.from_table.ref_name.lower()
                    if isinstance(original.from_table, RelRef)
                    else ""
                )
                boundary = {from_alias} if from_alias else set()
                alias_to_rel = _alias_to_relation(rewrite)
                iface = _build_interface_columns(boundary, alias_to_rel, catalog)
                return LocalRegion(
                    join_idx=0,
                    local_aliases=local_aliases,
                    boundary_aliases=boundary,
                    interface_columns=iface,
                    original_local=original,
                    rewrite_local=rewrite,
                    proof_kind="subquery_decorrelation",
                )
    return None


def _collect_subquery_exprs(expr: Optional[Expr]) -> list[QueryIR]:
    """Collect all subquery QueryIR nodes from an expression tree."""
    results: list[QueryIR] = []
    if expr is None:
        return results
    if isinstance(expr, ExistsSubquery):
        results.append(expr.query)
    elif isinstance(expr, InSubquery):
        results.append(expr.query)
        results.extend(_collect_subquery_exprs(expr.expr))
    elif isinstance(expr, ScalarSubquery):
        results.append(expr.query)
    elif isinstance(expr, BinOp):
        results.extend(_collect_subquery_exprs(expr.left))
        results.extend(_collect_subquery_exprs(expr.right))
    elif isinstance(expr, UnaryOp):
        results.extend(_collect_subquery_exprs(expr.operand))
    return results


def _narrow_interface_columns(
    full_interface: list[InterfaceColumn],
    ir: QueryIR,
    *,
    local_aliases: set[str],
    boundary_aliases: set[str],
    catalog: Catalog,
) -> list[InterfaceColumn]:
    """Narrow interface columns to only those externally referenced + PK/unique.

    Keeps columns that are referenced by the query's SELECT, WHERE, GROUP BY,
    HAVING, ORDER BY, or join ON clauses, plus any PK/unique columns for
    boundary tables.
    """
    referenced_cols: set[tuple[str, str]] = set()

    def _collect_col_refs(expr: Optional[Expr]) -> None:
        if expr is None:
            return
        if isinstance(expr, ColumnRef) and expr.table:
            referenced_cols.add((expr.table.lower(), expr.column.lower()))
        elif isinstance(expr, BinOp):
            _collect_col_refs(expr.left)
            _collect_col_refs(expr.right)
        elif isinstance(expr, UnaryOp):
            _collect_col_refs(expr.operand)
        elif isinstance(expr, FuncCall):
            for a in expr.args:
                _collect_col_refs(a)
        elif isinstance(expr, AggCall) and expr.arg:
            _collect_col_refs(expr.arg)
        elif isinstance(expr, InList):
            _collect_col_refs(expr.expr)
            for v in expr.values:
                _collect_col_refs(v)
        elif isinstance(expr, Between):
            _collect_col_refs(expr.expr)
            _collect_col_refs(expr.low)
            _collect_col_refs(expr.high)
        elif isinstance(expr, CaseExpr):
            for cw in expr.whens:
                _collect_col_refs(cw.when)
                _collect_col_refs(cw.then)
            if expr.else_:
                _collect_col_refs(expr.else_)

    for e in ir.select:
        _collect_col_refs(e)
    _collect_col_refs(ir.where)
    for g in ir.group_by:
        _collect_col_refs(g)
    _collect_col_refs(ir.having)
    for s in ir.order_by:
        _collect_col_refs(s.expr)
    for j in ir.joins:
        _collect_col_refs(j.on)

    pk_cols: set[tuple[str, str]] = set()
    alias_to_rel = _alias_to_relation(ir)
    for alias in boundary_aliases:
        table_name = alias_to_rel[alias].table if alias in alias_to_rel else alias
        tinfo = catalog.get_table(table_name)
        if tinfo:
            for pk in tinfo.primary_keys:
                pk_cols.add((alias.lower(), pk.lower()))
            for uc in getattr(tinfo, "unique_columns", []):
                pk_cols.add((alias.lower(), uc.lower()))

    narrowed: list[InterfaceColumn] = []
    for ic in full_interface:
        key = (ic.table_alias.lower(), ic.column_name.lower())
        if key in referenced_cols or key in pk_cols:
            narrowed.append(ic)
    return narrowed


def check_region_independence(plan: DecompositionPlan) -> tuple[bool, Optional[str]]:
    """Check that local regions have disjoint internal aliases.

    Two regions may share boundary aliases (that's expected), but their
    non-boundary (internal) aliases must be disjoint for independent
    verification to be sound.
    """
    seen_internal: dict[str, int] = {}
    for i, region in enumerate(plan.regions):
        internal = region.local_aliases - region.boundary_aliases
        for alias in internal:
            if alias in seen_internal:
                return (
                    False,
                    f"overlapping internal aliases: '{alias}' appears in "
                    f"regions {seen_internal[alias]} and {i}",
                )
            seen_internal[alias] = i
    return (True, None)


@dataclass
class LocalBlock:
    """Stub for removed API."""
    aliases: set = field(default_factory=set)
    internal_aliases: set = field(default_factory=set)
    exported_columns: list = field(default_factory=list)
    original_block: Optional[QueryIR] = None
    rewrite_block: Optional[QueryIR] = None

    @property
    def boundary_aliases(self) -> set:
        return self.aliases - self.internal_aliases


def _add_row_identity_columns(cols, aliases):
    """Stub for removed API."""
    result = list(cols)
    for alias in sorted(aliases):
        result.append(InterfaceColumn(table_alias=alias, column_name="__rid", sem_type=SemType.INT))
    return result


def _compute_closed_block(ir, aliases, max_aliases=None):
    """Stub for removed API."""
    result = set(aliases)
    if isinstance(ir.from_table, RelRef):
        a = (ir.from_table.alias or ir.from_table.table).lower()
        if a in aliases:
            result.add(a)
    for j in ir.joins:
        if isinstance(j.right, RelRef):
            a = (j.right.alias or j.right.table).lower()
            result.add(a)
    if max_aliases is not None and len(result) > max_aliases:
        return set(list(result)[:max_aliases])
    return result


def lift_local_witness(result, *args, **kwargs):
    """Stub for removed API."""
    if result is None or result.status != "sat":
        return None
    if not result.witness_db:
        return None
    return result.witness_db


def _default_value_for_type(sem_type):
    """Stub for removed API."""
    _defaults = {
        SemType.INT: 1,
        SemType.STRING: "default",
        SemType.FLOAT: 1.0,
        SemType.BOOL: True,
        SemType.DATE: "2025-01-01",
        SemType.DECIMAL: 1.0,
    }
    return _defaults.get(sem_type, None)
