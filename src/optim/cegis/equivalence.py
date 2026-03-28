"""Equivalence clustering via pairwise witness synthesis.

Given a set of verified candidate IRs, build an equivalence graph where
an edge (i, j) means "no witness exists under scope Σ" (UNSAT ⇒ equivalent).
Connected components are equivalence classes.

Optimizations:
  - Hash-based dedup before SMT (normalized IR identity)
  - Pairwise cache across loop iterations
  - Only compare representatives after early merges
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

import networkx as nx

from ..ir.normalization import normalize
from ..ir.types import QueryIR
from ..schema.catalog import Catalog
from ..verify.encode_z3 import BoundedScope
from .witness_export import ValidationResult, validate_witness
from .witness_synthesis import WitnessResult, batch_witness_synthesis, synthesize_witness


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """A candidate query IR with metadata."""
    id: str
    ir: QueryIR
    confidence: float = 0.5
    source: str = "unknown"  # e.g., "llm", "enumerator", "user"
    metadata: dict = field(default_factory=dict)


@dataclass
class PairwiseCheck:
    """Result of pairwise witness synthesis between two candidates."""
    id_a: str
    id_b: str
    status: str  # "sat", "unsat", "unknown", "timeout"
    witness_db: Optional[dict[str, list[dict[str, object]]]] = None
    validation: Optional[ValidationResult] = None
    solver_time_ms: float = 0.0


@dataclass
class EquivalenceClustering:
    """Result of clustering candidates into equivalence classes."""
    classes: list[list[str]]  # list of equivalence classes (lists of candidate ids)
    representatives: dict[int, str]  # class_index → representative candidate id
    pair_results: dict[tuple[str, str], PairwiseCheck] = field(default_factory=dict)
    scope: Optional[BoundedScope] = None
    uncertain_ids: set[str] = field(default_factory=set)

    @property
    def n_classes(self) -> int:
        return len(self.classes)

    def get_class_for(self, candidate_id: str) -> Optional[int]:
        """Return the class index for a candidate id."""
        for i, cls in enumerate(self.classes):
            if candidate_id in cls:
                return i
        return None

    def get_distinguisher(self, id_a: str, id_b: str) -> Optional[PairwiseCheck]:
        """Get the SAT witness between two candidates (if any)."""
        key = _pair_key(id_a, id_b)
        result = self.pair_results.get(key)
        if result and result.status == "sat":
            return result
        return None


def _pair_key(a: str, b: str) -> tuple[str, str]:
    """Canonical key for a pair (sorted)."""
    return (min(a, b), max(a, b))


def _ir_hash(ir: QueryIR) -> str:
    """Compute a stable hash of a normalized IR for dedup."""
    normed = normalize(ir)
    ir_str = normed.model_dump_json()
    return hashlib.sha256(ir_str.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Core clustering
# ---------------------------------------------------------------------------

def cluster_candidates(
    candidates: list[Candidate],
    catalog: Catalog,
    scope: Optional[BoundedScope] = None,
    *,
    validate: bool = True,
    cache: Optional[dict[tuple[str, str], PairwiseCheck]] = None,
    protected_ids: Optional[set[str]] = None,
) -> EquivalenceClustering:
    """Cluster candidates into equivalence classes under bounded scope Σ.

    Algorithm:
      1. Hash-based dedup: candidates with identical normalized IR are merged.
      2. For remaining pairs, run witness synthesis:
         - UNSAT → add equivalence edge
         - SAT → store distinguishing witness
      3. Connected components of the equivalence graph = equivalence classes.

    Args:
        candidates: List of candidate IRs to cluster.
        catalog: Schema catalog.
        scope: Bounded semantics scope.
        validate: Whether to validate SAT witnesses in sqlite3.
        cache: Optional cache of prior pairwise results.

    Returns:
        EquivalenceClustering with classes, representatives, and pair results.
    """
    if scope is None:
        scope = BoundedScope(k_rows=2)

    # Use a tighter timeout for clustering (most SAT/UNSAT resolve in <1s;
    # full 30s timeout is only needed for final clarification witnesses)
    clustering_timeout_ms = min(scope.solver_timeout_ms, 10_000)
    cluster_scope = BoundedScope(
        k_rows=scope.k_rows,
        int_bounds=scope.int_bounds,
        string_symbols=scope.string_symbols,
        date_values=scope.date_values,
        null_semantics=scope.null_semantics,
        solver_timeout_ms=clustering_timeout_ms,
    )

    if cache is None:
        cache = {}

    if not candidates:
        return EquivalenceClustering(classes=[], representatives={}, scope=scope)

    if len(candidates) == 1:
        return EquivalenceClustering(
            classes=[[candidates[0].id]],
            representatives={0: candidates[0].id},
            scope=scope,
        )

    # Step 1: Hash-based dedup
    hash_groups: dict[str, list[str]] = {}
    id_to_candidate: dict[str, Candidate] = {}
    for c in candidates:
        id_to_candidate[c.id] = c
        h = _ir_hash(c.ir)
        hash_groups.setdefault(h, []).append(c.id)

    # Build equivalence graph
    graph = nx.Graph()
    for c in candidates:
        graph.add_node(c.id)

    # Add edges for hash-identical candidates (free equivalence)
    for group_ids in hash_groups.values():
        for i in range(len(group_ids)):
            for j in range(i + 1, len(group_ids)):
                graph.add_edge(group_ids[i], group_ids[j])

    # Step 2: Pick unique representatives per hash group for SMT comparison
    unique_reps: list[str] = []
    for group_ids in hash_groups.values():
        unique_reps.append(group_ids[0])

    logger.debug("Hash dedup: %d candidates → %d unique", len(candidates), len(unique_reps))

    # Step 3: Pairwise witness synthesis between unique reps
    # Use union-find for transitive skip: if A≡B and B≡C, skip (A,C)
    pair_results: dict[tuple[str, str], PairwiseCheck] = dict(cache)
    uf: dict[str, str] = {uid: uid for uid in unique_reps}

    def _uf_find(x: str) -> str:
        while uf[x] != x:
            uf[x] = uf[uf[x]]  # path compression
            x = uf[x]
        return x

    def _uf_union(x: str, y: str) -> None:
        rx, ry = _uf_find(x), _uf_find(y)
        if rx != ry:
            uf[rx] = ry

    # Collect uncached pairs (respecting transitive skip via union-find)
    skipped = 0
    uncached_pairs: list[tuple[str, str]] = []
    cached_order: list[tuple[str, str]] = []
    for i in range(len(unique_reps)):
        for j in range(i + 1, len(unique_reps)):
            id_a, id_b = unique_reps[i], unique_reps[j]

            # Transitive skip: already in the same equivalence class
            if _uf_find(id_a) == _uf_find(id_b):
                skipped += 1
                continue

            key = _pair_key(id_a, id_b)

            if key in pair_results:
                cached_order.append((id_a, id_b))
            else:
                uncached_pairs.append((id_a, id_b))

    # Process cached pairs first (apply union-find merges)
    for id_a, id_b in cached_order:
        key = _pair_key(id_a, id_b)
        result = pair_results[key]
        logger.debug("Pair (%s, %s): %s (%.1fms) [cached]", id_a, id_b, result.status, result.solver_time_ms)
        if result.status == "unsat":
            graph.add_edge(id_a, id_b)
            _uf_union(id_a, id_b)

    # Use batch synthesis when ≥3 uncached pairs exist
    if len(uncached_pairs) >= 3:
        # Collect all candidate IRs referenced by uncached pairs
        batch_ids: set[str] = set()
        for id_a, id_b in uncached_pairs:
            batch_ids.add(id_a)
            batch_ids.add(id_b)
        batch_candidates = {cid: id_to_candidate[cid].ir for cid in batch_ids}

        # Filter pairs by transitive skip (union-find may have merged via cached results)
        batch_pairs: list[tuple[str, str]] = []
        for id_a, id_b in uncached_pairs:
            if _uf_find(id_a) != _uf_find(id_b):
                batch_pairs.append((id_a, id_b))
            else:
                skipped += 1

        batch_results = batch_witness_synthesis(
            batch_candidates, batch_pairs, catalog, cluster_scope, minimize=False,
        )

        # Process batch results + validate + apply union-find
        for id_a, id_b in batch_pairs:
            witness_result = batch_results.get((id_a, id_b))
            if witness_result is None:
                # Shouldn't happen, but fall back
                ca, cb = id_to_candidate[id_a], id_to_candidate[id_b]
                witness_result = synthesize_witness(
                    ca.ir, cb.ir, catalog, cluster_scope, minimize=False,
                )

            validation = None
            if validate and witness_result.status == "sat" and witness_result.witness_db is not None:
                validation = validate_witness(
                    id_to_candidate[id_a].ir, id_to_candidate[id_b].ir,
                    witness_result.witness_db, catalog,
                )
                if not validation.results_differ:
                    witness_result = WitnessResult(
                        status="unknown",
                        solver_time_ms=witness_result.solver_time_ms,
                    )

            key = _pair_key(id_a, id_b)
            result = PairwiseCheck(
                id_a=key[0],
                id_b=key[1],
                status=witness_result.status,
                witness_db=witness_result.witness_db if witness_result.status == "sat" else None,
                validation=validation,
                solver_time_ms=witness_result.solver_time_ms,
            )
            pair_results[key] = result

            logger.debug("Pair (%s, %s): %s (%.1fms) [batch]", id_a, id_b, result.status, result.solver_time_ms)

            if result.status == "unsat":
                graph.add_edge(id_a, id_b)
                _uf_union(id_a, id_b)
    else:
        # Fall back to individual synthesis for <3 uncached pairs
        for id_a, id_b in uncached_pairs:
            if _uf_find(id_a) == _uf_find(id_b):
                skipped += 1
                continue

            key = _pair_key(id_a, id_b)
            ca, cb = id_to_candidate[id_a], id_to_candidate[id_b]
            try:
                witness_result = synthesize_witness(
                    ca.ir, cb.ir, catalog, cluster_scope, minimize=False,
                )
            except Exception as exc:
                logger.warning("synthesize_witness OOM/crash for (%s, %s): %s", id_a, id_b, exc)
                witness_result = WitnessResult(status="unknown", solver_time_ms=0.0)

            validation = None
            if validate and witness_result.status == "sat" and witness_result.witness_db is not None:
                validation = validate_witness(ca.ir, cb.ir, witness_result.witness_db, catalog)
                if not validation.results_differ:
                    witness_result = WitnessResult(
                        status="unknown",
                        solver_time_ms=witness_result.solver_time_ms,
                    )

            result = PairwiseCheck(
                id_a=key[0],
                id_b=key[1],
                status=witness_result.status,
                witness_db=witness_result.witness_db if witness_result.status == "sat" else None,
                validation=validation,
                solver_time_ms=witness_result.solver_time_ms,
            )
            pair_results[key] = result

            logger.debug("Pair (%s, %s): %s (%.1fms)", id_a, id_b, result.status, result.solver_time_ms)

            if result.status == "unsat":
                graph.add_edge(id_a, id_b)
                _uf_union(id_a, id_b)

    # Update caller's cache with new results (only definitive SAT/UNSAT;
    # timeout/unknown may resolve differently with more budget later)
    cache.update({k: v for k, v in pair_results.items() if v.status in ("sat", "unsat")})

    if skipped:
        logger.debug("Union-find transitive skip: %d pairs skipped", skipped)

    # Timeout-prone candidate quarantine: if a candidate times out on >50%
    # of its pairwise checks, it's too complex for Z3.  Quarantine it so it
    # is not preferred as representative, but keep it in the graph so UNSAT
    # edges still merge its class (no fragmentation).
    _protected = protected_ids or set()
    timeout_counts: dict[str, int] = {}
    total_checks: dict[str, int] = {}
    for (id_a, id_b), pr in pair_results.items():
        total_checks[id_a] = total_checks.get(id_a, 0) + 1
        total_checks[id_b] = total_checks.get(id_b, 0) + 1
        if pr.status in ("timeout", "unknown"):
            timeout_counts[id_a] = timeout_counts.get(id_a, 0) + 1
            timeout_counts[id_b] = timeout_counts.get(id_b, 0) + 1

    quarantined_ids: set[str] = set()
    for cid, t_count in timeout_counts.items():
        n_checks = total_checks.get(cid, 1)
        if t_count > n_checks * 0.5 and cid not in _protected:
            quarantined_ids.add(cid)
            logger.info("Timeout pruning: quarantining %s (%d/%d timeouts)",
                        cid, t_count, n_checks)

    # Step 4: Connected components
    components = list(nx.connected_components(graph))
    classes = [sorted(list(comp)) for comp in components]

    # Pick representatives: prefer non-quarantined candidates
    representatives: dict[int, str] = {}
    for idx, cls in enumerate(classes):
        non_uncertain = [cid for cid in cls if cid not in quarantined_ids]
        pool = non_uncertain if non_uncertain else cls
        best = max(pool, key=lambda cid: id_to_candidate[cid].confidence)
        representatives[idx] = best

    logger.debug("Clustering result: %d classes %s", len(classes), classes)

    return EquivalenceClustering(
        classes=classes,
        representatives=representatives,
        pair_results=pair_results,
        scope=scope,
        uncertain_ids=quarantined_ids,
    )
