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


def _structural_signature(candidate: Candidate) -> str:
    """Compute the structural signature for family grouping.

    Candidates are grouped by (rule_id, structural_shape) so that
    a witness invalidating one member prunes the entire family.

    - R1 (predicate pushdown): group by rule + target join table
    - R2 (predicate pullup): group by rule + source join table
    - R3 (join elimination): group by rule + eliminated table
    - R5 (join reorder): all reorderings share one family
    - LLM candidates: all share one family
    - Others: group by rule_id alone
    """
    rule_id = candidate.source or "unknown"
    ir = candidate.ir

    if rule_id == "R1" and ir.joins:
        # Predicate pushdown: the structural defect is tied to which
        # table the predicate was pushed to.  Approximate by the set
        # of join tables whose ON clause was modified.
        join_tables = frozenset(
            j.right.name.lower() if hasattr(j.right, 'name') else str(j.right)
            for j in ir.joins if j.on is not None
        )
        return f"R1:{','.join(sorted(join_tables))}"

    if rule_id == "R2" and ir.joins:
        join_tables = frozenset(
            j.right.name.lower() if hasattr(j.right, 'name') else str(j.right)
            for j in ir.joins if j.on is not None
        )
        return f"R2:{','.join(sorted(join_tables))}"

    if rule_id == "R3":
        # Join elimination: group by which table was eliminated.
        # The eliminated table is absent from joins in the rewrite,
        # but we don't have the original IR here.  Group all R3
        # candidates together since they share the structural
        # pattern of FK→PK elimination.
        return "R3"

    if rule_id == "R5":
        # Join reorder: all permutations share the same structural
        # defect (wrong join order), so group together.
        return "R5"

    if rule_id == "llm":
        # All LLM candidates share a family.
        return "llm"

    # Default: group by rule_id
    return rule_id


def classify_rewrites(candidates: list[Candidate]) -> list[RewriteFamily]:
    """Group candidates into families by rule_id + structural signature.

    The structural signature captures the syntactic shape of the
    transformation.  When a witness invalidates one member of a
    family, all members sharing the same structural defect are pruned
    without individual solver calls.
    """
    groups: dict[str, list[str]] = defaultdict(list)

    for c in candidates:
        key = _structural_signature(c)
        groups[key].append(c.id)

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
