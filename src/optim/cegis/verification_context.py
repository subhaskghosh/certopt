"""Verification context: encapsulates mutable state for synthesis.

Refactors module-level mutable state into a composable context object
that enables parallel verification, per-candidate memoization, and
deterministic replay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..ir.types import QueryIR
from ..schema.catalog import Catalog
from ..verify.encode_z3 import BoundedScope


@dataclass
class VerificationContext:
    """Encapsulates all state needed for a verification run.
    
    Currently a data container — witness_synthesis.py still uses
    module-level state. This class provides the target API for
    future refactoring.
    """
    catalog: Catalog
    scope: BoundedScope
    original_ir: Optional[QueryIR] = None
    
    # Memoization
    expr_cache_hits: int = 0
    expr_cache_misses: int = 0
    
    # Timing
    total_encode_ms: float = 0.0
    total_solver_ms: float = 0.0
    
    # Statistics
    n_candidates_verified: int = 0
    n_unsat: int = 0
    n_sat: int = 0
    n_unknown: int = 0
    
    def record_result(self, status: str) -> None:
        self.n_candidates_verified += 1
        if status == "unsat":
            self.n_unsat += 1
        elif status == "sat":
            self.n_sat += 1
        else:
            self.n_unknown += 1
    
    def summary(self) -> dict:
        return {
            "n_verified": self.n_candidates_verified,
            "n_unsat": self.n_unsat,
            "n_sat": self.n_sat,
            "n_unknown": self.n_unknown,
            "total_encode_ms": self.total_encode_ms,
            "total_solver_ms": self.total_solver_ms,
            "cache_hit_rate": (
                self.expr_cache_hits / max(1, self.expr_cache_hits + self.expr_cache_misses)
            ),
        }
