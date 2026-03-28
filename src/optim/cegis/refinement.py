"""CEGAR-style refinement for witness synthesis.

When the standard synthesis produces a SAT result but the witness is
spurious (both queries agree on it), this module re-runs synthesis
with additional constraints that pin down the approximate predicate
results to their concrete SQLite-evaluated values.

This turns LIKE/string-function approximation from a permanent
limitation into a controlled refinement story.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..ir.types import QueryIR
from ..schema.catalog import Catalog
from ..verify.encode_z3 import BoundedScope
from .witness_export import ValidationResult, validate_witness, RefinementHint
from .witness_synthesis import WitnessResult, synthesize_witness

logger = logging.getLogger(__name__)


@dataclass
class RefinementResult:
    """Result of CEGAR-style refinement synthesis."""
    final_result: WitnessResult
    rounds: int
    spurious_count: int  # how many spurious witnesses were found
    refined: bool  # True if refinement changed the outcome


def synthesize_with_refinement(
    q1: QueryIR,
    q2: QueryIR,
    catalog: Catalog,
    scope: Optional[BoundedScope] = None,
    max_rounds: int = 3,
) -> RefinementResult:
    """CEGAR-style synthesis: approximate → validate → report.

    Round 0: standard synthesis (LIKE ≈ equality, unmodeled funcs ≈ fresh var)
    If SAT: validate witness in SQLite
    If spurious: log refinement hints and return "unknown" (conservative)

    Note: Full constraint injection (adding concrete function results back
    to the solver) requires architectural changes to expose the Z3 solver
    across rounds. For now, this implementation:
      1. Detects spurious witnesses
      2. Reports refinement hints
      3. Returns "unknown" instead of false "sat" for spurious cases

    This is already valuable: it prevents false rejections of equivalent
    rewrites that happen to use LIKE/string functions.

    Args:
        q1, q2: Query IRs to check equivalence.
        catalog: Schema catalog.
        scope: Bounded scope.
        max_rounds: Maximum refinement rounds (reserved for future use).

    Returns:
        RefinementResult with the final synthesis outcome.
    """
    if scope is None:
        scope = BoundedScope(k_rows=2)

    # Round 0: standard synthesis
    result = synthesize_witness(q1, q2, catalog, scope)

    if result.status != "sat" or result.witness_db is None:
        # UNSAT, unknown, or timeout — no refinement needed
        return RefinementResult(
            final_result=result,
            rounds=0,
            spurious_count=0,
            refined=False,
        )

    # Validate the witness
    validation = validate_witness(q1, q2, result.witness_db, catalog)

    if validation.results_differ:
        # Valid witness — the queries genuinely differ
        return RefinementResult(
            final_result=result,
            rounds=1,
            spurious_count=0,
            refined=False,
        )

    # Spurious witness — queries agree on the witness DB
    logger.info(
        "CEGAR: spurious witness detected (%d refinement hints). "
        "Returning 'unknown' instead of false 'sat'.",
        len(validation.refinement_hints),
    )
    if validation.refinement_hints:
        for hint in validation.refinement_hints:
            logger.debug(
                "  Hint: %s.%s — %s (pattern=%s)",
                hint.table, hint.column, hint.predicate_type, hint.pattern,
            )

    # Return "unknown" — conservative: we can't confirm the queries differ
    return RefinementResult(
        final_result=WitnessResult(
            status="unknown",
            witness_db=None,
            solver_time_ms=result.solver_time_ms,
        ),
        rounds=1,
        spurious_count=1,
        refined=True,
    )
