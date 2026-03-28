#!/usr/bin/env python3
"""Reproduce all tables and numbers for the Axis 2 (JOB-Complex) section.

Usage:
    python3 scripts/gen_job_complex_tables.py

Reads from:
    results/job_complex/results.json
    results/job_complex/summary.json
    data/JOB-Complex/JOB-Complex/JOB-Complex.sql

Outputs all numbers needed for the Axis 2 section of the paper.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────

RESULT_DIR = Path("results/job_complex")
SQL_FILE = Path("data/JOB-Complex/JOB-Complex/JOB-Complex.sql")


# ── Helpers ───────────────────────────────────────────────────────

def sep(n: int) -> str:
    """Format integer with comma separators."""
    return f"{n:,}"


def count_tables_in_query(sql: str) -> int:
    """Count table aliases in the FROM/JOIN clause (before WHERE)."""
    upper = sql.upper()
    where_idx = upper.find(" WHERE ")
    from_clause = sql[:where_idx] if where_idx > 0 else sql
    from_idx = from_clause.upper().find(" FROM ")
    table_part = from_clause[from_idx + 6:] if from_idx >= 0 else from_clause
    return len(re.findall(r"\w+\s+AS\s+\w+", table_part, re.IGNORECASE))


def load_table_counts() -> list[int]:
    """Read JOB-Complex.sql and return per-query table counts."""
    if not SQL_FILE.exists():
        return []
    lines = SQL_FILE.read_text().strip().splitlines()
    return [count_tables_in_query(line) for line in lines if line.strip()]


# ── Main ──────────────────────────────────────────────────────────

def main():
    if not (RESULT_DIR / "results.json").exists():
        print("  ⚠ results/job_complex/results.json not found")
        return

    results: list[dict] = json.loads((RESULT_DIR / "results.json").read_text())
    summary: dict = json.loads((RESULT_DIR / "summary.json").read_text())
    table_counts = load_table_counts()

    # Map query index (1-based from ID) to table count
    tc_map: dict[str, int] = {}
    for r in results:
        m = re.search(r"(\d+)$", r["id"])
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(table_counts):
                tc_map[r["id"]] = table_counts[idx]

    # ══════════════════════════════════════════════════════════════
    # 1. PER-QUERY TABLE
    # ══════════════════════════════════════════════════════════════
    print("=" * 100)
    print("TABLE: JOB-Complex Per-Query Results")
    print("=" * 100)
    header = (f"{'ID':<10} {'#Tbl':>5} {'Impr?':>6} "
              f"{'Cost_orig':>10} {'Cost_opt':>10} {'Red%':>7} "
              f"{'Speedup':>8} {'Time_ms':>10}")
    print(header)
    print("-" * 100)

    for r in results:
        qid = r["id"]
        ntbl = tc_map.get(qid, 0)
        improved = r["improved"]
        mark = "✓" if improved else "✗"
        c_orig = r["cost_original"]
        c_opt = r["cost_optimized"]
        red_pct = (1 - c_opt / c_orig) * 100 if c_orig > 0 else 0.0
        spd = r["speedup"]
        tms = r["total_time_ms"]
        print(f"{qid:<10} {ntbl:>5} {mark:>6} "
              f"{c_orig:>10.1f} {c_opt:>10.1f} {red_pct:>6.1f}% "
              f"{spd:>7.3f}× {tms:>10.1f}")

    print("-" * 100)

    # ══════════════════════════════════════════════════════════════
    # 2. AGGREGATE STATS
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("AGGREGATE STATS")
    print("=" * 100)

    total = len(results)
    improved_results = [r for r in results if r["improved"]]
    n_improved = len(improved_results)
    n_not_improved = total - n_improved

    speedups = [r["speedup"] for r in results if r["improved"]]
    cost_reds = [(1 - r["cost_optimized"] / r["cost_original"]) * 100
                 for r in results if r["improved"] and r["cost_original"] > 0]
    times = [r["total_time_ms"] for r in results]
    total_time_s = sum(times) / 1000.0

    print(f"  Total queries:          {total}")
    print(f"  Improved:               {n_improved} ({n_improved/total*100:.1f}%)")
    print(f"  Not improved:           {n_not_improved}")
    print()

    if speedups:
        print(f"  Speedup (improved only):")
        print(f"    Mean:                 {statistics.mean(speedups):.3f}×")
        print(f"    Median:               {statistics.median(speedups):.3f}×")
        print(f"    Max:                  {max(speedups):.3f}×")
        print(f"    Min:                  {min(speedups):.3f}×")
    print()

    if cost_reds:
        print(f"  Cost reduction % (improved only):")
        print(f"    Mean:                 {statistics.mean(cost_reds):.1f}%")
        print(f"    Median:               {statistics.median(cost_reds):.1f}%")
        print(f"    Max:                  {max(cost_reds):.1f}%")
    print()

    print(f"  Total time:             {total_time_s:.1f}s ({total_time_s/60:.1f}min)")
    print()

    # Cross-check with summary.json
    print(f"  ✓ Summary check:")
    print(f"    summary.total_queries = {summary['total_queries']} == {total}? "
          f"{'PASS' if summary['total_queries'] == total else 'FAIL'}")
    print(f"    summary.n_improved    = {summary['n_improved']} == {n_improved}? "
          f"{'PASS' if summary['n_improved'] == n_improved else 'FAIL'}")

    # ══════════════════════════════════════════════════════════════
    # 3. TABLE COUNT DISTRIBUTION
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("TABLE COUNT DISTRIBUTION (improved vs not-improved)")
    print("=" * 100)

    imp_tc = Counter(tc_map.get(r["id"], 0) for r in results if r["improved"])
    not_tc = Counter(tc_map.get(r["id"], 0) for r in results if not r["improved"])
    all_tc = sorted(set(imp_tc) | set(not_tc))

    print(f"  {'#Tables':>8} {'Improved':>10} {'Not-Impr':>10} {'Total':>8}")
    print(f"  {'-'*8} {'-'*10} {'-'*10} {'-'*8}")
    for tc in all_tc:
        i = imp_tc.get(tc, 0)
        n = not_tc.get(tc, 0)
        print(f"  {tc:>8} {i:>10} {n:>10} {i+n:>8}")
    print(f"  {'-'*8} {'-'*10} {'-'*10} {'-'*8}")
    print(f"  {'Total':>8} {sum(imp_tc.values()):>10} {sum(not_tc.values()):>10} "
          f"{sum(imp_tc.values())+sum(not_tc.values()):>8}")

    if tc_map:
        imp_tcs = [tc_map[r["id"]] for r in results if r["improved"] and r["id"] in tc_map]
        not_tcs = [tc_map[r["id"]] for r in results if not r["improved"] and r["id"] in tc_map]
        if imp_tcs:
            print(f"\n  Improved queries:   mean={statistics.mean(imp_tcs):.1f}, "
                  f"median={statistics.median(imp_tcs):.0f}, "
                  f"range=[{min(imp_tcs)}, {max(imp_tcs)}]")
        if not_tcs:
            print(f"  Not-improved:       mean={statistics.mean(not_tcs):.1f}, "
                  f"median={statistics.median(not_tcs):.0f}, "
                  f"range=[{min(not_tcs)}, {max(not_tcs)}]")

    # ══════════════════════════════════════════════════════════════
    # 4. REJECTION REASON BREAKDOWN
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("REJECTION REASON BREAKDOWN")
    print("=" * 100)

    reason_counter: Counter = Counter()
    total_rejected = 0
    total_candidates = 0
    total_verified = 0

    for r in results:
        total_candidates += r.get("total_candidates", 0)
        total_verified += r.get("n_verified", 0)
        n_rej = r.get("n_rejected", 0)
        total_rejected += n_rej
        for reason, count in r.get("rejection_reasons", {}).items():
            reason_counter[reason] += count

    print(f"  Total candidates:       {total_candidates}")
    print(f"  Total verified:         {total_verified}")
    print(f"  Total rejected:         {total_rejected}")
    print()
    print(f"  {'Reason':<50} {'Count':>8} {'%':>8}")
    print(f"  {'-'*50} {'-'*8} {'-'*8}")
    for reason, count in reason_counter.most_common():
        pct = count / total_rejected * 100 if total_rejected > 0 else 0
        print(f"  {reason:<50} {count:>8} {pct:>7.1f}%")

    # ══════════════════════════════════════════════════════════════
    # 5. TIMING DISTRIBUTION
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("TIMING DISTRIBUTION")
    print("=" * 100)

    times_sorted = sorted(times)
    p95_idx = int(len(times_sorted) * 0.95)

    print(f"  Median:                 {statistics.median(times):,.1f} ms")
    print(f"  Mean:                   {statistics.mean(times):,.1f} ms")
    print(f"  P95:                    {times_sorted[min(p95_idx, len(times_sorted)-1)]:,.1f} ms")
    print(f"  Max:                    {max(times):,.1f} ms")
    print(f"  Min:                    {min(times):,.1f} ms")
    print(f"  Std dev:                {statistics.stdev(times):,.1f} ms")
    print(f"  Total:                  {sum(times):,.1f} ms ({sum(times)/1000:.1f}s)")
    print()

    # Cross-check with summary
    print(f"  ✓ Summary check:")
    print(f"    summary.mean_time_ms   = {summary['mean_time_ms']:.1f} vs computed {statistics.mean(times):.1f}")
    print(f"    summary.median_time_ms = {summary['median_time_ms']:.1f} vs computed {statistics.median(times):.1f}")
    print(f"    summary.p95_time_ms    = {summary['p95_time_ms']:.1f} vs computed {times_sorted[min(p95_idx, len(times_sorted)-1)]:.1f}")
    print(f"    summary.max_time_ms    = {summary['max_time_ms']:.1f} vs computed {max(times):.1f}")

    print("\n" + "=" * 100)
    print("ALL CHECKS COMPLETE")
    print("=" * 100)


if __name__ == "__main__":
    main()
