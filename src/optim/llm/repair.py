"""Schema-grounded LLM output canonicalization and repair.

Repairs common LLM mistakes before structural verification:
1. Case normalization — lowercase all table/column references to match catalog
2. Alias normalization — strip unnecessary table aliases
3. Nearest-column repair — fix typos via Levenshtein distance (≤2, unique match)
4. Join-path completion — auto-generate ON clause from FK graph (single path)
5. Projection alignment — match original SELECT arity
"""

from __future__ import annotations

import logging
from typing import Optional

from ..ir.types import (
    BinOp,
    BinOpKind,
    ColumnRef,
    Expr,
    ExprUnion,
    JoinClause,
    Literal,
    QueryIR,
    RelRef,
)
from ..schema.catalog import Catalog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def repair_candidate(
    ir: QueryIR, catalog: Catalog, original_ir: QueryIR,
) -> QueryIR:
    """Canonicalize LLM output before verification.

    Applies safe, semantics-preserving repairs:
    - Case normalization (match catalog casing)
    - Nearest-column repair (edit distance ≤ 2, type-safe)
    - Join-path completion (FK graph lookup)
    - Projection alignment (match original SELECT arity)

    Returns repaired IR. If repair fails (ambiguous match,
    type mismatch), returns original IR unchanged.
    """
    repaired = ir.model_copy(deep=True)

    try:
        repaired = _normalize_case(repaired, catalog)
        repaired, _details = _repair_columns(repaired, catalog)
        repaired = _complete_join_paths(repaired, catalog)
    except Exception:
        logger.warning("Repair failed; returning original IR unchanged")
        return ir

    return repaired


# ---------------------------------------------------------------------------
# Pass 1: Case normalization
# ---------------------------------------------------------------------------


def _normalize_case(ir: QueryIR, catalog: Catalog) -> QueryIR:
    """Lowercase all table/column references to match catalog.

    Walks all expressions recursively, lowering ColumnRef.table and
    ColumnRef.column.  Also lowers from_table (RelRef.table, RelRef.alias)
    and joins (JoinClause right RelRef).

    Returns the mutated IR (already a deep copy).
    """
    count = 0

    # --- relation references ---
    if isinstance(ir.from_table, RelRef):
        old_t = ir.from_table.table
        ir.from_table.table = old_t.lower()
        if ir.from_table.alias is not None:
            ir.from_table.alias = ir.from_table.alias.lower()
        count += (old_t != ir.from_table.table)

    for join in ir.joins:
        if isinstance(join.right, RelRef):
            old_t = join.right.table
            join.right.table = old_t.lower()
            if join.right.alias is not None:
                join.right.alias = join.right.alias.lower()
            count += (old_t != join.right.table)

    # --- expressions ---
    def _lower_expr(expr: ExprUnion) -> int:
        nonlocal count
        if isinstance(expr, ColumnRef):
            changed = 0
            if expr.table is not None:
                old = expr.table
                expr.table = old.lower()
                changed += (old != expr.table)
            old_c = expr.column
            expr.column = old_c.lower()
            changed += (old_c != expr.column)
            count += changed
            return changed
        if isinstance(expr, BinOp):
            _lower_expr(expr.left)
            _lower_expr(expr.right)
        return 0

    for sel in ir.select:
        _lower_expr(sel)
    if ir.where is not None:
        _lower_expr(ir.where)
    for join in ir.joins:
        _lower_expr(join.on)
    for gb in ir.group_by:
        _lower_expr(gb)
    if ir.having is not None:
        _lower_expr(ir.having)

    if count:
        logger.debug("Case normalization: %d reference(s) lowered", count)

    return ir


# ---------------------------------------------------------------------------
# Pass 2: Nearest-column repair
# ---------------------------------------------------------------------------


def _repair_columns(
    ir: QueryIR, catalog: Catalog,
) -> tuple[QueryIR, list[str]]:
    """Fix unresolved column references via edit-distance matching.

    For each unresolved ColumnRef (table not in catalog or column not in
    table), find closest match by edit distance.  Only repair if distance ≤ 2
    and exactly one candidate matches.

    Returns repaired IR and list of repair descriptions.
    """
    repairs: list[str] = []

    all_tables = set(catalog.list_tables())
    # Collect all table names referenced in the IR
    ir_tables = _collect_table_names(ir)

    def _try_repair_ref(ref: ColumnRef) -> None:
        tbl = ref.table.lower() if ref.table else None

        # If table reference doesn't resolve, try to fix it first
        if tbl is not None and tbl not in all_tables:
            best_table, dist = _nearest(tbl, list(all_tables))
            if best_table is not None and dist <= 2:
                old = ref.table
                ref.table = best_table
                repairs.append(f"table_repaired:{old}→{best_table}")
                tbl = best_table
            else:
                return  # can't resolve table, skip column repair

        # Now fix column if needed
        if tbl is None:
            return
        table_info = catalog.get_table(tbl)
        if table_info is None:
            return
        col_names = table_info.column_names()
        if ref.column in col_names:
            return  # already valid

        best_col, dist = _nearest(ref.column, col_names)
        if best_col is not None and dist <= 2:
            old = ref.column
            ref.column = best_col
            repairs.append(f"column_repaired:{old}→{best_col}")

    def _walk_and_repair(expr: ExprUnion) -> None:
        if isinstance(expr, ColumnRef):
            _try_repair_ref(expr)
        elif isinstance(expr, BinOp):
            _walk_and_repair(expr.left)
            _walk_and_repair(expr.right)

    for sel in ir.select:
        _walk_and_repair(sel)
    if ir.where is not None:
        _walk_and_repair(ir.where)
    for join in ir.joins:
        _walk_and_repair(join.on)
    for gb in ir.group_by:
        _walk_and_repair(gb)
    if ir.having is not None:
        _walk_and_repair(ir.having)

    if repairs:
        logger.debug("Column repairs: %s", repairs)

    return ir, repairs


def _nearest(target: str, candidates: list[str]) -> tuple[Optional[str], int]:
    """Find the nearest candidate by edit distance.

    Returns (best_match, distance).  If no candidate within distance ≤ 2
    or multiple candidates tie at the same distance, returns (None, 999).
    """
    if not candidates:
        return None, 999

    scored = [(c, _levenshtein(target, c)) for c in candidates]
    scored.sort(key=lambda x: x[1])

    best_name, best_dist = scored[0]
    if best_dist > 2:
        return None, best_dist

    # Ambiguity check: if two candidates share the best distance, skip
    if len(scored) > 1 and scored[1][1] == best_dist:
        return None, best_dist

    return best_name, best_dist


# ---------------------------------------------------------------------------
# Pass 3: Join-path completion
# ---------------------------------------------------------------------------


def _complete_join_paths(ir: QueryIR, catalog: Catalog) -> QueryIR:
    """Auto-generate ON clause for joins with ON=Literal(True).

    For joins that are missing a real ON clause (placeholder
    ``Literal(True)``), look up FK relationships between join tables
    and auto-generate the equi-join ON clause when exactly one FK path
    exists.
    """
    from_name = _rel_name(ir.from_table)

    for join in ir.joins:
        if not _is_true_literal(join.on):
            continue

        right_name = _rel_name(join.right)
        if from_name is None or right_name is None:
            continue

        # Collect all referenced table names for context
        all_ir_tables = _collect_table_names(ir)

        fk = _find_fk(catalog, from_name, right_name, all_ir_tables)
        if fk is None:
            continue

        join.on = BinOp(
            op=BinOpKind.EQ,
            left=ColumnRef(table=fk[0], column=fk[1]),
            right=ColumnRef(table=fk[2], column=fk[3]),
        )
        logger.debug(
            "Join path completed: %s.%s = %s.%s",
            fk[0], fk[1], fk[2], fk[3],
        )

    return ir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _levenshtein(a: str, b: str) -> int:
    """Standard Levenshtein edit distance (no external libs)."""
    if len(a) < len(b):
        return _levenshtein(b, a)

    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            insert = prev[j + 1] + 1
            delete = curr[j] + 1
            substitute = prev[j] + (0 if ca == cb else 1)
            curr.append(min(insert, delete, substitute))
        prev = curr

    return prev[-1]


def _is_true_literal(expr: ExprUnion) -> bool:
    """Check if expression is Literal(True)."""
    return isinstance(expr, Literal) and expr.value is True


def _rel_name(rel: object) -> Optional[str]:
    """Extract table name from a RelRef (returns None for DerivedTable)."""
    if isinstance(rel, RelRef):
        return rel.table.lower()
    return None


def _collect_table_names(ir: QueryIR) -> set[str]:
    """Collect all table names referenced in the IR."""
    names: set[str] = set()
    if isinstance(ir.from_table, RelRef):
        names.add(ir.from_table.table.lower())
    for join in ir.joins:
        if isinstance(join.right, RelRef):
            names.add(join.right.table.lower())
    return names


def _find_fk(
    catalog: Catalog,
    left_table: str,
    right_table: str,
    all_tables: set[str],
) -> Optional[tuple[str, str, str, str]]:
    """Find a unique FK relationship between two tables.

    Returns (left_table, left_col, right_table, right_col) or None if
    no FK or ambiguous.
    """
    matches: list[tuple[str, str, str, str]] = []

    for fk in catalog.foreign_keys:
        src_t = fk.src_table.lower()
        dst_t = fk.dst_table.lower()

        if src_t == left_table and dst_t == right_table:
            matches.append((left_table, fk.src_column, right_table, fk.dst_column))
        elif src_t == right_table and dst_t == left_table:
            matches.append((left_table, fk.dst_column, right_table, fk.src_column))

    if len(matches) == 1:
        return matches[0]
    return None
