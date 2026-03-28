"""Scan SQLStorm query files, parse with sql_to_ir, and classify by feature coverage.

Usage:
    python3 -m scripts.filter_sqlstorm                              # default: stackoverflow
    python3 -m scripts.filter_sqlstorm --dataset all                # all datasets
    python3 -m scripts.filter_sqlstorm --dataset tpcds --verbose    # verbose on tpcds
    python3 -m scripts.filter_sqlstorm --max-queries 500            # quick test
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

DATASETS = ["stackoverflow", "tpcds", "tpch", "job"]

DATA_ROOT = Path("data/SQLStorm/v1.0")

# ---------------------------------------------------------------------------
# Pre-filter patterns (unsupported constructs)
# ---------------------------------------------------------------------------

_PREFILTER_PATTERNS: dict[str, re.Pattern[str]] = {
    "STRING_AGG": re.compile(r"\bSTRING_AGG\b", re.IGNORECASE),
    "ARRAY_AGG": re.compile(r"\bARRAY_AGG\b", re.IGNORECASE),
    "UNNEST": re.compile(r"\bUNNEST\b", re.IGNORECASE),
    "LATERAL": re.compile(r"\bLATERAL\b", re.IGNORECASE),
    "PG_CAST": re.compile(r"::"),
    "WITH_RECURSIVE": re.compile(r"\bWITH\s+RECURSIVE\b", re.IGNORECASE),
    "FETCH_FIRST": re.compile(r"\bFETCH\s+(FIRST|NEXT)\b", re.IGNORECASE),
}

# ---------------------------------------------------------------------------
# Feature classification patterns
# ---------------------------------------------------------------------------

_FEATURE_PATTERNS: dict[str, re.Pattern[str]] = {
    "has_window": re.compile(r"OVER\s*\(", re.IGNORECASE),
    "has_exists": re.compile(r"\bEXISTS\s*\(", re.IGNORECASE),
    "has_in_subquery": re.compile(r"\bIN\s*\(\s*SELECT", re.IGNORECASE),
    "has_group_by": re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE),
    "has_having": re.compile(r"\bHAVING\b", re.IGNORECASE),
    "has_order_by": re.compile(r"\bORDER\s+BY\b", re.IGNORECASE),
    "has_distinct": re.compile(r"\bDISTINCT\b", re.IGNORECASE),
    "has_cte": re.compile(r"\bWITH\b", re.IGNORECASE),
    "has_subquery": re.compile(r"\(\s*SELECT", re.IGNORECASE),
    "has_union": re.compile(r"\bUNION\b", re.IGNORECASE),
    "has_left_join": re.compile(r"\bLEFT\s+(OUTER\s+)?JOIN\b", re.IGNORECASE),
    "has_self_join": re.compile(r"placeholder"),  # handled specially
    "has_case": re.compile(r"\bCASE\b", re.IGNORECASE),
    "has_like": re.compile(r"\bLIKE\b", re.IGNORECASE),
    "has_between": re.compile(r"\bBETWEEN\b", re.IGNORECASE),
    "has_coalesce": re.compile(r"\bCOALESCE\b", re.IGNORECASE),
}

# Pattern to extract table names from FROM / JOIN clauses
_TABLE_REF_PATTERN = re.compile(
    r"(?:\bFROM\b|\bJOIN\b)\s+(\w+)", re.IGNORECASE,
)


def _classify_features(sql: str) -> dict[str, bool]:
    """Classify SQL text features using regex patterns."""
    features: dict[str, bool] = {}
    for name, pat in _FEATURE_PATTERNS.items():
        if name == "has_self_join":
            # Check if same table appears 2+ times in FROM/JOIN clauses
            tables = _TABLE_REF_PATTERN.findall(sql)
            table_counts = Counter(t.lower() for t in tables)
            features[name] = any(c >= 2 for c in table_counts.values())
        else:
            features[name] = bool(pat.search(sql))
    return features


def _prefilter(sql: str) -> str | None:
    """Return the name of the first unsupported construct found, or None."""
    for name, pat in _PREFILTER_PATTERNS.items():
        if pat.search(sql):
            return name
    return None


# ---------------------------------------------------------------------------
# Feature display names for summary
# ---------------------------------------------------------------------------

_FEATURE_LABELS: dict[str, str] = {
    "has_window": "Window functions",
    "has_exists": "EXISTS subquery",
    "has_in_subquery": "IN subquery",
    "has_group_by": "GROUP BY",
    "has_having": "HAVING",
    "has_order_by": "ORDER BY",
    "has_distinct": "DISTINCT",
    "has_cte": "CTE (WITH)",
    "has_subquery": "Subquery",
    "has_union": "UNION",
    "has_left_join": "LEFT JOIN",
    "has_self_join": "Self-join",
    "has_case": "CASE",
    "has_like": "LIKE",
    "has_between": "BETWEEN",
    "has_coalesce": "COALESCE",
}


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def _process_dataset(
    dataset: str,
    *,
    output_dir: Path | None,
    verbose: bool,
    max_queries: int | None,
) -> dict:
    """Process all queries in a dataset. Returns summary dict."""
    from optim.parser.sql_to_ir import sql_to_ir

    queries_dir = DATA_ROOT / dataset / "queries"
    if not queries_dir.is_dir():
        print(f"  WARNING: {queries_dir} does not exist, skipping.")
        return {}

    sql_files = sorted(queries_dir.glob("*.sql"), key=lambda p: p.stem)
    total = len(sql_files)
    if max_queries is not None:
        sql_files = sql_files[:max_queries]
        total = len(sql_files)

    # Auto-generate output directory
    if output_dir is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out = Path(f"results/sqlstorm_filter_{dataset}_{ts}")
    else:
        out = output_dir
    out.mkdir(parents=True, exist_ok=True)

    results_file = out / "results.jsonl"

    prefilter_counts: Counter[str] = Counter()
    n_prefiltered = 0
    n_parse_ok = 0
    n_parse_fail = 0
    feature_counts: Counter[str] = Counter()
    window_ids: list[str] = []
    exists_ids: list[str] = []
    subquery_ids: list[str] = []

    print(f"\n{'='*70}")
    print(f"  Dataset: {dataset}")
    print(f"  Queries directory: {queries_dir}")
    print(f"  Total query files: {total}")
    print(f"  Output: {out}")
    print(f"{'='*70}\n")

    t_start = time.monotonic()

    with open(results_file, "w") as fout:
        for i, sql_file in enumerate(sql_files):
            query_id = sql_file.stem
            sql = sql_file.read_text(encoding="utf-8", errors="replace").strip()

            # Progress every 1000 queries
            if (i + 1) % 1000 == 0 or i == 0:
                print(
                    f"  [{i+1}/{total}] {n_parse_ok} parsed, "
                    f"{n_parse_fail} failed, {n_prefiltered} pre-filtered..."
                )

            # Pre-filter check
            blocked_by = _prefilter(sql)
            if blocked_by is not None:
                n_prefiltered += 1
                prefilter_counts[blocked_by] += 1
                entry = {
                    "id": query_id,
                    "dataset": dataset,
                    "parse_ok": False,
                    "features": {},
                    "error": f"pre-filtered: {blocked_by}",
                }
                fout.write(json.dumps(entry) + "\n")
                if verbose:
                    print(f"    {query_id}: pre-filtered ({blocked_by})")
                continue

            # Attempt parse
            ir, error = sql_to_ir(sql, dialect="postgres")
            if ir is not None:
                n_parse_ok += 1
                features = _classify_features(sql)
                entry = {
                    "id": query_id,
                    "dataset": dataset,
                    "parse_ok": True,
                    "features": features,
                    "error": None,
                }

                # Track feature IDs
                if features.get("has_window"):
                    window_ids.append(query_id)
                if features.get("has_exists"):
                    exists_ids.append(query_id)
                if features.get("has_in_subquery"):
                    subquery_ids.append(query_id)

                for feat, val in features.items():
                    if val:
                        feature_counts[feat] += 1
            else:
                n_parse_fail += 1
                entry = {
                    "id": query_id,
                    "dataset": dataset,
                    "parse_ok": False,
                    "features": {},
                    "error": error,
                }
                if verbose:
                    print(f"    {query_id}: parse failed — {error}")

            fout.write(json.dumps(entry) + "\n")

    t_total = time.monotonic() - t_start
    n_attempted = n_parse_ok + n_parse_fail

    # --- Print summary ---
    print(f"\n  Dataset: {dataset}")
    print(f"  Total queries:    {total}")

    prefilter_detail = ", ".join(
        f"{name}: {cnt}" for name, cnt in prefilter_counts.most_common()
    )
    print(f"  Pre-filtered:     {n_prefiltered}  ({prefilter_detail})")
    print(f"  Parse attempted:  {n_attempted}")
    if n_attempted > 0:
        pct = n_parse_ok / n_attempted * 100
        print(f"  Parse success:    {n_parse_ok}  ({pct:.1f}%)")
    else:
        print(f"  Parse success:    {n_parse_ok}")
    print(f"  Parse failed:     {n_parse_fail}")

    if n_parse_ok > 0:
        print(f"\n  Feature distribution (among parseable):")
        for feat in _FEATURE_PATTERNS:
            cnt = feature_counts.get(feat, 0)
            pct = cnt / n_parse_ok * 100
            label = _FEATURE_LABELS.get(feat, feat)
            print(f"    {label + ':':<22s} {cnt:>6d}  ({pct:.1f}%)")

    # Count GROUP BY + JOIN for pre-agg benchmark
    # (we don't track per-query combos in counts, so compute from features)
    print(f"\n  Recommended subsets:")
    print(f"    Window benchmark (WF.1):   {len(window_ids)} queries with OVER")
    print(f"    Subquery benchmark (R7):   {len(exists_ids)} queries with EXISTS")
    print(f"    IN subquery set:           {len(subquery_ids)} queries with IN subquery")

    print(f"\n  Time: {t_total:.1f}s")

    # --- Save output files ---
    summary = {
        "dataset": dataset,
        "total_queries": total,
        "pre_filtered": n_prefiltered,
        "prefilter_breakdown": dict(prefilter_counts),
        "parse_attempted": n_attempted,
        "parse_success": n_parse_ok,
        "parse_failed": n_parse_fail,
        "feature_counts": {k: feature_counts.get(k, 0) for k in _FEATURE_PATTERNS},
        "window_query_count": len(window_ids),
        "exists_query_count": len(exists_ids),
        "in_subquery_count": len(subquery_ids),
        "time_seconds": round(t_total, 2),
    }
    with open(out / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    (out / "window_queries.txt").write_text("\n".join(window_ids) + "\n")
    (out / "exists_queries.txt").write_text("\n".join(exists_ids) + "\n")
    (out / "subquery_queries.txt").write_text("\n".join(subquery_ids) + "\n")

    print(f"\n  Results saved to {out}/")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Scan SQLStorm queries, parse with sql_to_ir, classify by feature.",
    )
    parser.add_argument(
        "--dataset",
        default="stackoverflow",
        choices=DATASETS + ["all"],
        help="Dataset to process (default: stackoverflow)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory (default: auto-generated with timestamp)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-query details for pre-filtered and failed parses",
    )
    parser.add_argument(
        "--max-queries",
        type=int,
        default=None,
        help="Limit number of queries per dataset (for quick testing)",
    )

    args = parser.parse_args(argv)

    datasets = DATASETS if args.dataset == "all" else [args.dataset]

    for ds in datasets:
        output_dir = None
        if args.output is not None:
            output_dir = Path(args.output) if len(datasets) == 1 else Path(args.output) / ds
        _process_dataset(
            ds,
            output_dir=output_dir,
            verbose=args.verbose,
            max_queries=args.max_queries,
        )


if __name__ == "__main__":
    main()
