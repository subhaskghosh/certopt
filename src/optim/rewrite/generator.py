"""Rewrite generator: apply rules to produce candidate rewrites.

Applies each enabled rule to the input QueryIR, tags results with
source=rule_id, deduplicates via shape signatures, and returns up to
max_total_candidates candidates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..cegis.equivalence import Candidate
from ..ir.types import QueryIR
from ..schema.catalog import Catalog
from .rules import RULE_REGISTRY, _shape_signature, dedup_candidates

logger = logging.getLogger(__name__)


@dataclass
class RewriteConfig:
    """Configuration for the rewrite generator."""
    enabled_rules: list[str] = field(default_factory=lambda: [
        "R1", "R2", "R3", "R4", "R5", "R6",
    ])
    max_candidates_per_rule: int = 5
    max_total_candidates: int = 20


class RewriteGenerator:
    """Generate candidate rewrites from a QueryIR by applying algebraic rules."""

    def __init__(self, catalog: Catalog, config: RewriteConfig | None = None):
        self.catalog = catalog
        self.config = config or RewriteConfig()

    def generate(self, ir: QueryIR) -> list[Candidate]:
        """Apply all enabled rules and return deduplicated candidates."""
        all_candidates: list[Candidate] = []
        seen_shapes: set[str] = set()

        # Add original IR's shape to seen set to avoid generating it as a candidate
        orig_shape = _shape_signature(ir)
        seen_shapes.add(orig_shape)

        for rule_id in self.config.enabled_rules:
            rule_fn = RULE_REGISTRY.get(rule_id)
            if rule_fn is None:
                logger.warning("Unknown rule: %s", rule_id)
                continue

            try:
                rewrites = rule_fn(ir, self.catalog)
            except Exception:
                logger.exception("Rule %s failed on input IR", rule_id)
                continue

            added = 0
            for i, rewritten_ir in enumerate(rewrites):
                if added >= self.config.max_candidates_per_rule:
                    break
                if len(all_candidates) >= self.config.max_total_candidates:
                    break

                sig = _shape_signature(rewritten_ir)
                if sig in seen_shapes:
                    continue
                seen_shapes.add(sig)

                candidate = Candidate(
                    id=f"{rule_id}_{i}",
                    ir=rewritten_ir,
                    confidence=0.8,
                    source=rule_id,
                )
                all_candidates.append(candidate)
                added += 1

            if added > 0:
                logger.info("Rule %s: generated %d candidates", rule_id, added)

        # Final dedup pass
        all_candidates = dedup_candidates(all_candidates)

        # Trim to max total
        if len(all_candidates) > self.config.max_total_candidates:
            all_candidates = all_candidates[:self.config.max_total_candidates]

        logger.info("RewriteGenerator: %d total candidates", len(all_candidates))
        return all_candidates
