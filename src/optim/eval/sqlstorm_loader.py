"""SQLStorm rewrite-pair loader for equivalence verification.

Loads (rewritten, compatible) pairs from SQLStorm's ``queries_generated/``
directory.  Each pair represents an LLM dialect-fix rewrite that should
be semantically equivalent to the original.

Entry point: ``load_sqlstorm_pairs(dataset, max_pairs)``
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data root
# ---------------------------------------------------------------------------

DATA_ROOT = Path("data/SQLStorm/v1.0")

# ---------------------------------------------------------------------------
# Pre-filter patterns (unsupported constructs our parser cannot handle)
# ---------------------------------------------------------------------------

_PREFILTER_PATTERNS: dict[str, re.Pattern[str]] = {
    "STRING_AGG": re.compile(r"\bSTRING_AGG\b", re.IGNORECASE),
    "ARRAY_AGG": re.compile(r"\bARRAY_AGG\b", re.IGNORECASE),
    "UNNEST": re.compile(r"\bUNNEST\b", re.IGNORECASE),
    "LATERAL": re.compile(r"\bLATERAL\b", re.IGNORECASE),
    "WITH_RECURSIVE": re.compile(r"\bWITH\s+RECURSIVE\b", re.IGNORECASE),
    "FETCH_FIRST": re.compile(r"\bFETCH\s+(FIRST|NEXT)\b", re.IGNORECASE),
}


def _prefilter(sql: str) -> str | None:
    """Return the name of the first unsupported construct found, or ``None``."""
    for name, pat in _PREFILTER_PATTERNS.items():
        if pat.search(sql):
            return name
    return None


# ---------------------------------------------------------------------------
# Pair loader
# ---------------------------------------------------------------------------

def load_sqlstorm_pairs(
    dataset: str = "stackoverflow",
    max_pairs: int | None = None,
) -> list[dict]:
    """Load (rewritten, compatible) SQL pairs from SQLStorm.

    Scans ``data/SQLStorm/v1.0/<dataset>/queries_generated/`` for query IDs
    that have both ``<id>.sql_rewritten`` and ``<id>.sql_compatible`` files,
    then returns the pair for equivalence verification.

    Parameters
    ----------
    dataset:
        SQLStorm dataset name (e.g. ``"stackoverflow"``, ``"tpcds"``,
        ``"tpch"``, ``"job"``).
    max_pairs:
        Optional upper bound on the number of pairs returned.

    Returns
    -------
    list[dict]
        Each dict has keys ``pair_id``, ``sql1`` (rewritten), ``sql2``
        (compatible), and ``dataset``.
    """
    query_dir = DATA_ROOT / dataset / "queries_generated"
    if not query_dir.is_dir():
        logger.warning("SQLStorm query directory not found: %s", query_dir)
        return []

    # Discover query IDs that have both _rewritten and _compatible files
    rewritten_files: dict[str, Path] = {}
    compatible_files: dict[str, Path] = {}

    for p in query_dir.iterdir():
        if not p.is_file():
            continue
        name = p.name
        if name.endswith(".sql_rewritten"):
            qid = name.removesuffix(".sql_rewritten")
            rewritten_files[qid] = p
        elif name.endswith(".sql_compatible"):
            qid = name.removesuffix(".sql_compatible")
            compatible_files[qid] = p

    common_ids = sorted(
        rewritten_files.keys() & compatible_files.keys(),
        key=lambda x: int(x) if x.isdigit() else x,
    )
    logger.info(
        "SQLStorm %s: %d rewritten, %d compatible, %d common IDs",
        dataset,
        len(rewritten_files),
        len(compatible_files),
        len(common_ids),
    )

    # Build pairs with pre-filtering
    pairs: list[dict] = []
    skipped = 0

    for qid in common_ids:
        sql1 = rewritten_files[qid].read_text(encoding="utf-8").strip()
        sql2 = compatible_files[qid].read_text(encoding="utf-8").strip()

        # Skip pairs where either query contains unsupported constructs
        hit1 = _prefilter(sql1)
        hit2 = _prefilter(sql2)
        if hit1 is not None or hit2 is not None:
            reason = hit1 or hit2
            logger.debug("Skipping query %s (%s): unsupported %s", qid, dataset, reason)
            skipped += 1
            continue

        pairs.append({
            "pair_id": qid,
            "sql1": sql1,
            "sql2": sql2,
            "dataset": dataset,
        })

        if max_pairs is not None and len(pairs) >= max_pairs:
            break

    logger.info(
        "SQLStorm %s: loaded %d pairs (%d skipped by pre-filter)",
        dataset,
        len(pairs),
        skipped,
    )
    return pairs
