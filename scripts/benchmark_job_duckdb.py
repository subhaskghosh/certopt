#!/usr/bin/env python3
"""Benchmark JOB-Complex original vs optimized queries on DuckDB (IMDB).

Runs each verified query 3 times, takes median wall-clock, computes speedup.
"""

import json
import statistics
import time
from pathlib import Path

import duckdb

DB_PATH = "data/JOB-Complex/imdb.duckdb"
RESULTS_PATH = "results/job_complex/results.json"
OUTPUT_PATH = "results/job_complex/duckdb_exec_benchmark.json"
N_RUNS = 3


def benchmark_query(con, sql, n_runs=N_RUNS):
    """Run a query n_runs times, return list of wall-clock seconds."""
    times = []
    for _ in range(n_runs):
        t0 = time.perf_counter()
        try:
            con.execute(sql).fetchall()
            elapsed = time.perf_counter() - t0
        except Exception as e:
            elapsed = None
            print(f"  ERROR: {e}")
        times.append(elapsed)
    return times


def main():
    with open(RESULTS_PATH) as f:
        queries = json.load(f)

    improved = [q for q in queries if q.get("improved")]
    print(f"Benchmarking {len(improved)} verified queries on DuckDB ({DB_PATH})")
    print(f"Runs per query: {N_RUNS}")
    print()

    con = duckdb.connect(DB_PATH, read_only=True)

    results = []
    orig_medians = []
    opt_medians = []

    for q in improved:
        qid = q["id"]
        orig_sql = q["original_sql"]
        opt_sql = q["optimized_sql"]

        print(f"  {qid}: ", end="", flush=True)

        # Warmup
        try:
            con.execute(orig_sql).fetchall()
        except Exception:
            pass

        orig_times = benchmark_query(con, orig_sql)
        opt_times = benchmark_query(con, opt_sql)

        orig_valid = [t for t in orig_times if t is not None]
        opt_valid = [t for t in opt_times if t is not None]

        if orig_valid and opt_valid:
            orig_med = statistics.median(orig_valid)
            opt_med = statistics.median(opt_valid)
            speedup = orig_med / opt_med if opt_med > 0 else None
            orig_medians.append(orig_med)
            opt_medians.append(opt_med)
        else:
            orig_med = opt_med = speedup = None

        entry = {
            "id": qid,
            "original_times_s": [round(t, 4) if t else None for t in orig_times],
            "optimized_times_s": [round(t, 4) if t else None for t in opt_times],
            "original_median_s": round(orig_med, 4) if orig_med else None,
            "optimized_median_s": round(opt_med, 4) if opt_med else None,
            "speedup": round(speedup, 3) if speedup else None,
        }
        results.append(entry)
        print(f"orig={orig_med:.3f}s  opt={opt_med:.3f}s  speedup={speedup:.3f}x"
              if speedup else "FAILED")

    con.close()

    # Aggregate stats
    speedups = [r["speedup"] for r in results if r["speedup"] is not None]
    overall_median_speedup = round(statistics.median(speedups), 3) if speedups else None
    overall_mean_speedup = round(statistics.mean(speedups), 3) if speedups else None
    overall_orig_total = round(sum(orig_medians), 2)
    overall_opt_total = round(sum(opt_medians), 2)

    summary = {
        "description": "DuckDB execution benchmark: JOB-Complex original vs optimized queries on full IMDB database",
        "database": DB_PATH,
        "n_queries": len(improved),
        "n_runs_per_query": N_RUNS,
        "median_speedup": overall_median_speedup,
        "mean_speedup": overall_mean_speedup,
        "total_original_time_s": overall_orig_total,
        "total_optimized_time_s": overall_opt_total,
        "per_query": results,
    }

    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*60}")
    print(f"  Queries benchmarked:  {len(speedups)}")
    print(f"  Median speedup:       {overall_median_speedup}x")
    print(f"  Mean speedup:         {overall_mean_speedup}x")
    print(f"  Total orig time:      {overall_orig_total}s")
    print(f"  Total opt time:       {overall_opt_total}s")
    print(f"{'='*60}")
    print(f"Saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
