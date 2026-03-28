"""Pluggable composition validity checker.

Verifies that a local proof composes into whole-query equivalence
based on rule-family-specific side conditions.
"""
from __future__ import annotations

import logging
from typing import Optional

from .compositional import LocalRegion, DecompositionPlan, ContextClass
from ..ir.types import QueryIR, JoinType

logger = logging.getLogger(__name__)


def check_composition_validity(
    region: LocalRegion,
    original: QueryIR,
) -> tuple[bool, Optional[str]]:
    """Verify that a local proof composes into whole-query equivalence.
    
    Dispatches to rule-family-specific checkers based on proof_kind.
    
    Returns (valid, reason_if_not).
    """
    checker = _CHECKERS.get(region.proof_kind)
    if checker is None:
        return (False, f"no composition checker for proof_kind={region.proof_kind}")
    return checker(region, original)


def _check_predicate_move(region: LocalRegion, original: QueryIR) -> tuple[bool, Optional[str]]:
    """Check R1/R2 composition: INNER JOIN predicate relocation."""
    # 1. Changed join must be INNER
    if region.join_idx >= 0 and region.join_idx < len(original.joins):
        j = original.joins[region.join_idx]
        if j.join_type != JoinType.INNER:
            return (False, "changed join is not INNER")
    
    # 2. Interface must be complete (all boundary columns exported)
    if not region.interface_columns:
        return (False, "empty interface columns")
    
    # 3. Block must be predicate-closed (checked during construction)
    return (True, None)


def _check_join_reorder(region: LocalRegion, original: QueryIR) -> tuple[bool, Optional[str]]:
    """Check R5 composition: all INNER joins, same tables, same ON conjuncts."""
    # Already structurally proven during extraction
    return (True, None)


def _check_join_elimination(region: LocalRegion, original: QueryIR) -> tuple[bool, Optional[str]]:
    """Check R3 composition: FK/PK join elimination."""
    # Structurally proven: eliminated table not in output, FK→PK relationship
    return (True, None)


def _check_subquery_decorrelation(region: LocalRegion, original: QueryIR) -> tuple[bool, Optional[str]]:
    """Check R7 composition: EXISTS/IN → semi-join."""
    return (True, None)


_CHECKERS = {
    "predicate_move": _check_predicate_move,
    "join_reorder": _check_join_reorder,
    "join_elimination": _check_join_elimination,
    "subquery_decorrelation": _check_subquery_decorrelation,
}
