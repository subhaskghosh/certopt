"""Preprocessing passes for witness synthesis scalability.

These transforms reduce the number of tables and normalize join structure
before combo enumeration, enabling synthesis on queries with 7–16 tables
that would otherwise exceed the combo safety limit.

Pass 1 — promote_predicates_to_on:
    Move equi-join WHERE predicates to JOIN ON; convert CROSS → INNER.
    This reconstructs explicit join structure from implicit join syntax
    (comma-separated FROM) and is a prerequisite for table elimination.

Pass 2 — eliminate_redundant_tables:
    Remove tables whose columns are not referenced outside their own
    JOIN ON clause, when the join is INNER on a FK→PK relationship
    with a non-nullable FK column (guaranteeing exactly one match).

Pass 3 — existence-only table rewrite:
    Detect INNER-joined tables whose columns never appear in SELECT,
    GROUP BY, HAVING, or ORDER BY (only in WHERE / ON for filtering).
    Rewrite those joins as EXISTS subqueries, reducing combo count.
    Only applied when the query uses DISTINCT (so multiplicity is safe).

Combined entry point:
    preprocess_for_synthesis(ir, catalog) → QueryIR
"""

from __future__ import annotations

import logging
from typing import Optional

from ..ir.types import (
    BinOp,
    BinOpKind,
    ColumnRef,
    Expr,
    JoinClause,
    JoinType,
    Literal,
    QueryIR,
    RelRef,
)
from ..schema.catalog import Catalog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (duplicated from rewrite/rules.py to avoid cross-package import)
# ---------------------------------------------------------------------------

def _collect_and_conjuncts(expr: Optional[Expr]) -> list[Expr]:
    """Flatten top-level AND into a list of conjuncts."""
    if expr is None:
        return []
    if isinstance(expr, BinOp) and expr.op == BinOpKind.AND:
        return _collect_and_conjuncts(expr.left) + _collect_and_conjuncts(expr.right)
    return [expr]


def _rebuild_and(conjuncts: list[Expr]) -> Optional[Expr]:
    """Rebuild a chain of AND from a list of conjuncts."""
    if not conjuncts:
        return None
    result = conjuncts[0]
    for c in conjuncts[1:]:
        result = BinOp(op=BinOpKind.AND, left=result, right=c)
    return result


def _is_equi_join_predicate(expr: Expr) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Check if expr is ``t1.col = t2.col`` (equi-join between two different tables).

    Returns (left_table, left_col, right_table, right_col) or (None,)*4 if not.
    """
    if not isinstance(expr, BinOp) or expr.op != BinOpKind.EQ:
        return None, None, None, None
    left, right = expr.left, expr.right
    if not isinstance(left, ColumnRef) or not isinstance(right, ColumnRef):
        return None, None, None, None
    lt = (left.table or "").lower()
    rt = (right.table or "").lower()
    if not lt or not rt or lt == rt:
        return None, None, None, None
    return lt, left.column.lower(), rt, right.column.lower()


def _collect_column_refs(expr: Optional[Expr]) -> set[tuple[str, str]]:
    """Collect all (table, column) pairs referenced in an expression."""
    from ..ir.types import (
        AggCall, CaseExpr, FuncCall, InList, Between,
        UnaryOp, ExistsSubquery, InSubquery, ScalarSubquery, WindowFunc,
    )
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
    elif isinstance(expr, Between):
        refs |= _collect_column_refs(expr.expr)
        refs |= _collect_column_refs(expr.low)
        refs |= _collect_column_refs(expr.high)
    elif isinstance(expr, CaseExpr):
        for cw in expr.whens:
            refs |= _collect_column_refs(cw.when)
            refs |= _collect_column_refs(cw.then)
        refs |= _collect_column_refs(expr.else_)
    elif isinstance(expr, WindowFunc):
        for a in expr.args:
            refs |= _collect_column_refs(a)
        for p in expr.partition_by:
            refs |= _collect_column_refs(p)
        for o in expr.order_by:
            refs |= _collect_column_refs(o.expr)
    elif isinstance(expr, (ScalarSubquery, InSubquery, ExistsSubquery)):
        pass  # Don't descend into subqueries
    return refs


def _get_table_refs(expr: Optional[Expr]) -> set[str]:
    """Get just the table names referenced in an expression."""
    return {t for t, _ in _collect_column_refs(expr) if t}


# ---------------------------------------------------------------------------
# Pass 1: Predicate-to-ON promotion
# ---------------------------------------------------------------------------

def promote_predicates_to_on(ir: QueryIR) -> tuple[QueryIR, int]:
    """Move equi-join WHERE predicates to JOIN ON; convert CROSS → INNER.

    For each WHERE conjunct of the form ``t1.col = t2.col``, check if
    one of the referenced tables is on the right side of a CROSS JOIN
    (or an INNER JOIN with no meaningful ON predicate). If so, move the
    predicate to that join's ON clause and convert CROSS to INNER.

    Only handles CROSS and INNER joins (outer joins are left untouched).

    Returns (new_ir, n_promoted).
    """
    if ir.where is None or not ir.joins:
        return ir, 0

    conjuncts = _collect_and_conjuncts(ir.where)
    if not conjuncts:
        return ir, 0

    # Build alias → join index map (including from_table at index -1)
    from_alias = ir.from_table.ref_name.lower() if ir.from_table else ""
    alias_to_join_idx: dict[str, int] = {from_alias: -1}
    for idx, j in enumerate(ir.joins):
        alias_to_join_idx[j.right.ref_name.lower()] = idx

    # Aliases available on the "left side" at each join position
    # At join i, left-side = {from_alias} ∪ {joins[0..i-1].right.alias}
    left_at: dict[int, set[str]] = {}
    left_so_far: set[str] = {from_alias}
    for idx in range(len(ir.joins)):
        left_at[idx] = set(left_so_far)
        left_so_far.add(ir.joins[idx].right.ref_name.lower())

    promoted: list[tuple[int, Expr]] = []  # (join_idx, conjunct)
    remaining: list[Expr] = []

    for conj in conjuncts:
        lt, lc, rt, rc = _is_equi_join_predicate(conj)
        if lt is None:
            remaining.append(conj)
            continue

        # Try to match this predicate to a CROSS/INNER join
        matched_idx = None

        # Case 1: right table is on a CROSS/INNER join's right side,
        # and left table is available on the left side at that position
        if rt in alias_to_join_idx:
            idx = alias_to_join_idx[rt]
            if idx >= 0 and ir.joins[idx].join_type in (JoinType.CROSS, JoinType.INNER):
                if lt in left_at.get(idx, set()):
                    matched_idx = idx

        # Case 2: symmetric — left table is on a CROSS/INNER join's right side
        if matched_idx is None and lt in alias_to_join_idx:
            idx = alias_to_join_idx[lt]
            if idx >= 0 and ir.joins[idx].join_type in (JoinType.CROSS, JoinType.INNER):
                if rt in left_at.get(idx, set()):
                    matched_idx = idx

        if matched_idx is not None:
            promoted.append((matched_idx, conj))
        else:
            remaining.append(conj)

    if not promoted:
        return ir, 0

    new_ir = ir.model_copy(deep=True)

    # Apply promoted predicates
    for idx, conj in promoted:
        join = new_ir.joins[idx]
        # If the join currently has a trivial ON (TRUE literal or no predicate),
        # replace it; otherwise AND with existing ON
        old_on = join.on
        is_trivial = (
            old_on is None
            or (isinstance(old_on, Literal) and old_on.value is True)
        )
        if is_trivial:
            new_ir.joins[idx].on = conj
        else:
            new_ir.joins[idx].on = BinOp(op=BinOpKind.AND, left=old_on, right=conj)

        # Convert CROSS → INNER
        if join.join_type == JoinType.CROSS:
            new_ir.joins[idx].join_type = JoinType.INNER

    # Update WHERE
    new_ir.where = _rebuild_and(remaining)

    logger.debug("Predicate promotion: promoted %d predicates to ON", len(promoted))
    return new_ir, len(promoted)


# ---------------------------------------------------------------------------
# Pass 2: Redundant table elimination
# ---------------------------------------------------------------------------

def _all_referenced_tables(ir: QueryIR) -> set[str]:
    """Collect all table aliases referenced in SELECT, WHERE, GROUP BY,
    HAVING, ORDER BY (not FROM/JOIN declarations or JOIN ON)."""
    refs: set[str] = set()
    for sel in ir.select:
        refs |= _get_table_refs(sel)
    refs |= _get_table_refs(ir.where)
    refs |= _get_table_refs(ir.having)
    for g in ir.group_by:
        refs |= _get_table_refs(g)
    for s in ir.order_by:
        refs |= _get_table_refs(s.expr)
    return refs


def eliminate_redundant_tables(ir: QueryIR, catalog: Catalog) -> tuple[QueryIR, list[str]]:
    """Remove joins to tables that don't affect query output.

    A joined table can be eliminated if ALL of these hold:
    1. INNER join only
    2. Table not referenced in SELECT, WHERE (non-join), GROUP BY, HAVING, ORDER BY
    3. Table not referenced in any other join's ON clause
    4. The join ON is an equi-join on a FK→PK/UNIQUE relationship
    5. The FK column is NOT NULL (ensuring exactly one match per row)

    Applies iteratively until no more eliminations are possible.

    Returns (new_ir, list_of_eliminated_table_aliases).
    """
    eliminated: list[str] = []
    current = ir

    # Iterate until fixed point (eliminating one table may make another eliminable)
    for _ in range(len(ir.joins)):
        removed_any = False

        for idx in range(len(current.joins) - 1, -1, -1):
            join = current.joins[idx]

            # Condition 1: INNER join only
            if join.join_type != JoinType.INNER:
                continue
            if not isinstance(join.right, RelRef):
                continue

            join_alias = (join.right.alias or join.right.table).lower()
            join_real_table = join.right.table.lower()

            # Condition 2: not referenced in output-affecting clauses
            output_refs = _all_referenced_tables(current)
            if join_alias in output_refs:
                continue

            # Condition 3: not referenced in other joins' ON clauses
            other_join_refs: set[str] = set()
            for other_idx, other_j in enumerate(current.joins):
                if other_idx == idx:
                    continue
                other_join_refs |= _get_table_refs(other_j.on)
            if join_alias in other_join_refs:
                continue

            # Condition 4 + 5: ON matches a FK→PK/UNIQUE with non-null FK
            if not _is_safe_fk_pk_join(join, current, catalog):
                continue

            # Safe to eliminate
            new_ir = current.model_copy(deep=True)
            new_ir.joins = [j for i, j in enumerate(current.joins) if i != idx]
            current = new_ir
            eliminated.append(join_alias)
            removed_any = True
            break  # Restart scan after removal

        if not removed_any:
            break

    if eliminated:
        logger.debug("Table elimination: removed %d tables: %s", len(eliminated), eliminated)

    return current, eliminated


def _is_safe_fk_pk_join(
    join: JoinClause,
    ir: QueryIR,
    catalog: Catalog,
) -> bool:
    """Check if a join's ON clause matches a FK→PK/UNIQUE relationship
    with a non-nullable FK column.

    The join is safe to eliminate if:
    - ON contains an equality t_left.col = t_right.col (possibly among ANDs)
    - One side's column is a PK/UNIQUE in the catalog
    - The other side's column is a FK pointing to that PK
    - The FK column is NOT NULL
    """
    if not isinstance(join.right, RelRef):
        return False

    join_alias = (join.right.alias or join.right.table).lower()
    join_real_table = join.right.table.lower()

    # Get the from_table alias
    from_alias = ir.from_table.ref_name.lower() if ir.from_table else ""

    # Build alias → real table map
    alias_to_real: dict[str, str] = {from_alias: ""}
    if isinstance(ir.from_table, RelRef):
        alias_to_real[from_alias] = ir.from_table.table.lower()
    for j in ir.joins:
        if isinstance(j.right, RelRef):
            a = (j.right.alias or j.right.table).lower()
            alias_to_real[a] = j.right.table.lower()

    # Extract equi-join predicates from ON
    on_conjuncts = _collect_and_conjuncts(join.on)

    for conj in on_conjuncts:
        lt, lc, rt, rc = _is_equi_join_predicate(conj)
        if lt is None:
            continue

        # Determine which side is the join table we're eliminating
        # and which side is the "kept" table
        if rt == join_alias:
            kept_alias, kept_col = lt, lc
            elim_alias, elim_col = rt, rc
        elif lt == join_alias:
            kept_alias, kept_col = rt, rc
            elim_alias, elim_col = lt, lc
        else:
            continue

        kept_real = alias_to_real.get(kept_alias, kept_alias)
        elim_real = alias_to_real.get(elim_alias, elim_alias)

        # Check: elim table's column is PK or UNIQUE
        elim_tinfo = catalog.get_table(elim_real)
        if elim_tinfo is None:
            continue
        pk_lower = [pk.lower() for pk in elim_tinfo.primary_keys]
        uq_lower = [uq.lower() for uq in elim_tinfo.unique_columns]
        if elim_col not in pk_lower and elim_col not in uq_lower:
            continue

        # Check: there's a FK from kept.col → elim.col
        has_fk = False
        for fk in catalog.foreign_keys:
            if (fk.src_table.lower() == kept_real
                    and fk.src_column.lower() == kept_col
                    and fk.dst_table.lower() == elim_real
                    and fk.dst_column.lower() == elim_col):
                has_fk = True
                break

        if not has_fk:
            continue

        # Check: FK column (kept side) is NOT NULL
        kept_tinfo = catalog.get_table(kept_real)
        if kept_tinfo is None:
            continue
        kept_cinfo = kept_tinfo.get_column(kept_col)
        if kept_cinfo is None:
            continue
        if kept_cinfo.nullable:
            continue

        return True

    return False


# ---------------------------------------------------------------------------
# Pass 3: Existence-only table detection and rewrite
# ---------------------------------------------------------------------------

def _all_select_table_refs(ir: QueryIR) -> set[str]:
    """Collect table aliases referenced in SELECT, GROUP BY, HAVING, ORDER BY.

    These are 'projection-referenced' tables — their columns appear in the output.
    This does NOT include WHERE — a table referenced only in WHERE is existence-only.
    """
    refs: set[str] = set()
    for sel in ir.select:
        refs |= _get_table_refs(sel)
    for g in ir.group_by:
        refs |= _get_table_refs(g)
    refs |= _get_table_refs(ir.having)
    for s in ir.order_by:
        refs |= _get_table_refs(s.expr)
    return refs


def detect_existence_only_tables(ir: QueryIR, catalog: Catalog) -> list[str]:
    """Identify tables that are only used to filter (never projected).

    A table T (joined via INNER JOIN) is existence-only if:
      1. T is not referenced in SELECT, GROUP BY, HAVING, ORDER BY
      2. The join to T is INNER
      3. T is not referenced in other joins' ON clauses (except as the join partner)
      5. The query uses DISTINCT (so row multiplicity from the join
         does not affect the result set)

    Returns list of alias names that are existence-only.
    """
    # The EXISTS rewrite changes row multiplicity: a 1:many join can
    # produce duplicate rows on the "one" side, but EXISTS does not.
    # This is only safe when the query already deduplicates via DISTINCT.
    if not ir.distinct:
        return []

    # Tables that appear in output
    output_refs = _all_select_table_refs(ir)

    existence_only: list[str] = []

    for idx, join in enumerate(ir.joins):
        if join.join_type != JoinType.INNER:
            continue
        if not isinstance(join.right, RelRef):
            continue

        join_alias = join.right.ref_name.lower()

        # Condition 1: not in output
        if join_alias in output_refs:
            continue

        # Condition 3: not referenced in other joins' ON clauses
        other_on_refs: set[str] = set()
        for other_idx, other_j in enumerate(ir.joins):
            if other_idx == idx:
                continue
            other_on_refs |= _get_table_refs(other_j.on)
        if join_alias in other_on_refs:
            continue

        existence_only.append(join_alias)

    return existence_only


def rewrite_existence_tables(
    ir: QueryIR,
    catalog: Catalog,
    existence_tables: list[str],
) -> tuple[QueryIR, list[str]]:
    """Rewrite existence-only joins as EXISTS subqueries.

    For each existence-only table T:
      1. Collect the JOIN ON predicate for T
      2. Collect WHERE conjuncts that reference T
      3. Build: EXISTS(SELECT 1 FROM T WHERE on_pred AND where_preds)
      4. Remove the JOIN
      5. Add the EXISTS to WHERE
      6. Remove T's WHERE conjuncts from the main WHERE

    Returns (rewritten_ir, list_of_rewritten_aliases).
    """
    if not existence_tables:
        return ir, []

    from ..ir.types import ExistsSubquery

    rewritten: list[str] = []
    current = ir.model_copy(deep=True)

    for target_alias in existence_tables:
        # Find the join for this alias
        join_idx = None
        join_clause = None
        for idx, j in enumerate(current.joins):
            if isinstance(j.right, RelRef) and j.right.ref_name.lower() == target_alias:
                join_idx = idx
                join_clause = j
                break

        if join_idx is None:
            continue

        # Collect the ON predicate
        on_pred = join_clause.on

        # Collect WHERE conjuncts that reference this table
        where_conjuncts = _collect_and_conjuncts(current.where)
        table_conjuncts: list[Expr] = []
        remaining_conjuncts: list[Expr] = []

        for conj in where_conjuncts:
            conj_tables = _get_table_refs(conj)
            if target_alias in conj_tables:
                table_conjuncts.append(conj)
            else:
                remaining_conjuncts.append(conj)

        # Build the EXISTS subquery inner predicate:
        # ON predicate AND all WHERE conjuncts that reference the table
        inner_preds: list[Expr] = [on_pred]
        inner_preds.extend(table_conjuncts)

        inner_where = _rebuild_and(inner_preds)

        # Build the inner SELECT 1 FROM T WHERE ...
        inner_query = QueryIR(
            select=[Literal(value=1)],
            from_table=join_clause.right.model_copy(deep=True),
            where=inner_where,
        )

        # Build EXISTS(inner_query)
        exists_expr = ExistsSubquery(query=inner_query)

        # Remove the join
        new_joins = [j for i, j in enumerate(current.joins) if i != join_idx]

        # Update WHERE: remaining conjuncts AND EXISTS
        if remaining_conjuncts:
            new_where = _rebuild_and(remaining_conjuncts)
            # AND with EXISTS
            new_where = BinOp(op=BinOpKind.AND, left=new_where, right=exists_expr)
        else:
            new_where = exists_expr

        current = current.model_copy(deep=True)
        current.joins = new_joins
        current.where = new_where

        rewritten.append(target_alias)
        logger.debug("Existence rewrite: %s → EXISTS subquery", target_alias)

    return current, rewritten


# ---------------------------------------------------------------------------
# C.2: Multiplicity-neutral join detection
# ---------------------------------------------------------------------------

def detect_multiplicity_neutral_joins(ir: QueryIR, catalog: Catalog) -> list[tuple[str, int]]:
    """Identify joins that are provably 1:1 (won't duplicate rows).

    A join to table T is multiplicity-neutral if:
      1. Join type is INNER
      2. Join ON predicate includes an equi-join on T's PK or UNIQUE column
      3. The FK side's column is NOT NULL
      4. The FK→PK relationship exists in the catalog

    Unlike eliminate_redundant_tables, this does NOT require the table
    to be unreferenced — the key insight is that a 1:1 FK→PK join
    never changes result cardinality.

    Returns list of (alias, join_index) for multiplicity-neutral joins.
    """
    results: list[tuple[str, int]] = []

    for idx, join in enumerate(ir.joins):
        if join.join_type != JoinType.INNER:
            continue
        if not isinstance(join.right, RelRef):
            continue

        # Check if this join's ON matches FK→PK with NOT NULL FK
        # We can reuse _is_safe_fk_pk_join which checks exactly this
        if _is_safe_fk_pk_join(join, ir, catalog):
            join_alias = join.right.ref_name.lower()
            results.append((join_alias, idx))

    return results


# ---------------------------------------------------------------------------
# C.3: Implied-predicate join elimination
# ---------------------------------------------------------------------------

def detect_implied_predicates(
    ir: QueryIR, catalog: Catalog
) -> list[tuple[int, str, list[Expr]]]:
    """Detect joins where predicates on the joined table can be transplanted.

    If a join to table T is:
      1. INNER JOIN on a.fk = T.pk (FK→PK relationship)
      2. T is existence-only (not projected in SELECT/GROUP BY/HAVING/ORDER BY)
      3. T is not referenced in other joins' ON clauses
      4. All WHERE predicates referencing T are on T's PK column
         (i.e., predicates of the form T.pk op value)

    Then those predicates can be transplanted to the FK side:
      T.pk IN (1, 2, 3) → a.fk IN (1, 2, 3)
      T.pk = 5 → a.fk = 5
      T.pk BETWEEN 1 AND 10 → a.fk BETWEEN 1 AND 10

    Returns list of (join_index, target_alias, transplantable_predicates).
    """
    from ..ir.types import ExistsSubquery

    output_refs = _all_select_table_refs(ir)
    from_alias = ir.from_table.ref_name.lower() if ir.from_table else ""

    # Build alias → real table map
    alias_to_real: dict[str, str] = {}
    if isinstance(ir.from_table, RelRef):
        alias_to_real[from_alias] = ir.from_table.table.lower()
    for j in ir.joins:
        if isinstance(j.right, RelRef):
            a = j.right.ref_name.lower()
            alias_to_real[a] = j.right.table.lower()

    results: list[tuple[int, str, list[Expr]]] = []

    for idx, join in enumerate(ir.joins):
        if join.join_type != JoinType.INNER:
            continue
        if not isinstance(join.right, RelRef):
            continue

        join_alias = join.right.ref_name.lower()
        join_real = alias_to_real.get(join_alias, join_alias)

        # Must be existence-only
        if join_alias in output_refs:
            continue

        # Not referenced in other joins' ON
        other_on_refs: set[str] = set()
        for other_idx, other_j in enumerate(ir.joins):
            if other_idx == idx:
                continue
            other_on_refs |= _get_table_refs(other_j.on)
        if join_alias in other_on_refs:
            continue

        # Must have FK→PK join
        if not _is_safe_fk_pk_join(join, ir, catalog):
            continue

        # Find the FK→PK column mapping from the ON clause
        on_conjuncts = _collect_and_conjuncts(join.on)
        fk_mapping: dict[str, tuple[str, str]] = {}  # elim_col → (kept_alias, kept_col)

        for conj in on_conjuncts:
            lt, lc, rt, rc = _is_equi_join_predicate(conj)
            if lt is None:
                continue
            if rt == join_alias:
                fk_mapping[rc] = (lt, lc)
            elif lt == join_alias:
                fk_mapping[lc] = (rt, rc)

        if not fk_mapping:
            continue

        # Check WHERE predicates referencing this table
        where_conjuncts = _collect_and_conjuncts(ir.where)
        transplantable: list[Expr] = []
        all_transplantable = True

        for conj in where_conjuncts:
            conj_tables = _get_table_refs(conj)
            if join_alias not in conj_tables:
                continue

            # Check if ALL column refs to join_alias are to PK columns
            # that have FK mappings
            conj_refs = _collect_column_refs(conj)
            join_cols_used = [(t, c) for t, c in conj_refs if t == join_alias]

            can_transplant = True
            for t, c in join_cols_used:
                if c not in fk_mapping:
                    can_transplant = False
                    break

            # Also check that no OTHER table refs are involved
            # (besides the join table itself)
            other_tables = {t for t, _ in conj_refs if t and t != join_alias}
            if other_tables:
                can_transplant = False

            if can_transplant:
                transplantable.append(conj)
            else:
                all_transplantable = False

        if transplantable and all_transplantable:
            results.append((idx, join_alias, transplantable))

    return results


def eliminate_with_transplant(
    ir: QueryIR, catalog: Catalog,
    transplant_info: list[tuple[int, str, list[Expr]]],
) -> tuple[QueryIR, list[str]]:
    """Eliminate joins by transplanting predicates to the FK side.

    For each (join_idx, alias, predicates):
      1. Rewrite each predicate: replace T.pk_col refs with kept.fk_col refs
      2. Remove the join
      3. Replace the original predicates in WHERE with transplanted versions

    Returns (new_ir, list_of_eliminated_aliases).
    """
    if not transplant_info:
        return ir, []

    eliminated: list[str] = []
    current = ir.model_copy(deep=True)

    # Process in reverse join order to keep indices valid
    for join_idx, target_alias, predicates in sorted(transplant_info, key=lambda x: -x[0]):
        join = current.joins[join_idx]

        # Build FK mapping from ON clause
        on_conjuncts = _collect_and_conjuncts(join.on)
        fk_mapping: dict[str, tuple[str, str]] = {}

        for conj in on_conjuncts:
            lt, lc, rt, rc = _is_equi_join_predicate(conj)
            if lt is None:
                continue
            if rt == target_alias:
                fk_mapping[rc] = (lt, lc)
            elif lt == target_alias:
                fk_mapping[lc] = (rt, rc)

        # Transplant predicates — identify by checking table refs, not object identity
        # (deep copy may have changed object IDs)
        where_conjuncts = _collect_and_conjuncts(current.where)
        new_conjuncts: list[Expr] = []

        for conj in where_conjuncts:
            conj_tables = _get_table_refs(conj)
            if target_alias in conj_tables:
                # Check if this conjunct is transplantable (all refs to target
                # alias use columns in fk_mapping, no other table refs)
                conj_refs = _collect_column_refs(conj)
                join_cols = [(t, c) for t, c in conj_refs if t == target_alias]
                other_tables = {t for t, _ in conj_refs if t and t != target_alias}
                if not other_tables and all(c in fk_mapping for _, c in join_cols):
                    transplanted = _transplant_refs(conj, target_alias, fk_mapping)
                    new_conjuncts.append(transplanted)
                # else: drop non-transplantable predicates referencing eliminated table
            else:
                new_conjuncts.append(conj)

        # Remove the join
        new_joins = [j for i, j in enumerate(current.joins) if i != join_idx]

        current = current.model_copy(deep=True)
        current.joins = new_joins
        current.where = _rebuild_and(new_conjuncts)
        eliminated.append(target_alias)
        logger.debug("Implied-predicate elimination: %s removed, predicates transplanted", target_alias)

    return current, eliminated


def _transplant_refs(
    expr: Expr, source_alias: str, fk_mapping: dict[str, tuple[str, str]]
) -> Expr:
    """Replace ColumnRef(table=source_alias, column=col) with ColumnRef(table=kept, column=fk_col)."""
    from ..ir.types import UnaryOp, InList, Between

    if isinstance(expr, ColumnRef):
        if (expr.table or "").lower() == source_alias:
            mapping = fk_mapping.get(expr.column.lower())
            if mapping:
                kept_alias, kept_col = mapping
                return ColumnRef(
                    table=kept_alias, column=kept_col,
                    sem_type=expr.sem_type, alias=expr.alias,
                )
        return expr
    if isinstance(expr, BinOp):
        return BinOp(
            op=expr.op,
            left=_transplant_refs(expr.left, source_alias, fk_mapping),
            right=_transplant_refs(expr.right, source_alias, fk_mapping),
            sem_type=expr.sem_type, alias=expr.alias,
        )
    if isinstance(expr, UnaryOp):
        return UnaryOp(
            op=expr.op,
            operand=_transplant_refs(expr.operand, source_alias, fk_mapping),
            sem_type=expr.sem_type, alias=expr.alias,
        )
    if isinstance(expr, InList):
        return InList(
            expr=_transplant_refs(expr.expr, source_alias, fk_mapping),
            values=[_transplant_refs(v, source_alias, fk_mapping) for v in expr.values],
            sem_type=expr.sem_type, alias=expr.alias,
        )
    if isinstance(expr, Between):
        return Between(
            expr=_transplant_refs(expr.expr, source_alias, fk_mapping),
            low=_transplant_refs(expr.low, source_alias, fk_mapping),
            high=_transplant_refs(expr.high, source_alias, fk_mapping),
            sem_type=expr.sem_type, alias=expr.alias,
        )
    return expr


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------

def preprocess_for_synthesis(ir: QueryIR, catalog: Catalog) -> tuple[QueryIR, dict]:
    """Apply all preprocessing passes before witness synthesis.

    Returns (preprocessed_ir, stats_dict) where stats_dict contains:
        - tables_before: int
        - tables_after: int
        - promoted: int (predicates moved from WHERE to ON)
        - eliminated: list[str] (removed table aliases)
        - existence_rewritten: list[str] (aliases rewritten to EXISTS)
    """
    n_tables_before = 1 + len(ir.joins)

    # Pass 1: Predicate-to-ON promotion
    ir, n_promoted = promote_predicates_to_on(ir)

    # Pass 2: Redundant table elimination
    ir, eliminated = eliminate_redundant_tables(ir, catalog)

    # Pass 3: Existence-only table rewrite (join → EXISTS subquery)
    existence_tables = detect_existence_only_tables(ir, catalog)
    ir, existence_rewritten = rewrite_existence_tables(ir, catalog, existence_tables)

    # Pass 4: Implied-predicate join elimination
    transplant_info = detect_implied_predicates(ir, catalog)
    ir, transplant_eliminated = eliminate_with_transplant(ir, catalog, transplant_info)

    n_tables_after = 1 + len(ir.joins)

    stats = {
        "tables_before": n_tables_before,
        "tables_after": n_tables_after,
        "promoted": n_promoted,
        "eliminated": eliminated,
        "existence_rewritten": existence_rewritten,
        "transplant_eliminated": transplant_eliminated,
    }

    if n_tables_before != n_tables_after:
        logger.info(
            "Preprocessing: %d tables → %d tables "
            "(%d predicates promoted, %d tables eliminated: %s, "
            "%d existence-rewritten: %s, %d transplant-eliminated: %s)",
            n_tables_before, n_tables_after, n_promoted,
            len(eliminated), eliminated,
            len(existence_rewritten), existence_rewritten,
            len(transplant_eliminated), transplant_eliminated,
        )

    return ir, stats
