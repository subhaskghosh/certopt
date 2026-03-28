#!/usr/bin/env python3
"""Post-run analysis for CEGIS query optimization results.

Usage:
    python3 -m scripts.analyze_results results/eval_20260310/
    python3 -m scripts.analyze_results results/eval_20260310/ --compare results/eval_20260311/
    python3 -m scripts.analyze_results results/eval_20260310/ --timing
    python3 -m scripts.analyze_results results/eval_20260310/ --rejections
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

# Rejection reason keys we track
REJECTION_REASONS = [
    "non_equivalent",
    "structural",
    "solver_unknown",
    "compositional_inconclusive",
    "family_pruned",
]


def load_results(results_dir: str) -> list[dict]:
    """Load results from a directory or JSON file."""
    p = Path(results_dir)
    if p.is_file() and p.suffix == ".json":
        results_path = p
    else:
        results_path = p / "results.json"
        if not results_path.exists():
            # Try finding any JSON in the dir
            jsons = list(p.glob("*.json"))
            if jsons:
                results_path = jsons[0]
            else:
                print(f"No results.json in {results_dir}")
                sys.exit(1)

    data = json.load(open(results_path))

    # Handle wrapped format: {"results": [...], ...}
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    if isinstance(data, list):
        return data

    print(f"Unexpected format in {results_path}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def cmd_summary(results: list[dict], args: argparse.Namespace) -> None:
    """Print overall summary and per-query table."""
    total = len(results)
    n_improved = sum(1 for r in results if r.get("improved"))
    n_errors = sum(1 for r in results if r.get("error"))
    n_no_improvement = total - n_improved - n_errors

    speedups = [r["speedup"] for r in results if r.get("improved")]
    avg_speedup = statistics.mean(speedups) if speedups else 0.0
    max_speedup = max(speedups) if speedups else 0.0

    times = [r.get("total_time_ms", 0) for r in results if r.get("total_time_ms", 0) > 0]
    total_time_s = sum(times) / 1000.0

    pct = lambda n, d: f"{n/d*100:.1f}%" if d > 0 else "N/A"

    print("=" * 74)
    print("  JOB-Complex Evaluation Summary")
    print("=" * 74)
    print(f"\n  {'Metric':<30} {'Value':>20}")
    print(f"  {'-'*30} {'-'*20}")
    print(f"  {'Total queries':<30} {total:>20}")
    print(f"  {'Improved':<30} {f'{n_improved} ({pct(n_improved, total)})':>20}")
    print(f"  {'No improvement':<30} {n_no_improvement:>20}")
    print(f"  {'Errors':<30} {n_errors:>20}")
    if speedups:
        print(f"  {'Avg speedup (improved)':<30} {f'{avg_speedup:.2f}×':>20}")
        print(f"  {'Max speedup':<30} {f'{max_speedup:.2f}×':>20}")
    print(f"  {'Total time':<30} {f'{total_time_s:.1f}s':>20}")

    # --- Per-query table ---
    print(f"\n  {'Query':<12} {'Improved':>8} {'Speedup':>8} {'Cost Orig':>10} "
          f"{'Cost Opt':>10} {'Cands':>6} {'Verif':>6} {'Rej':>5} {'Time':>8}")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*10} {'-'*10} {'-'*6} {'-'*6} {'-'*5} {'-'*8}")

    for r in results:
        qid = r.get("id", "?")
        improved = "✓" if r.get("improved") else ("ERR" if r.get("error") else "✗")
        speedup_str = f"{r.get('speedup', 1.0):.2f}×" if r.get("improved") else "-"

        cost_orig = r.get("cost_original")
        cost_opt = r.get("cost_optimized")
        cost_orig_str = f"{cost_orig:.1f}" if cost_orig is not None else "-"
        cost_opt_str = f"{cost_opt:.1f}" if cost_opt is not None else "-"

        cands = r.get("total_candidates", 0)
        verif = r.get("n_verified", 0)
        rej = r.get("n_rejected", 0)
        time_ms = r.get("total_time_ms", 0)
        time_str = f"{time_ms:.0f}ms" if time_ms > 0 else "-"

        print(f"  {qid:<12} {improved:>8} {speedup_str:>8} {cost_orig_str:>10} "
              f"{cost_opt_str:>10} {cands:>6} {verif:>6} {rej:>5} {time_str:>8}")


# ---------------------------------------------------------------------------
# Rejection analysis
# ---------------------------------------------------------------------------


def cmd_rejections(results: list[dict], args: argparse.Namespace) -> None:
    """Breakdown of rejection reasons across all queries."""
    total_rejections: Counter = Counter()
    per_query: dict[str, Counter] = {}

    for r in results:
        qid = r.get("id", "?")
        reasons = r.get("rejection_reasons", {})
        per_query[qid] = Counter(reasons)
        for reason, count in reasons.items():
            total_rejections[reason] += count

    total_rej = sum(total_rejections.values())

    print("=" * 60)
    print("  Rejection Reason Analysis")
    print("=" * 60)
    print(f"\n  Total rejections: {total_rej}")
    print(f"\n  {'Reason':<35} {'Count':>8} {'%':>8}")
    print(f"  {'-'*35} {'-'*8} {'-'*8}")

    for reason in REJECTION_REASONS:
        count = total_rejections.get(reason, 0)
        if count > 0:
            pct = f"{count / total_rej * 100:.1f}%" if total_rej > 0 else "-"
            print(f"  {reason:<35} {count:>8} {pct:>8}")

    # Any reasons not in our known list
    for reason, count in total_rejections.most_common():
        if reason not in REJECTION_REASONS:
            pct = f"{count / total_rej * 100:.1f}%" if total_rej > 0 else "-"
            print(f"  {reason:<35} {count:>8} {pct:>8}")

    # Per-query breakdown for queries with rejections
    queries_with_rej = {qid: c for qid, c in per_query.items() if sum(c.values()) > 0}
    if queries_with_rej:
        print(f"\n  Per-query rejection counts:")
        print(f"  {'Query':<12} {'Total':>6}  Top reasons")
        print(f"  {'-'*12} {'-'*6}  {'-'*30}")
        for qid in sorted(queries_with_rej):
            c = queries_with_rej[qid]
            total_q = sum(c.values())
            top = ", ".join(f"{r}={n}" for r, n in c.most_common(3))
            print(f"  {qid:<12} {total_q:>6}  {top}")


# ---------------------------------------------------------------------------
# Timing analysis
# ---------------------------------------------------------------------------


def cmd_timing(results: list[dict], args: argparse.Namespace) -> None:
    """Timing analysis: mean/median/p95/max of total_time_ms and solver_time_ms."""
    total_times = sorted(
        r.get("total_time_ms", 0) for r in results if r.get("total_time_ms", 0) > 0
    )
    solver_times = sorted(
        r.get("solver_time_ms", 0) for r in results if r.get("solver_time_ms", 0) > 0
    )

    def _stats(vals: list[float], label: str) -> None:
        if not vals:
            print(f"  {label}: no data")
            return
        mean = statistics.mean(vals)
        median = statistics.median(vals)
        p95_idx = min(int(len(vals) * 0.95), len(vals) - 1)
        p95 = vals[p95_idx]
        mx = max(vals)
        print(f"  {label:<25} {f'{mean:.0f}ms':>10} {f'{median:.0f}ms':>10} "
              f"{f'{p95:.0f}ms':>10} {f'{mx:.0f}ms':>10}")

    print("=" * 74)
    print("  Timing Analysis")
    print("=" * 74)
    print(f"\n  N = {len(total_times)} queries with timing data\n")
    print(f"  {'Metric':<25} {'Mean':>10} {'Median':>10} {'P95':>10} {'Max':>10}")
    print(f"  {'-'*25} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
    _stats(total_times, "total_time_ms")
    _stats(solver_times, "solver_time_ms")

    # Time distribution buckets
    if total_times:
        buckets = [100, 500, 1000, 5000, 10000]
        print(f"\n  Time distribution (total_time_ms):")
        for threshold in buckets:
            count = sum(1 for t in total_times if t <= threshold)
            print(f"    ≤{threshold:>6}ms: {count:>4} ({count/len(total_times)*100:.0f}%)")
        over = sum(1 for t in total_times if t > buckets[-1])
        if over:
            print(f"    >{buckets[-1]:>6}ms: {over:>4} ({over/len(total_times)*100:.0f}%)")


# ---------------------------------------------------------------------------
# Compare two runs
# ---------------------------------------------------------------------------


def cmd_compare(results: list[dict], args: argparse.Namespace) -> None:
    """Compare two evaluation runs side by side."""
    results_b = load_results(args.compare)

    map_a = {r.get("id"): r for r in results}
    map_b = {r.get("id"): r for r in results_b}
    ids_a = set(map_a.keys())
    ids_b = set(map_b.keys())
    overlap = ids_a & ids_b

    # Per-run stats
    imp_a = sum(1 for r in results if r.get("improved"))
    imp_b = sum(1 for r in results_b if r.get("improved"))
    sp_a = [r["speedup"] for r in results if r.get("improved")]
    sp_b = [r["speedup"] for r in results_b if r.get("improved")]
    avg_a = statistics.mean(sp_a) if sp_a else 0.0
    avg_b = statistics.mean(sp_b) if sp_b else 0.0

    pct = lambda n, d: f"{n/d*100:.1f}%" if d > 0 else "N/A"

    print("=" * 74)
    print("  Run Comparison")
    print("=" * 74)
    print(f"\n  {'Metric':<30} {'Run A':>20} {'Run B':>20}")
    print(f"  {'-'*30} {'-'*20} {'-'*20}")
    print(f"  {'Total queries':<30} {len(results):>20} {len(results_b):>20}")
    print(f"  {'Improved':<30} {f'{imp_a} ({pct(imp_a, len(results))})':>20} "
          f"{f'{imp_b} ({pct(imp_b, len(results_b))})':>20}")
    print(f"  {'Avg speedup (improved)':<30} {f'{avg_a:.2f}×':>20} {f'{avg_b:.2f}×':>20}")
    print(f"  {'Overlap queries':<30} {len(overlap):>20}")

    if not overlap:
        print("\n  No overlapping queries to compare.")
        return

    # Classification on overlap
    both_improved = []
    a_only = []
    b_only = []
    neither = []

    for qid in sorted(overlap):
        a_imp = map_a[qid].get("improved", False)
        b_imp = map_b[qid].get("improved", False)
        if a_imp and b_imp:
            both_improved.append(qid)
        elif a_imp and not b_imp:
            a_only.append(qid)
        elif not a_imp and b_imp:
            b_only.append(qid)
        else:
            neither.append(qid)

    print(f"\n  On {len(overlap)} overlapping queries:")
    print(f"    Both improved:    {len(both_improved)}")
    print(f"    A improved only:  {len(a_only)}")
    print(f"    B improved only:  {len(b_only)}")
    print(f"    Neither improved: {len(neither)}")

    # Show A-only wins
    if a_only and len(a_only) <= 20:
        print(f"\n  A improved but B did not:")
        for qid in a_only:
            sp = map_a[qid].get("speedup", 1.0)
            print(f"    {qid:<12} speedup={sp:.2f}×")

    # Show B-only wins
    if b_only and len(b_only) <= 20:
        print(f"\n  B improved but A did not:")
        for qid in b_only:
            sp = map_b[qid].get("speedup", 1.0)
            print(f"    {qid:<12} speedup={sp:.2f}×")

    # Both improved: compare speedups
    if both_improved:
        a_better = sum(
            1 for qid in both_improved
            if map_a[qid].get("speedup", 1.0) > map_b[qid].get("speedup", 1.0)
        )
        b_better = len(both_improved) - a_better
        print(f"\n  Both improved ({len(both_improved)} queries):")
        print(f"    A has higher speedup: {a_better}")
        print(f"    B has higher speedup: {b_better}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description="Post-run analysis for CEGIS query optimization results",
    )
    p.add_argument(
        "results_dir",
        help="Path to results directory or results.json",
    )
    p.add_argument(
        "--compare",
        type=str,
        default=None,
        help="Compare with another results directory",
    )
    p.add_argument(
        "--timing",
        action="store_true",
        help="Show timing analysis (mean/median/p95/max)",
    )
    p.add_argument(
        "--rejections",
        action="store_true",
        help="Show rejection reason breakdown",
    )
    args = p.parse_args()

    results = load_results(args.results_dir)
    print(f"Loaded {len(results)} results from {args.results_dir}\n")

    if args.compare:
        cmd_compare(results, args)
    elif args.timing:
        cmd_timing(results, args)
    elif args.rejections:
        cmd_rejections(results, args)
    else:
        cmd_summary(results, args)


if __name__ == "__main__":
    main()
