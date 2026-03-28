"""Result types for the CEGIS optimization loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..cegis.equivalence import Candidate
from ..cost.estimator import CostEstimate
from ..ir.types import QueryIR
from ..verify.certificate import Certificate


@dataclass
class RejectedRewrite:
    """A rewrite candidate rejected by the verifier."""
    candidate: Candidate
    witness_db: Optional[dict[str, list[dict[str, object]]]] = None
    family: Optional[str] = None
    reason: str = "non_equivalent"  # "non_equivalent", "structural", "family_pruned"


@dataclass
class OptimizationResult:
    """Complete result of the CEGIS optimization loop."""
    original_sql: str
    original_ir: QueryIR
    optimized_sql: str
    optimized_ir: QueryIR
    certificate: Optional[Certificate]
    cost_original: CostEstimate
    cost_optimized: CostEstimate
    speedup: float
    all_verified: list[tuple[Candidate, CostEstimate]] = field(default_factory=list)
    rejected: list[RejectedRewrite] = field(default_factory=list)
    total_candidates: int = 0
    solver_time_ms: float = 0.0
    total_time_ms: float = 0.0

    @property
    def improved(self) -> bool:
        """Whether optimization found a cheaper equivalent rewrite."""
        return self.speedup > 1.0

    @property
    def n_verified(self) -> int:
        return len(self.all_verified)

    @property
    def n_rejected(self) -> int:
        return len(self.rejected)


class CandidateOutcome:
    """Stub for removed API."""
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EQUIVALENT = "equivalent"
    TIMEOUT = "timeout"
    ERROR = "error"

    def __init__(self, status="", reason="", candidate_id="", source="",
                 category="", repair_applied=False, repair_details=None,
                 witness_db=None, cost=None):
        self.status = status
        self.reason = reason
        self.candidate_id = candidate_id
        self.source = source
        self.category = category
        self.repair_applied = repair_applied
        self.repair_details = repair_details
        self.witness_db = witness_db
        self.cost = cost
