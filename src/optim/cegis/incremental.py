"""Incremental solving infrastructure for multi-candidate verification.

Encodes the original query once and shares the base constraints across
all candidate verifications via Z3 push/pop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from ..ir.types import QueryIR
from ..schema.catalog import Catalog
from ..verify.encode_z3 import BoundedScope
from .witness_synthesis import WitnessResult, synthesize_witness

logger = logging.getLogger(__name__)


@dataclass
class IncrementalVerifier:
    """Verify multiple candidates against a shared original query.
    
    Currently wraps synthesize_witness() calls — when Z3 push/pop
    support is added to witness_synthesis, this will encode the original
    once and share base constraints.
    
    Usage:
        verifier = IncrementalVerifier(original_ir, catalog, scope)
        for cand in candidates:
            result = verifier.verify(cand.ir)
    """
    original_ir: QueryIR
    catalog: Catalog
    scope: BoundedScope
    _results_cache: dict[str, WitnessResult] = field(default_factory=dict)
    total_encode_ms: float = 0.0
    total_solver_ms: float = 0.0
    
    def verify(self, candidate_ir: QueryIR) -> WitnessResult:
        """Verify a candidate against the original.
        
        Currently delegates to synthesize_witness(). Future: share
        encoded original via push/pop.
        """
        result = synthesize_witness(
            self.original_ir, candidate_ir, self.catalog, self.scope,
        )
        self.total_solver_ms += result.solver_time_ms
        return result
    
    @property
    def n_verified(self) -> int:
        return len(self._results_cache)
