"""Incremental combo construction for witness synthesis.

Instead of materializing the full cartesian product of row indices
and then evaluating all JOIN ON predicates post-hoc, this module
builds combos by walking the join tree left-to-right, threading
the ON predicate evaluation at each step.

Semantically identical to the eager approach but:
  - Builds survival conditions incrementally: And(parent_survives, on_ok)
  - Produces a cleaner Z3 expression tree (better for the solver)
  - Enables better ExprCache hit rates (shared parent bindings)
"""

from __future__ import annotations

import logging

import z3

from ..ir.types import JoinType, QueryIR
from .witness_synthesis import (
    NullableVal,
    SymbolicDB,
    SymbolicRow,
    SymbolicTable,
    _eval_predicate,
    _make_null_row,
)
from ..verify.encode_z3 import BoundedScope

logger = logging.getLogger(__name__)


def build_combos_incremental(
    ir: QueryIR,
    db: SymbolicDB,
    alias_to_table: dict[str, str],
    scope: BoundedScope,
) -> list[tuple[z3.ExprRef, dict[str, SymbolicRow]]]:
    """Build (survives, binding) pairs by walking the join tree incrementally.

    For INNER joins: each step extends partial bindings with k rows from
    the right table and evaluates the ON predicate.

    For LEFT/RIGHT/FULL joins: also adds unmatched rows with NULL padding.

    Returns the same result as _build_combos_with_outer_joins but with
    incrementally constructed survival expressions.
    """
    k = scope.k_rows
    from_alias = ir.from_table.ref_name.lower()
    from_table = alias_to_table.get(from_alias, from_alias)

    # Start with FROM table: k partial bindings
    partials: list[tuple[z3.ExprRef, dict[str, SymbolicRow]]] = []
    sym_from = db.tables.get(from_table)
    if sym_from is None:
        return []

    for row_i in range(k):
        binding: dict[str, SymbolicRow] = {}
        binding[from_alias] = sym_from.rows[row_i]
        if from_alias != from_table:
            binding[from_table] = sym_from.rows[row_i]
        partials.append((z3.BoolVal(True), binding))

    if not ir.joins:
        return partials

    # Walk each JOIN
    for join in ir.joins:
        right_alias = join.right.ref_name.lower()
        right_table_name = alias_to_table.get(right_alias, right_alias)
        sym_right = db.tables.get(right_table_name)
        if sym_right is None:
            continue

        is_outer = join.join_type in (JoinType.LEFT, JoinType.RIGHT, JoinType.FULL)

        new_partials: list[tuple[z3.ExprRef, dict[str, SymbolicRow]]] = []

        if not is_outer:
            # INNER / CROSS join
            for parent_survives, parent_binding in partials:
                for row_j in range(k):
                    child_binding = dict(parent_binding)
                    child_binding[right_alias] = sym_right.rows[row_j]
                    if right_alias != right_table_name:
                        child_binding[right_table_name] = sym_right.rows[row_j]

                    if join.on is not None:
                        on_ok = _eval_predicate(join.on, child_binding)
                        survives = z3.And(parent_survives, on_ok)
                    else:
                        survives = parent_survives

                    new_partials.append((survives, child_binding))
        else:
            # LEFT / RIGHT / FULL join
            for parent_survives, parent_binding in partials:
                # Matched combos
                match_conditions = []
                for row_j in range(k):
                    child_binding = dict(parent_binding)
                    child_binding[right_alias] = sym_right.rows[row_j]
                    if right_alias != right_table_name:
                        child_binding[right_table_name] = sym_right.rows[row_j]

                    if join.on is not None:
                        on_ok = _eval_predicate(join.on, child_binding)
                    else:
                        on_ok = z3.BoolVal(True)
                    survives = z3.And(parent_survives, on_ok)
                    new_partials.append((survives, child_binding))
                    match_conditions.append(on_ok)

                # LEFT / FULL: unmatched left row (right side is NULL)
                if join.join_type in (JoinType.LEFT, JoinType.FULL):
                    null_right = _make_null_row(sym_right)
                    unmatched_binding = dict(parent_binding)
                    unmatched_binding[right_alias] = null_right
                    if right_alias != right_table_name:
                        unmatched_binding[right_table_name] = null_right
                    no_right_match = z3.Not(z3.Or(match_conditions)) if match_conditions else z3.BoolVal(True)
                    unmatched_survives = z3.And(parent_survives, no_right_match)
                    new_partials.append((unmatched_survives, unmatched_binding))

            # RIGHT / FULL: unmatched right rows (left side is NULL)
            if join.join_type in (JoinType.RIGHT, JoinType.FULL):
                for row_j in range(k):
                    right_binding: dict[str, SymbolicRow] = {}
                    right_binding[right_alias] = sym_right.rows[row_j]
                    if right_alias != right_table_name:
                        right_binding[right_table_name] = sym_right.rows[row_j]
                    # NULL out all prior aliases
                    for prev_alias, prev_row in partials[0][1].items():
                        if prev_alias not in right_binding:
                            prev_table_name = alias_to_table.get(prev_alias, prev_alias)
                            sym_prev = db.tables.get(prev_table_name)
                            if sym_prev:
                                right_binding[prev_alias] = _make_null_row(sym_prev)

                    # Check no left row matches this right row
                    right_match_conds = []
                    for _, parent_binding in partials:
                        check_binding = dict(parent_binding)
                        check_binding[right_alias] = sym_right.rows[row_j]
                        if right_alias != right_table_name:
                            check_binding[right_table_name] = sym_right.rows[row_j]
                        if join.on is not None:
                            right_match_conds.append(_eval_predicate(join.on, check_binding))
                    no_left_match = z3.Not(z3.Or(right_match_conds)) if right_match_conds else z3.BoolVal(True)
                    new_partials.append((no_left_match, right_binding))

        partials = new_partials

    return partials
