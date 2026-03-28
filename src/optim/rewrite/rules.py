"""Algebraic rewrite rules for query optimization.

Each rule is a function:
    rule_X(ir: QueryIR, catalog: Catalog) -> list[QueryIR]

Returns zero or more rewritten IRs. The verifier decides correctness.

Rules:
  R1 - Predicate pushdown (WHERE → JOIN ON)
  R2 - Predicate pullup (JOIN ON → WHERE, INNER only)
  R3 - Join elimination (remove provably redundant joins)
  R4 - Redundant DISTINCT removal (when PK guarantees uniqueness)
  R5 - Join reordering (permute INNER JOIN order)
  R6 - Projection minimization (strip unused non-agg columns)

Seeded from mutations:
  - DISTINCT toggle
  - Aggregation swap (COUNT(*) ↔ COUNT(DISTINCT col), SUM ↔ AVG)
  - Unwrap ABS

Utilities:
  - _shape_signature, dedup_candidates, diversity_filter
"""

from __future__ import annotations

import itertools
import logging
from typing import Optional

from ..cegis.equivalence import Candidate
from ..ir.types import (
    AggCall,
    AggFunc,
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
    Star,
    UnaryOp,
    UnaryOpKind,
)
from ..schema.catalog import Catalog, ForeignKey

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shape signature for dedup (from mutations.py, verbatim)
# ---------------------------------------------------------------------------

def _shape_signature(ir: QueryIR) -> str:
    """Compute a structural 'shape' signature for dedup."""
    tables: set[str] = set()
    if isinstance(ir.from_table, RelRef):
        tables.add(ir.from_table.table.lower())
    elif isinstance(ir.from_table, DerivedTable):
        inner = ir.from_table.query
        if isinstance(inner.from_table, RelRef):
            tables.add(inner.from_table.table.lower())
        for ij in inner.joins:
            if isinstance(ij.right, RelRef):
                tables.add(ij.right.table.lower())
    for j in ir.joins:
        if isinstance(j.right, RelRef):
            tables.add(j.right.table.lower())

    join_parts: list[str] = []
    for j in ir.joins:
        if isinstance(j.right, RelRef):
            join_parts.append(f"{j.right.table.lower()}:{j.join_type.value}")
    join_sig = ",".join(sorted(join_parts))

    gb_keys: list[str] = []
    for g in ir.group_by:
        if isinstance(g, ColumnRef):
            gb_keys.append(f"{(g.table or '').lower()}.{g.column.lower()}")
        else:
            gb_keys.append(type(g).__name__)
    gb_sig = ",".join(sorted(gb_keys))

    agg_funcs = sorted({
        f"{e.func.value}{'_D' if e.distinct else ''}"
        for e in ir.select if isinstance(e, AggCall)
    })

    # WHERE predicate count (distinguishes pushdown/pullup rewrites)
    where_conjuncts = len(_collect_and_conjuncts(ir.where)) if ir.where else 0

    # ON-clause predicate counts per join
    on_counts = []
    for j in ir.joins:
        if j.on:
            on_counts.append(str(len(_collect_and_conjuncts(j.on))))
        else:
            on_counts.append("0")

    parts = [
        ",".join(sorted(tables)),
        join_sig,
        gb_sig,
        str(ir.distinct),
        str(ir.has_aggregation()),
        ",".join(agg_funcs),
        str(len(ir.joins)),
        str(len(ir.group_by)),
        str(len(ir.select)),
        str(ir.having is not None),
        str(len(ir.order_by) > 0),
        str(ir.limit is not None),
        str(where_conjuncts),
        ",".join(on_counts),
    ]
    return "|".join(parts)


def dedup_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Remove near-duplicate candidates by shape signature."""
    seen: set[str] = set()
    result: list[Candidate] = []
    for c in candidates:
        sig = _shape_signature(c.ir)
        if sig not in seen:
            seen.add(sig)
            result.append(c)
    if len(result) < len(candidates):
        logger.info("Dedup: %d → %d candidates", len(candidates), len(result))
    return result


def diversity_filter(candidates: list[Candidate], max_per_key: int = 2) -> list[Candidate]:
    """Keep up to *max_per_key* candidates per semantic key."""
    def _semantic_key(ir: QueryIR) -> str:
        tables: set[str] = set()
        if isinstance(ir.from_table, RelRef):
            tables.add(ir.from_table.table.lower())
        for j in ir.joins:
            if isinstance(j.right, RelRef):
                tables.add(j.right.table.lower())

        join_types = sorted(
            f"{j.right.table.lower() if isinstance(j.right, RelRef) else '?'}:{j.join_type.value}"
            for j in ir.joins
        )
        gb = sorted(
            f"{(g.table or '').lower()}.{g.column.lower()}"
            for g in ir.group_by if isinstance(g, ColumnRef)
        )
        aggs = sorted(
            f"{e.func.value}{'_D' if e.distinct else ''}"
            for e in ir.select if isinstance(e, AggCall)
        )
        return f"{','.join(sorted(tables))}|{','.join(join_types)}|{','.join(gb)}|{','.join(aggs)}"

    from collections import defaultdict
    groups: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        groups[_semantic_key(c.ir)].append(c)

    kept: set[str] = set()
    for key, group in groups.items():
        group.sort(key=lambda c: c.confidence, reverse=True)
        for c in group[:max_per_key]:
            kept.add(c.id)

    result = [c for c in candidates if c.id in kept]
    if len(result) < len(candidates):
        logger.info("Diversity filter: %d → %d candidates", len(candidates), len(result))
    return result


# ---------------------------------------------------------------------------
# Helpers: expression tree utilities
# ---------------------------------------------------------------------------

def _collect_and_conjuncts(expr: Expr) -> list[Expr]:
    """Flatten top-level AND into a list of conjuncts."""
    if isinstance(expr, BinOp) and expr.op == BinOpKind.AND:
        return _collect_and_conjuncts(expr.left) + _collect_and_conjuncts(expr.right)
    return [expr]


def _rebuild_and(conjuncts: list[Expr]) -> Expr:
    """Rebuild a chain of AND from a list of conjuncts."""
    result = conjuncts[0]
    for c in conjuncts[1:]:
        result = BinOp(op=BinOpKind.AND, left=result, right=c)
    return result


def _collect_column_refs(expr: Optional[Expr]) -> set[tuple[str, str]]:
    """Collect all (table, column) pairs referenced in an expression."""
    refs: set[tuple[str, str]] = set()
    if expr is None:
        return refs
    if isinstance(expr, ColumnRef):
        refs.add(((expr.table or "").lower(), expr.column.lower()))
    elif isinstance(expr, BinOp):
        refs |= _collect_column_refs(expr.left)
        refs |= _collect_column_refs(expr.right)
    elif isinstance(expr, UnaryOp):
        refs |= _collect_column_refs(expr.operand)
    elif isinstance(expr, AggCall):
        refs |= _collect_column_refs(expr.arg)
    elif isinstance(expr, FuncCall):
        for a in expr.args:
            refs |= _collect_column_refs(a)
    elif isinstance(expr, InList):
        refs |= _collect_column_refs(expr.expr)
        for v in expr.values:
            refs |= _collect_column_refs(v)
    elif isinstance(expr, (ScalarSubquery, InSubquery, ExistsSubquery)):
        pass  # Don't descend into subqueries for table-ref checking
    return refs


def _table_names_in_ir(ir: QueryIR) -> set[str]:
    """Get all table names in FROM + JOINs."""
    tables: set[str] = set()
    if isinstance(ir.from_table, RelRef):
        tables.add((ir.from_table.alias or ir.from_table.table).lower())
    for j in ir.joins:
        if isinstance(j.right, RelRef):
            tables.add((j.right.alias or j.right.table).lower())
    return tables


def _all_referenced_tables(ir: QueryIR) -> set[str]:
    """Collect all table names referenced in SELECT, WHERE, GROUP BY,
    HAVING, ORDER BY (but not FROM/JOIN declarations)."""
    refs: set[tuple[str, str]] = set()
    for sel in ir.select:
        refs |= _collect_column_refs(sel)
    refs |= _collect_column_refs(ir.where)
    refs |= _collect_column_refs(ir.having)
    for g in ir.group_by:
        refs |= _collect_column_refs(g)
    for s in ir.order_by:
        refs |= _collect_column_refs(s.expr)
    return {t for t, _ in refs if t}


# ===================================================================
# SEEDED RULES (from mutations.py, adapted)
# ===================================================================

# ---------------------------------------------------------------------------
# DISTINCT toggle
# ---------------------------------------------------------------------------

def rule_distinct_toggle(ir: QueryIR, catalog: Catalog) -> list[QueryIR]:
    """Toggle the DISTINCT flag."""
    mutated = ir.model_copy(deep=True)
    mutated.distinct = not ir.distinct
    return [mutated]


# ---------------------------------------------------------------------------
# Aggregation swap
# ---------------------------------------------------------------------------

def _find_first_column_ref(ir: QueryIR) -> Optional[ColumnRef]:
    """Find the first ColumnRef in group_by or select."""
    for expr in ir.group_by:
        if isinstance(expr, ColumnRef):
            return expr
    for expr in ir.select:
        if isinstance(expr, ColumnRef):
            return expr
    return None


def rule_agg_swap(ir: QueryIR, catalog: Catalog) -> list[QueryIR]:
    """Generate aggregation swap variants.

    COUNT(*) ↔ COUNT(DISTINCT col), SUM ↔ AVG, SUM → COUNT(*).
    """
    results: list[QueryIR] = []
    col_ref = _find_first_column_ref(ir)

    for i, expr in enumerate(ir.select):
        if not isinstance(expr, AggCall):
            continue

        # COUNT(*) → COUNT(DISTINCT col)
        if (expr.func == AggFunc.COUNT and expr.arg is None
                and not expr.distinct and col_ref is not None):
            new_ir = ir.model_copy(deep=True)
            new_ir.select[i] = AggCall(
                func=AggFunc.COUNT,
                arg=col_ref.model_copy(deep=True),
                distinct=True,
                alias=expr.alias,
            )
            results.append(new_ir)

        # COUNT(DISTINCT col) → COUNT(col)
        if expr.func == AggFunc.COUNT and expr.distinct:
            new_ir = ir.model_copy(deep=True)
            new_ir.select[i] = AggCall(
                func=AggFunc.COUNT,
                arg=expr.arg.model_copy(deep=True) if expr.arg else None,
                distinct=False,
                alias=expr.alias,
            )
            results.append(new_ir)

        # SUM ↔ AVG
        if expr.func in (AggFunc.SUM, AggFunc.AVG) and expr.arg is not None:
            swap = AggFunc.AVG if expr.func == AggFunc.SUM else AggFunc.SUM
            new_ir = ir.model_copy(deep=True)
            new_ir.select[i] = AggCall(
                func=swap,
                arg=expr.arg.model_copy(deep=True),
                distinct=expr.distinct,
                alias=expr.alias,
            )
            results.append(new_ir)

    return results


# ---------------------------------------------------------------------------
# Unwrap ABS
# ---------------------------------------------------------------------------

def rule_unwrap_abs(ir: QueryIR, catalog: Catalog) -> list[QueryIR]:
    """Remove ABS() wrappers from SELECT expressions."""
    results: list[QueryIR] = []
    for i, expr in enumerate(ir.select):
        if isinstance(expr, FuncCall) and expr.func_name.upper() == "ABS" and len(expr.args) == 1:
            new_ir = ir.model_copy(deep=True)
            inner = expr.args[0].model_copy(deep=True)
            if hasattr(expr, 'alias') and expr.alias:
                if hasattr(inner, 'alias'):
                    inner.alias = expr.alias
            new_ir.select[i] = inner
            results.append(new_ir)
    return results


# ===================================================================
# NEW OPTIMIZATION RULES (R1–R6)
# ===================================================================

# ---------------------------------------------------------------------------
# R1: Predicate pushdown (WHERE → JOIN ON)
# ---------------------------------------------------------------------------

def rule_predicate_pushdown(ir: QueryIR, catalog: Catalog) -> list[QueryIR]:
    """Move WHERE conjuncts that reference a single joined table into its ON clause.

    Only pushes through INNER JOINs (semantics-preserving).
    """
    if ir.where is None or not ir.joins:
        return []

    conjuncts = _collect_and_conjuncts(ir.where)
    if len(conjuncts) <= 1:
        return []

    # Build map: join index → table name
    join_tables: dict[int, str] = {}
    for idx, j in enumerate(ir.joins):
        if isinstance(j.right, RelRef):
            join_tables[idx] = (j.right.alias or j.right.table).lower()

    pushed: list[tuple[int, Expr]] = []  # (join_idx, conjunct)
    remaining: list[Expr] = []

    for conj in conjuncts:
        refs = _collect_column_refs(conj)
        ref_tables = {t for t, _ in refs if t}

        # Find if all referenced tables belong to exactly one INNER join
        matched_idx = None
        for idx, jtable in join_tables.items():
            if ref_tables and ref_tables <= {jtable} and ir.joins[idx].join_type == JoinType.INNER:
                matched_idx = idx
                break

        if matched_idx is not None:
            pushed.append((matched_idx, conj))
        else:
            remaining.append(conj)

    if not pushed:
        return []

    new_ir = ir.model_copy(deep=True)

    # Add pushed predicates to the ON clause of each join
    for idx, conj in pushed:
        old_on = new_ir.joins[idx].on
        new_ir.joins[idx].on = BinOp(op=BinOpKind.AND, left=old_on, right=conj)

    # Update WHERE
    if remaining:
        new_ir.where = _rebuild_and(remaining)
    else:
        new_ir.where = None

    return [new_ir]


# ---------------------------------------------------------------------------
# R2: Predicate pullup (JOIN ON → WHERE, INNER JOINs only)
# ---------------------------------------------------------------------------

def rule_predicate_pullup(ir: QueryIR, catalog: Catalog) -> list[QueryIR]:
    """Move non-join-key ON-clause predicates to WHERE (INNER JOINs only).

    For each INNER JOIN, decompose its ON clause into conjuncts.
    Move conjuncts that don't reference both sides of the join to WHERE.
    """
    if not ir.joins:
        return []

    from_table = ""
    if isinstance(ir.from_table, RelRef):
        from_table = (ir.from_table.alias or ir.from_table.table).lower()

    pulled: list[Expr] = []
    new_joins = list(ir.joins)
    changed = False

    for idx, j in enumerate(ir.joins):
        if j.join_type != JoinType.INNER:
            continue
        if j.on is None:
            continue

        join_right = ""
        if isinstance(j.right, RelRef):
            join_right = (j.right.alias or j.right.table).lower()

        # Tables on the "left" side = from_table + all prior joins
        left_tables = {from_table}
        for prev_j in ir.joins[:idx]:
            if isinstance(prev_j.right, RelRef):
                left_tables.add((prev_j.right.alias or prev_j.right.table).lower())

        conjuncts = _collect_and_conjuncts(j.on)
        if len(conjuncts) <= 1:
            continue

        keep_on: list[Expr] = []
        for conj in conjuncts:
            refs = _collect_column_refs(conj)
            ref_tables = {t for t, _ in refs if t}
            # Pull up if the conjunct doesn't reference both sides
            references_left = bool(ref_tables & left_tables)
            references_right = join_right in ref_tables
            if references_left and references_right:
                keep_on.append(conj)
            else:
                pulled.append(conj)

        if len(keep_on) < len(conjuncts):
            changed = True
            new_j = j.model_copy(deep=True)
            new_j.on = _rebuild_and(keep_on) if keep_on else j.on
            new_joins[idx] = new_j

    if not changed:
        return []

    new_ir = ir.model_copy(deep=True)
    new_ir.joins = new_joins

    # Extend WHERE with pulled predicates
    existing_where = new_ir.where
    all_where = ([existing_where] if existing_where else []) + pulled
    new_ir.where = _rebuild_and(all_where)

    return [new_ir]


# ---------------------------------------------------------------------------
# R3: Join elimination (remove provably redundant joins)
# ---------------------------------------------------------------------------

def rule_join_elimination(ir: QueryIR, catalog: Catalog) -> list[QueryIR]:
    """Remove a joined table if:
    1. No columns from that table appear in SELECT, WHERE, GROUP BY, HAVING, ORDER BY
    2. The join is INNER on a FK→PK relationship (guaranteeing exactly 1 match)
    """
    if not ir.joins:
        return []

    # Collect all column references from non-FROM/JOIN parts
    referenced = _all_referenced_tables(ir)

    # Also collect refs from other JOIN ON clauses
    for j in ir.joins:
        referenced |= {t for t, _ in _collect_column_refs(j.on) if t}

    results: list[QueryIR] = []

    for idx, j in enumerate(ir.joins):
        if j.join_type != JoinType.INNER:
            continue
        if not isinstance(j.right, RelRef):
            continue

        join_table = (j.right.alias or j.right.table).lower()
        join_real_table = j.right.table.lower()

        # Check if any other join's ON references this table
        other_join_refs: set[str] = set()
        for other_idx, other_j in enumerate(ir.joins):
            if other_idx == idx:
                continue
            other_join_refs |= {t for t, _ in _collect_column_refs(other_j.on) if t}

        # Check if this table is referenced anywhere except its own ON clause
        all_refs_except_own = referenced - {join_table}
        if join_table in (referenced | other_join_refs):
            # Table is still used somewhere
            # But check: is it only used in its own ON clause?
            refs_in_select_where = _all_referenced_tables(ir)
            if join_table in refs_in_select_where or join_table in other_join_refs:
                continue

        # Check FK→PK: the join condition should match a FK relationship.
        # FIX.28a: Verify the ON predicate actually uses the FK/PK columns.
        # Previously only checked if the joined table was a PK target of
        # any FK, without verifying the ON clause references those columns.
        on_refs = _collect_column_refs(j.on)
        on_ref_set = {(t.lower() if t else "", c.lower()) for t, c in on_refs}
        is_fk_pk = False
        for fk in catalog.foreign_keys:
            src_t = fk.src_table.lower()
            dst_t = fk.dst_table.lower()
            src_c = fk.src_column.lower()
            dst_c = fk.dst_column.lower()

            # Check: from_side.fk_col = join_table.pk_col
            if dst_t == join_real_table:
                dst_table_info = catalog.get_table(dst_t)
                if dst_table_info and dst_c in [pk.lower() for pk in dst_table_info.primary_keys]:
                    # Verify ON actually references both FK and PK columns
                    has_dst = any(t == join_table and c == dst_c for t, c in on_ref_set)
                    has_src = any(c == src_c for t, c in on_ref_set if t != join_table)
                    if has_dst and has_src:
                        is_fk_pk = True
                        break
            # Reverse: join_table.fk_col = from_side.pk_col
            # This means the join could fan out, so skip
            if src_t == join_real_table:
                pass

        if not is_fk_pk:
            continue

        # Safe to eliminate this join
        new_ir = ir.model_copy(deep=True)
        new_ir.joins = [jj for ii, jj in enumerate(ir.joins) if ii != idx]
        results.append(new_ir)

    return results


# ---------------------------------------------------------------------------
# R4: Redundant DISTINCT removal
# ---------------------------------------------------------------------------

def rule_redundant_distinct_removal(ir: QueryIR, catalog: Catalog) -> list[QueryIR]:
    """Remove DISTINCT if the SELECT columns include a PK/UNIQUE key.

    Safe when: single table (no joins adding multiplicity), and all PK
    columns are in the SELECT list.
    """
    if not ir.distinct:
        return []

    # Only safe for single-table queries (no joins)
    if ir.joins:
        return []

    if not isinstance(ir.from_table, RelRef):
        return []

    table_name = ir.from_table.table.lower()
    table_info = catalog.get_table(table_name)
    if table_info is None:
        return []

    # Check if all PK columns are in SELECT
    if not table_info.primary_keys:
        # Try unique columns
        if not table_info.unique_columns:
            return []
        key_cols = {u.lower() for u in table_info.unique_columns}
    else:
        key_cols = {pk.lower() for pk in table_info.primary_keys}

    select_cols: set[str] = set()
    for sel in ir.select:
        if isinstance(sel, ColumnRef):
            select_cols.add(sel.column.lower())
        elif isinstance(sel, Star):
            # SELECT * includes all columns
            select_cols = key_cols
            break

    if not key_cols <= select_cols:
        return []

    new_ir = ir.model_copy(deep=True)
    new_ir.distinct = False
    return [new_ir]


# ---------------------------------------------------------------------------
# R5: Join reordering (INNER JOINs only)
# ---------------------------------------------------------------------------

def rule_join_reorder(ir: QueryIR, catalog: Catalog) -> list[QueryIR]:
    """Generate permutations of INNER JOIN order.

    Only permutes INNER JOINs. Preserves all ON predicates by keeping
    them with their respective right-hand table. The from_table is also
    eligible for swapping into a join position.

    Caps at 6 permutations to bound combinatorial explosion.
    """
    if not ir.joins:
        return []

    # Only reorder if ALL joins are INNER
    if not all(j.join_type == JoinType.INNER for j in ir.joins):
        return []

    # Collect all "positions": from_table + joins
    # Each position is (relation, on_condition)
    # For from_table, there's no ON condition
    if not isinstance(ir.from_table, RelRef):
        return []

    # Build list of (table_ref, on_predicate_or_None)
    # We'll reassemble: first item becomes from_table, rest become joins
    # The ON predicates stay with their respective tables
    relations = [ir.from_table] + [j.right for j in ir.joins]
    on_clauses = [None] + [j.on for j in ir.joins]

    # Pair each relation with its ON clause
    items = list(zip(relations, on_clauses))

    n = len(items)
    if n <= 1:
        return []

    # Generate up to 6 distinct permutations (excluding original order)
    results: list[QueryIR] = []
    seen = set()
    original_key = tuple(
        (r.table.lower() if isinstance(r, RelRef) else str(r)) for r, _ in items
    )
    seen.add(original_key)

    max_perms = 6
    for perm in itertools.islice(itertools.permutations(range(n)), n * n):
        if len(results) >= max_perms:
            break

        perm_key = tuple(
            (items[i][0].table.lower() if isinstance(items[i][0], RelRef) else str(items[i][0]))
            for i in perm
        )
        if perm_key in seen:
            continue
        seen.add(perm_key)

        reordered = [items[i] for i in perm]

        new_ir = ir.model_copy(deep=True)
        new_ir.from_table = reordered[0][0].model_copy(deep=True)

        new_joins: list[JoinClause] = []
        for rel, on in reordered[1:]:
            # Find the original ON clause for this relation
            orig_on = on
            if orig_on is None:
                # This was originally the from_table — we need to find any ON
                # clause that references it. Use a synthetic cross-join ON TRUE.
                # Actually, we need to reconstruct: gather all ON conditions
                # and assign each to the correct join position.
                # Simpler approach: collect ALL ON predicates, then assign each
                # to the first join that has both sides available.
                orig_on = Literal(value=True)

            new_joins.append(JoinClause(
                join_type=JoinType.INNER,
                right=rel.model_copy(deep=True),
                on=orig_on,
            ))

        # Reassign ON clauses properly: collect all original ON predicates
        # and reconstruct based on which tables are available at each join point
        all_on_preds: list[Expr] = [j.on for j in ir.joins]

        # For each join position in the new order, assign predicates
        # whose referenced tables are all available at that point
        available_tables: set[str] = set()
        if isinstance(new_ir.from_table, RelRef):
            available_tables.add((new_ir.from_table.alias or new_ir.from_table.table).lower())

        assigned: list[bool] = [False] * len(all_on_preds)
        for j_idx, nj in enumerate(new_joins):
            if isinstance(nj.right, RelRef):
                available_tables.add((nj.right.alias or nj.right.table).lower())

            # Find unassigned predicates whose tables are now all available
            applicable: list[Expr] = []
            for p_idx, pred in enumerate(all_on_preds):
                if assigned[p_idx]:
                    continue
                ref_tables = {t for t, _ in _collect_column_refs(pred) if t}
                if ref_tables <= available_tables:
                    applicable.append(pred)
                    assigned[p_idx] = True

            if applicable:
                new_joins[j_idx] = JoinClause(
                    join_type=JoinType.INNER,
                    right=nj.right,
                    on=_rebuild_and(applicable),
                )
            else:
                # Use TRUE as placeholder (cross-join semantics)
                new_joins[j_idx] = JoinClause(
                    join_type=JoinType.INNER,
                    right=nj.right,
                    on=Literal(value=True),
                )

        # Check all predicates were assigned
        if not all(assigned):
            continue  # Skip this permutation — can't safely reassign all predicates

        new_ir.joins = new_joins
        results.append(new_ir)

    return results


# ---------------------------------------------------------------------------
# R6: Projection minimization
# ---------------------------------------------------------------------------

def rule_projection_minimization(ir: QueryIR, catalog: Catalog) -> list[QueryIR]:
    """Strip non-aggregate SELECT columns when query has aggregation.

    Keeps only aggregate expressions + group_by columns needed for validity.
    """
    if not ir.has_aggregation():
        return []

    agg_indices = [i for i, e in enumerate(ir.select) if isinstance(e, AggCall)]
    non_agg_indices = [i for i, e in enumerate(ir.select)
                       if not isinstance(e, AggCall) and not isinstance(e, Star)]

    if len(non_agg_indices) <= 1 or len(agg_indices) == 0:
        return []

    new_ir = ir.model_copy(deep=True)
    new_ir.select = [ir.select[i].model_copy(deep=True) for i in agg_indices]

    # Must keep group_by columns in SELECT for valid SQL
    gb_refs = set()
    for g in ir.group_by:
        if isinstance(g, ColumnRef):
            gb_refs.add(((g.table or "").lower(), g.column.lower()))

    for i in non_agg_indices:
        expr = ir.select[i]
        if isinstance(expr, ColumnRef) and ((expr.table or "").lower(), expr.column.lower()) in gb_refs:
            new_ir.select.append(expr.model_copy(deep=True))

    if len(new_ir.select) < len(ir.select):
        return [new_ir]
    return []


# ===================================================================
# Rule registry
# ===================================================================

RULE_REGISTRY: dict[str, callable] = {
    "R1": rule_predicate_pushdown,
    "R2": rule_predicate_pullup,
    "R3": rule_join_elimination,
    "R4": rule_redundant_distinct_removal,
    "R5": rule_join_reorder,
    "R6": rule_projection_minimization,
    "distinct_toggle": rule_distinct_toggle,
    "agg_swap": rule_agg_swap,
    "unwrap_abs": rule_unwrap_abs,
}


# Compatibility stubs for removed APIs (referenced by tests)
from dataclasses import dataclass


@dataclass
class RewriteDelta:
    """Stub for removed API."""
    rule_id: str = ""
    affected_aliases: set = None
    description: str = ""

    def __post_init__(self):
        if self.affected_aliases is None:
            self.affected_aliases = set()


def rule_self_join_collapse(ir: QueryIR, catalog: Catalog) -> list[QueryIR]:
    """Collapse redundant self-joins on the same table via the same key.

    When two aliases of the same table are INNER-JOINed on the same
    equi-key, and one alias is not referenced in SELECT, the redundant
    join can be eliminated by rewriting all references to the removed
    alias to point at the kept alias.
    """
    if len(ir.joins) < 2:
        return []

    # Build table→[(join_idx, alias, join_key_col)] map
    from_alias = ir.from_table.ref_name.lower() if isinstance(ir.from_table, RelRef) else None
    from_table = ir.from_table.table.lower() if isinstance(ir.from_table, RelRef) else None

    alias_info: list[tuple[int, str, str, str]] = []  # (join_idx, alias, table, equi_col)
    for idx, j in enumerate(ir.joins):
        if j.join_type != JoinType.INNER:
            continue
        if not isinstance(j.right, RelRef):
            continue
        alias = j.right.ref_name.lower()
        table = j.right.table.lower()
        equi_col = _extract_equi_col(j.on, alias)
        if equi_col:
            alias_info.append((idx, alias, table, equi_col))

    # Find pairs on the same table with the same equi-join column
    select_aliases = set()
    for expr in ir.select:
        select_aliases |= _collect_aliases(expr)

    for i, (idx_a, alias_a, table_a, col_a) in enumerate(alias_info):
        for idx_b, alias_b, table_b, col_b in alias_info[i + 1:]:
            if table_a != table_b or col_a != col_b:
                continue
            # Determine which alias is NOT in SELECT → candidate for removal
            a_in_sel = alias_a in select_aliases
            b_in_sel = alias_b in select_aliases
            if a_in_sel and b_in_sel:
                continue  # Both needed
            remove_alias = alias_b if not b_in_sel else alias_a
            keep_alias = alias_a if remove_alias == alias_b else alias_b
            remove_idx = idx_b if remove_alias == alias_b else idx_a

            # Build collapsed IR
            new_joins = [j for k, j in enumerate(ir.joins) if k != remove_idx]
            new_select = [_rewrite_alias(e, remove_alias, keep_alias) for e in ir.select]
            new_where = _rewrite_alias(ir.where, remove_alias, keep_alias) if ir.where else None
            new_joins = [
                JoinClause(
                    join_type=j.join_type,
                    right=j.right,
                    on=_rewrite_alias(j.on, remove_alias, keep_alias),
                )
                for j in new_joins
            ]
            collapsed = QueryIR(
                select=new_select,
                from_table=ir.from_table,
                joins=new_joins,
                where=new_where,
                group_by=[_rewrite_alias(g, remove_alias, keep_alias) for g in ir.group_by],
                having=_rewrite_alias(ir.having, remove_alias, keep_alias) if ir.having else None,
                order_by=ir.order_by,
                limit=ir.limit,
                distinct=ir.distinct,
            )
            return [collapsed]

    return []


def _extract_equi_col(on_expr, alias: str) -> Optional[str]:
    """Extract the column used in an equi-join ON predicate for the given alias."""
    if not isinstance(on_expr, BinOp) or on_expr.op != BinOpKind.EQ:
        return None
    if isinstance(on_expr.left, ColumnRef) and (on_expr.left.table or "").lower() == alias:
        return on_expr.left.column.lower()
    if isinstance(on_expr.right, ColumnRef) and (on_expr.right.table or "").lower() == alias:
        return on_expr.right.column.lower()
    return None


def _collect_aliases(expr) -> set[str]:
    """Collect all table aliases referenced in an expression."""
    aliases: set[str] = set()
    if expr is None:
        return aliases
    if isinstance(expr, ColumnRef) and expr.table:
        aliases.add(expr.table.lower())
    elif isinstance(expr, BinOp):
        aliases |= _collect_aliases(expr.left)
        aliases |= _collect_aliases(expr.right)
    elif isinstance(expr, UnaryOp):
        aliases |= _collect_aliases(expr.operand)
    elif isinstance(expr, FuncCall):
        for a in expr.args:
            aliases |= _collect_aliases(a)
    elif isinstance(expr, AggCall) and expr.arg:
        aliases |= _collect_aliases(expr.arg)
    return aliases


def _rewrite_alias(expr, old_alias: str, new_alias: str):
    """Rewrite all ColumnRef references from old_alias to new_alias."""
    if expr is None:
        return None
    if isinstance(expr, ColumnRef):
        if (expr.table or "").lower() == old_alias:
            return ColumnRef(
                table=new_alias, column=expr.column,
                sem_type=expr.sem_type, nullability=expr.nullability,
                alias=expr.alias,
            )
        return expr
    if isinstance(expr, BinOp):
        return BinOp(
            op=expr.op,
            left=_rewrite_alias(expr.left, old_alias, new_alias),
            right=_rewrite_alias(expr.right, old_alias, new_alias),
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    if isinstance(expr, UnaryOp):
        return UnaryOp(
            op=expr.op,
            operand=_rewrite_alias(expr.operand, old_alias, new_alias),
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    if isinstance(expr, FuncCall):
        return FuncCall(
            func_name=expr.func_name,
            args=[_rewrite_alias(a, old_alias, new_alias) for a in expr.args],
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    if isinstance(expr, AggCall):
        return AggCall(
            func=expr.func,
            arg=_rewrite_alias(expr.arg, old_alias, new_alias),
            distinct=expr.distinct,
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    return expr


def rule_cte_inline(ir, catalog):
    """Stub: CTE inline removed."""
    return []
