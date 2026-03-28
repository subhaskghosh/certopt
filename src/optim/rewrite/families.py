"""Rewrite family classification for counterexample-guided pruning.

When a witness invalidates one rewrite, prune the entire family of
rewrites sharing the same structural transformation.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from ..cegis.equivalence import Candidate

logger = logging.getLogger(__name__)


@dataclass
class RewriteFamily:
    """A family of rewrites sharing a structural transformation."""
    rule_id: str
    structural_key: str
    members: list[str] = field(default_factory=list)


def classify_rewrites(candidates: list[Candidate]) -> list[RewriteFamily]:
    """Group candidates into families by rule_id + structural key.

    The structural key captures the specific transformation applied:
    - R1: "pred_push:{table}" for which table the predicate was pushed to
    - R3: "join_elim:{table}" for which table was eliminated
    - R5: "join_reorder:{order_hash}" for the specific join order
    - Others: just the rule_id as the key
    """
    groups: dict[str, list[str]] = defaultdict(list)

    for c in candidates:
        rule_id = c.source or "unknown"

        # Extract structural key from candidate ID
        # IDs look like "R1_0", "R3_0", "R5_2", etc.
        structural_key = f"{rule_id}:{c.id}"

        groups[structural_key].append(c.id)

    families = [
        RewriteFamily(
            rule_id=key.split(":")[0],
            structural_key=key,
            members=members,
        )
        for key, members in groups.items()
    ]

    logger.info("Classified %d candidates into %d families",
                sum(len(f.members) for f in families), len(families))
    return families


def prune_families(
    families: list[RewriteFamily],
    rejected_ids: set[str],
) -> set[str]:
    """Given rejected candidate IDs, return all IDs in the same families to prune.

    If any member of a family is rejected by a witness, all members of
    that family are pruned (they share the same structural defect).
    """
    pruned: set[str] = set()

    for family in families:
        # Check if any member was explicitly rejected
        if rejected_ids & set(family.members):
            pruned.update(family.members)
            logger.info("Family pruning: %s — pruning %d members",
                        family.structural_key, len(family.members))

    # Don't double-count already-rejected ones
    newly_pruned = pruned - rejected_ids
    if newly_pruned:
        logger.info("Family pruning saved %d solver calls", len(newly_pruned))

    return pruned
