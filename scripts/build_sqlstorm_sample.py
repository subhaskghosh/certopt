"""Build stratified evaluation samples from SQLStorm query pairs.

Supports two comparison modes:

  orig_vs_rewritten  — raw LLM output (.sql) vs PF-fixed version (.sql_rewritten)
                       Filters for structurally non-trivial edits only.

  rewritten_vs_compat — PF-fixed (.sql_rewritten) vs dialect-converted (.sql_compatible)
                        (Legacy mode, kept for completeness.)

Usage:
    # Default: original vs rewritten, structural diffs only
    python3 -m scripts.build_sqlstorm_sample

    # Customise
    python3 -m scripts.build_sqlstorm_sample --sample-size 300
    python3 -m scripts.build_sqlstorm_sample --datasets tpcds job --sample-size 400
    python3 -m scripts.build_sqlstorm_sample --mode rewritten_vs_compat
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATASETS = ["tpch", "tpcds", "stackoverflow", "job"]
DATA_ROOT = Path("data/SQLStorm/v1.0")
OUTPUT_DIR = Path("scripts/sqlstorm_sample")

MAX_QUERY_ID = 34999

PROMPT_TIERS: list[tuple[str, int, int]] = [
    ("P1", 0, 4999),
    ("P2", 5000, 9999),
    ("P3", 10000, 14999),
    ("P4", 15000, 19999),
    ("P5", 20000, 24999),
    ("P6", 25000, 29999),
    ("P7", 30000, 34999),
]

# ---------------------------------------------------------------------------
# Pre-filter patterns (unsupported constructs)
# ---------------------------------------------------------------------------

_PREFILTER_PATTERNS: dict[str, re.Pattern[str]] = {
    "WITH_RECURSIVE": re.compile(r"\bWITH\s+RECURSIVE\b", re.IGNORECASE),
    "STRING_AGG": re.compile(r"\bSTRING_AGG\b", re.IGNORECASE),
    "ARRAY_AGG": re.compile(r"\bARRAY_AGG\b", re.IGNORECASE),
    "UNNEST": re.compile(r"\bUNNEST\b", re.IGNORECASE),
    "LATERAL": re.compile(r"\bLATERAL\b", re.IGNORECASE),
    "FETCH_FIRST": re.compile(r"\bFETCH\s+(FIRST|NEXT)\b", re.IGNORECASE),
    "GROUP_CONCAT": re.compile(r"\bGROUP_CONCAT\b", re.IGNORECASE),
    "XMLAGG": re.compile(r"\bXMLAGG\b", re.IGNORECASE),
    "LISTAGG": re.compile(r"\bLISTAGG\b", re.IGNORECASE),
}


def _prefilter(sql: str) -> str | None:
    """Return the name of the first unsupported construct found, or ``None``."""
    for name, pat in _PREFILTER_PATTERNS.items():
        if pat.search(sql):
            return name
    return None


# ---------------------------------------------------------------------------
# Triviality detection for original→rewritten diffs
# ---------------------------------------------------------------------------

_COMMENT_PAT = re.compile(r"--[^\n]*")
_DATE_LITERAL_PAT = re.compile(r"'[0-9]{4}-[0-9]{2}-[0-9]{2}[^']*'")
_CURRENT_FN_PAT = re.compile(
    r"\b(getdate\(\)|current_date|current_timestamp|current_time|now\(\))\b",
    re.IGNORECASE,
)
_CAST_SHORT_PAT = re.compile(r"::\w+(\([^)]*\))?")


def _normalize_for_diff(sql: str) -> str:
    """Aggressively normalize SQL to detect structurally non-trivial edits.

    Strips comments, collapses whitespace, replaces date literals and
    current_date/now() with placeholders, removes :: cast syntax.
    Two queries that are identical after this normalization differ only
    in trivial ways (comments, dates, cast syntax).
    """
    s = _COMMENT_PAT.sub("", sql)
    s = _DATE_LITERAL_PAT.sub("'__DATE__'", s)
    s = _CURRENT_FN_PAT.sub("__CURRENT__", s)
    s = _CAST_SHORT_PAT.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _classify_edit(sql_orig: str, sql_rewritten: str) -> str:
    """Classify the edit between original and rewritten query.

    Returns one of:
      'identical'   — byte-identical
      'trivial'     — only comments/whitespace/date constants/cast syntax differ
      'structural'  — genuine structural change (GROUP BY, predicate, join, etc.)
    """
    if sql_orig == sql_rewritten:
        return "identical"
    n_orig = _normalize_for_diff(sql_orig)
    n_rew = _normalize_for_diff(sql_rewritten)
    if n_orig == n_rew:
        return "trivial"
    return "structural"


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

_CTE_PAT = re.compile(r"\bWITH\b", re.IGNORECASE)
_SUBQUERY_PAT = re.compile(r"\(\s*SELECT\b", re.IGNORECASE)
_JOIN_PAT = re.compile(r"\bJOIN\b", re.IGNORECASE)
_NESTED_CTE_PAT = re.compile(
    r"\bWITH\b.*\bAS\s*\(.*\bWITH\b", re.IGNORECASE | re.DOTALL,
)
_TABLE_REF_PAT = re.compile(r"(?:\bFROM\b|\bJOIN\b)\s+(\w+)", re.IGNORECASE)


def _prompt_tier(qid: int) -> str:
    for tier, lo, hi in PROMPT_TIERS:
        if lo <= qid <= hi:
            return tier
    return "unknown"


def _count_joins(sql: str) -> int:
    return len(_JOIN_PAT.findall(sql))


def _count_tables(sql: str) -> int:
    refs = _TABLE_REF_PAT.findall(sql)
    return len(set(t.lower() for t in refs))


def _classify_complexity(sql: str) -> str:
    has_cte = bool(_CTE_PAT.search(sql))
    has_subquery = bool(_SUBQUERY_PAT.search(sql))
    n_joins = _count_joins(sql)
    nested_cte = bool(_NESTED_CTE_PAT.search(sql))

    if (has_cte and has_subquery) or n_joins >= 7 or nested_cte:
        return "complex"
    if has_cte or has_subquery or n_joins >= 4:
        return "moderate"
    return "simple"


# ---------------------------------------------------------------------------
# Stratified sampling
# ---------------------------------------------------------------------------


def _stratified_sample(
    pairs: list[dict],
    sample_size: int,
    seed: int,
) -> list[dict]:
    """Proportional stratified sample by (prompt_tier, complexity)."""
    if len(pairs) <= sample_size:
        return pairs

    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in pairs:
        key = (p["prompt_tier"], p["complexity"])
        strata[key].append(p)

    rng = random.Random(seed)
    sampled: list[dict] = []

    total = len(pairs)
    allocated: dict[tuple[str, str], int] = {}
    remainder_pool: list[tuple[tuple[str, str], float]] = []

    for key, group in strata.items():
        exact = len(group) / total * sample_size
        floor_n = int(exact)
        allocated[key] = floor_n
        remainder_pool.append((key, exact - floor_n))

    remaining = sample_size - sum(allocated.values())
    remainder_pool.sort(key=lambda x: x[1], reverse=True)
    for i in range(remaining):
        key = remainder_pool[i][0]
        allocated[key] = allocated.get(key, 0) + 1

    for key, group in strata.items():
        n = min(allocated.get(key, 0), len(group))
        sampled.extend(rng.sample(group, n))

    sampled.sort(key=lambda p: int(p["pair_id"]))
    return sampled


# ---------------------------------------------------------------------------
# Process one dataset — original vs rewritten
# ---------------------------------------------------------------------------


def _process_orig_vs_rewritten(
    dataset: str,
    sample_size: int,
    seed: int,
) -> None:
    """Build sample of (original, rewritten) pairs with structural diffs."""
    queries_dir = DATA_ROOT / dataset / "queries_generated"
    if not queries_dir.is_dir():
        print(f"  WARNING: {queries_dir} does not exist, skipping.")
        return

    print(f"\n{'=' * 70}")
    print(f"  Dataset: {dataset}  (mode: orig_vs_rewritten)")
    print(f"  Scanning: {queries_dir}")
    print(f"{'=' * 70}")

    skip_reasons: Counter[str] = Counter()
    edit_types: Counter[str] = Counter()
    pairs: list[dict] = []
    n_found = 0
    n_filtered = 0

    # Scan for .sql_rewritten files (faster than iterating 0..34999)
    rewritten_files = sorted(queries_dir.glob("*.sql_rewritten"))
    for rewritten_path in rewritten_files:
        qid_str = rewritten_path.stem  # e.g. "1234"
        if not qid_str.isdigit():
            continue
        qid = int(qid_str)
        orig_path = queries_dir / f"{qid}.sql"
        if not orig_path.exists():
            continue

        n_found += 1

        sql_orig = orig_path.read_text(encoding="utf-8").strip()
        sql_rewr = rewritten_path.read_text(encoding="utf-8").strip()

        # Classify edit type
        edit_type = _classify_edit(sql_orig, sql_rewr)
        edit_types[edit_type] += 1

        # Only keep structurally non-trivial edits
        if edit_type != "structural":
            continue

        # Pre-filter unsupported constructs
        hit1 = _prefilter(sql_orig)
        hit2 = _prefilter(sql_rewr)
        if hit1 is not None or hit2 is not None:
            reason = hit1 or hit2
            skip_reasons[reason] += 1
            n_filtered += 1
            continue

        # Classify using the original SQL
        tier = _prompt_tier(qid)
        complexity = _classify_complexity(sql_orig)
        n_joins = _count_joins(sql_orig)
        n_tables = _count_tables(sql_orig)
        has_cte = bool(_CTE_PAT.search(sql_orig))
        has_subquery = bool(_SUBQUERY_PAT.search(sql_orig))
        has_window = bool(re.search(r"\bOVER\s*\(", sql_orig, re.IGNORECASE))

        pairs.append({
            "pair_id": str(qid),
            "sql1": sql_orig,
            "sql2": sql_rewr,
            "edit_type": edit_type,
            "prompt_tier": tier,
            "complexity": complexity,
            "n_tables": n_tables,
            "n_joins": n_joins,
            "has_cte": has_cte,
            "has_subquery": has_subquery,
            "has_window": has_window,
        })

    # Stratified sample
    sampled = _stratified_sample(pairs, sample_size, seed)

    # Write output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{dataset}.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in sampled:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # --- Summary ---
    tier_counts: Counter[str] = Counter(p["prompt_tier"] for p in pairs)
    complexity_counts: Counter[str] = Counter(p["complexity"] for p in pairs)
    sample_tier_counts: Counter[str] = Counter(p["prompt_tier"] for p in sampled)
    sample_complexity_counts: Counter[str] = Counter(p["complexity"] for p in sampled)

    print(f"\n  Total orig+rewritten pairs:     {n_found}")
    print(f"  Edit types:")
    for etype in ("identical", "trivial", "structural"):
        print(f"    {etype:<15s} {edit_types.get(etype, 0):>6d}")
    print(f"  Structural (pre-filter):        {len(pairs) + n_filtered}")
    print(f"  Pre-filtered:                   {n_filtered}")
    print(f"  Surviving structural pairs:     {len(pairs)}")
    print(f"  Sample size:                    {len(sampled)}")
    print(f"  Output:                         {out_path}")

    if skip_reasons:
        print(f"\n  Skip reasons:")
        for reason, cnt in skip_reasons.most_common():
            print(f"    {reason:<22s} {cnt:>6d}")

    print(f"\n  Per-tier distribution (surviving → sampled):")
    for tier, _, _ in PROMPT_TIERS:
        total_t = tier_counts.get(tier, 0)
        samp_t = sample_tier_counts.get(tier, 0)
        print(f"    {tier}: {total_t:>6d} → {samp_t:>4d}")

    print(f"\n  Per-complexity distribution (surviving → sampled):")
    for bucket in ("simple", "moderate", "complex"):
        total_c = complexity_counts.get(bucket, 0)
        samp_c = sample_complexity_counts.get(bucket, 0)
        print(f"    {bucket:<10s}: {total_c:>6d} → {samp_c:>4d}")


# ---------------------------------------------------------------------------
# Process one dataset — rewritten vs compatible (legacy)
# ---------------------------------------------------------------------------


def _process_rewritten_vs_compat(
    dataset: str,
    sample_size: int,
    seed: int,
) -> None:
    """Build sample of (rewritten, compatible) pairs.  Legacy mode."""
    queries_dir = DATA_ROOT / dataset / "queries_generated"
    if not queries_dir.is_dir():
        print(f"  WARNING: {queries_dir} does not exist, skipping.")
        return

    print(f"\n{'=' * 70}")
    print(f"  Dataset: {dataset}  (mode: rewritten_vs_compat)")
    print(f"  Scanning: {queries_dir}")
    print(f"{'=' * 70}")

    skip_reasons: Counter[str] = Counter()
    pairs: list[dict] = []
    n_found = 0
    n_filtered = 0

    compatible_files = sorted(queries_dir.glob("*.sql_compatible"))
    for compatible_path in compatible_files:
        qid_str = compatible_path.stem  # e.g. "1234"
        if not qid_str.isdigit():
            continue
        qid = int(qid_str)
        rewritten_path = queries_dir / f"{qid}.sql_rewritten"
        if not rewritten_path.exists():
            continue

        n_found += 1
        sql1 = rewritten_path.read_text(encoding="utf-8").strip()
        sql2 = compatible_path.read_text(encoding="utf-8").strip()

        hit1 = _prefilter(sql1)
        hit2 = _prefilter(sql2)
        if hit1 is not None or hit2 is not None:
            reason = hit1 or hit2
            skip_reasons[reason] += 1
            n_filtered += 1
            continue

        combined = sql1 + "\n" + sql2
        tier = _prompt_tier(qid)
        complexity = _classify_complexity(combined)
        n_joins = _count_joins(combined)
        n_tables = _count_tables(combined)
        has_cte = bool(_CTE_PAT.search(combined))
        has_subquery = bool(_SUBQUERY_PAT.search(combined))
        has_window = bool(re.search(r"\bOVER\s*\(", combined, re.IGNORECASE))

        pairs.append({
            "pair_id": str(qid),
            "sql1": sql1,
            "sql2": sql2,
            "prompt_tier": tier,
            "complexity": complexity,
            "n_tables": n_tables,
            "n_joins": n_joins,
            "has_cte": has_cte,
            "has_subquery": has_subquery,
            "has_window": has_window,
        })

    sampled = _stratified_sample(pairs, sample_size, seed)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{dataset}.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in sampled:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    tier_counts = Counter(p["prompt_tier"] for p in pairs)
    complexity_counts = Counter(p["complexity"] for p in pairs)
    sample_tier_counts = Counter(p["prompt_tier"] for p in sampled)
    sample_complexity_counts = Counter(p["complexity"] for p in sampled)

    print(f"\n  Total pairs found (both files): {n_found}")
    print(f"  Pre-filtered:                   {n_filtered}")
    print(f"  Surviving pairs:                {len(pairs)}")
    print(f"  Sample size:                    {len(sampled)}")
    print(f"  Output:                         {out_path}")

    if skip_reasons:
        print(f"\n  Skip reasons:")
        for reason, cnt in skip_reasons.most_common():
            print(f"    {reason:<22s} {cnt:>6d}")

    print(f"\n  Per-tier distribution (surviving → sampled):")
    for tier, _, _ in PROMPT_TIERS:
        total_t = tier_counts.get(tier, 0)
        samp_t = sample_tier_counts.get(tier, 0)
        print(f"    {tier}: {total_t:>6d} → {samp_t:>4d}")

    print(f"\n  Per-complexity distribution (surviving → sampled):")
    for bucket in ("simple", "moderate", "complex"):
        total_c = complexity_counts.get(bucket, 0)
        samp_c = sample_complexity_counts.get(bucket, 0)
        print(f"    {bucket:<10s}: {total_c:>6d} → {samp_c:>4d}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Build stratified evaluation samples from SQLStorm query pairs.",
    )
    parser.add_argument(
        "--mode",
        choices=["orig_vs_rewritten", "rewritten_vs_compat"],
        default="orig_vs_rewritten",
        help="Comparison mode (default: orig_vs_rewritten)",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        default=DATASETS,
        choices=DATASETS,
        help="Datasets to process (default: all four)",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=400,
        help="Target sample size per dataset (default: 400)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility (default: 42)",
    )

    args = parser.parse_args(argv)

    for ds in args.datasets:
        if args.mode == "orig_vs_rewritten":
            _process_orig_vs_rewritten(ds, sample_size=args.sample_size, seed=args.seed)
        else:
            _process_rewritten_vs_compat(ds, sample_size=args.sample_size, seed=args.seed)

    print(f"\nDone. Samples written to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
