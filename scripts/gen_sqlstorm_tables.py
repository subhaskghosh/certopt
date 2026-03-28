#!/usr/bin/env python3
"""Reproduce all tables and numbers for the Axis 3 (SQLStorm) section.

Supports two evaluation modes:
  Option 1 (orig): orig_vs_rewritten — SQLStorm PF pass rewrites
  Option 2 (llm):  LLM-generated semantically equivalent rewrites

Usage:
    python3 scripts/gen_sqlstorm_tables.py              # Option 1 only
    python3 scripts/gen_sqlstorm_tables.py --llm         # Option 2 only
    python3 scripts/gen_sqlstorm_tables.py --both        # Both options

Reads from:
    results/sqlstorm_{dataset}/results.json              (Option 1)
    results/sqlstorm_{dataset}_llm/results.json          (Option 2)
    scripts/sqlstorm_sample/{dataset}.jsonl               (Option 1 metadata)
    scripts/sqlstorm_sample/{dataset}_llm.jsonl           (Option 2 metadata)

Outputs all numbers needed for the Axis 3 section of the paper.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────

DATASETS = ["tpch", "tpcds", "stackoverflow", "job"]
RESULT_DIR_ORIG = "results/sqlstorm_{dataset}"
RESULT_DIR_LLM = "results/sqlstorm_{dataset}_llm"
SAMPLE_DIR = Path("scripts/sqlstorm_sample")


# ── Helpers ───────────────────────────────────────────────────────

def sep(n: int) -> str:
    """Format integer with comma separators."""
    return f"{n:,}"


def load_dataset(dataset: str, source: str = "orig") -> dict | None:
    """Load results.json and summary.json for a dataset, or None."""
    template = RESULT_DIR_LLM if source == "llm" else RESULT_DIR_ORIG
    rdir = Path(template.format(dataset=dataset))
    rpath = rdir / "results.json"
    spath = rdir / "summary.json"
    if not rpath.exists():
        return None
    return {
        "results": json.loads(rpath.read_text()),
        "summary": json.loads(spath.read_text()) if spath.exists() else {},
    }


def load_sample_metadata(dataset: str, source: str = "orig") -> dict[str, dict]:
    """Load per-pair metadata from the sample JSONL, keyed by pair_id."""
    suffix = "_llm" if source == "llm" else ""
    jpath = SAMPLE_DIR / f"{dataset}{suffix}.jsonl"
    meta: dict[str, dict] = {}
    if not jpath.exists():
        return meta
    with jpath.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            meta[str(obj["pair_id"])] = obj
    return meta


# ── Main ──────────────────────────────────────────────────────────

def _run_for_source(source: str):
    """Run all tables/analyses for a given source ('orig' or 'llm')."""
    label = "Option 1: orig_vs_rewritten" if source == "orig" else "Option 2: LLM rewrites"
    dir_label = "sqlstorm_{ds}" if source == "orig" else "sqlstorm_{ds}_llm"

    print(f"\n{'#'*120}")
    print(f"#  {label}")
    print(f"{'#'*120}")

    suites: dict[str, dict] = {}
    for ds in DATASETS:
        data = load_dataset(ds, source=source)
        if data is not None:
            suites[ds] = data
        else:
            actual_dir = dir_label.format(ds=ds)
            print(f"  ⚠ Skipping {ds} (results/{actual_dir}/results.json not found)")

    if not suites:
        print("No results found.")
        return

    # Pre-load sample metadata for all datasets
    sample_meta: dict[str, dict[str, dict]] = {}
    for ds in DATASETS:
        sample_meta[ds] = load_sample_metadata(ds, source=source)

    # ══════════════════════════════════════════════════════════════
    # 1. CROSS-DATASET SUMMARY TABLE
    # ══════════════════════════════════════════════════════════════
    print("=" * 120)
    print("TABLE: Cross-Dataset Summary (SQLStorm)")
    print("=" * 120)
    header = (f"{'Dataset':<16} {'Total':>8} {'Parsed':>8} {'EQU':>8} "
              f"{'NEQ':>8} {'UNK':>8} {'TMO':>8} {'PFail':>8} "
              f"{'Dec%':>8} {'Mean_ms':>10} {'Total_s':>10}")
    print(header)
    print("-" * 120)

    agg = {"total": 0, "parsed": 0, "equ": 0, "neq": 0, "unk": 0,
           "tmo": 0, "pfail": 0, "error": 0, "total_time_s": 0.0}

    for ds in DATASETS:
        if ds not in suites:
            continue
        s = suites[ds]["summary"]
        results = suites[ds]["results"]

        total = s.get("total_pairs", len(results))
        parsed = s.get("parsed", 0)
        equ = s.get("our_equ", 0)
        neq = s.get("our_neq", 0)
        unk = s.get("our_unknown", 0)
        tmo = s.get("our_tmo", 0)
        pfail = s.get("our_parse_fail", 0)
        error = s.get("our_error", 0)
        total_time = s.get("total_time_s", 0.0)
        mean_time = s.get("mean_time_ms", 0.0)

        decided = equ + neq
        dec_pct = decided / total * 100 if total > 0 else 0.0

        print(f"{ds:<16} {sep(total):>8} {sep(parsed):>8} {sep(equ):>8} "
              f"{sep(neq):>8} {sep(unk):>8} {sep(tmo):>8} {sep(pfail):>8} "
              f"{dec_pct:>7.1f}% {mean_time:>10.1f} {total_time:>10.1f}")

        agg["total"] += total
        agg["parsed"] += parsed
        agg["equ"] += equ
        agg["neq"] += neq
        agg["unk"] += unk
        agg["tmo"] += tmo
        agg["pfail"] += pfail
        agg["error"] += error
        agg["total_time_s"] += total_time

    print("-" * 120)

    # ══════════════════════════════════════════════════════════════
    # 2. AGGREGATE TOTALS
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("AGGREGATE TOTALS (all 4 datasets)")
    print("=" * 120)

    agg_decided = agg["equ"] + agg["neq"]
    agg_dec_pct = agg_decided / agg["total"] * 100 if agg["total"] > 0 else 0.0

    all_times = []
    for ds in DATASETS:
        if ds not in suites:
            continue
        for r in suites[ds]["results"]:
            all_times.append(r.get("time_ms", 0.0))

    agg_mean = statistics.mean(all_times) if all_times else 0.0

    print(f"  Total pairs:            {sep(agg['total'])}")
    print(f"  Parsed:                 {sep(agg['parsed'])}")
    print(f"  EQU:                    {sep(agg['equ'])}")
    print(f"  NEQ:                    {sep(agg['neq'])}")
    print(f"  UNKNOWN:                {sep(agg['unk'])}")
    print(f"  TMO:                    {sep(agg['tmo'])}")
    print(f"  PARSE_FAIL:             {sep(agg['pfail'])}")
    print(f"  ERROR:                  {sep(agg['error'])}")
    print(f"  Decision rate:          {agg_dec_pct:.1f}%")
    print(f"  Total time:             {agg['total_time_s']:.1f}s ({agg['total_time_s']/60:.1f}min)")
    if all_times:
        print(f"  Mean time (computed):   {agg_mean:.1f}ms")
    print()

    row_sum = agg["equ"] + agg["neq"] + agg["unk"] + agg["tmo"] + agg["pfail"] + agg["error"]
    print(f"  ✓ Row sum check: {sep(row_sum)} == {sep(agg['total'])}? "
          f"{'PASS' if row_sum == agg['total'] else 'FAIL'}")

    # ══════════════════════════════════════════════════════════════
    # 3. PER-COMPLEXITY BREAKDOWN
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("PER-COMPLEXITY BREAKDOWN")
    print("=" * 120)

    complexity_levels = ["simple", "moderate", "complex"]

    for ds in DATASETS:
        if ds not in suites:
            continue
        meta = sample_meta[ds]
        results = suites[ds]["results"]

        print(f"\n  {ds}:")
        print(f"  {'Complexity':<14} {'Total':>8} {'EQU':>8} {'NEQ':>8} "
              f"{'UNK':>8} {'TMO':>8} {'PFail':>8} {'Error':>8}")
        print(f"  {'-'*14} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")

        for cmplx in complexity_levels:
            subset = [r for r in results
                      if meta.get(str(r.get("pair_id", "")), {}).get("complexity") == cmplx]
            c = Counter(r.get("our_result", "ERROR") for r in subset)
            total_c = len(subset)
            print(f"  {cmplx:<14} {total_c:>8} {c.get('EQU', 0):>8} {c.get('NEQ', 0):>8} "
                  f"{c.get('UNKNOWN', 0):>8} {c.get('TMO', 0):>8} "
                  f"{c.get('PARSE_FAIL', 0):>8} {c.get('ERROR', 0):>8}")

        # Unmatched (no complexity in sample)
        unmatched = [r for r in results
                     if meta.get(str(r.get("pair_id", "")), {}).get("complexity") is None]
        if unmatched:
            c = Counter(r.get("our_result", "ERROR") for r in unmatched)
            print(f"  {'(unmatched)':<14} {len(unmatched):>8} {c.get('EQU', 0):>8} "
                  f"{c.get('NEQ', 0):>8} {c.get('UNKNOWN', 0):>8} {c.get('TMO', 0):>8} "
                  f"{c.get('PARSE_FAIL', 0):>8} {c.get('ERROR', 0):>8}")

    # ══════════════════════════════════════════════════════════════
    # 4. PER-PROMPT-TIER BREAKDOWN
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("PER-PROMPT-TIER BREAKDOWN")
    print("=" * 120)

    prompt_tiers = ["P1", "P2", "P3", "P4", "P5", "P6", "P7"]

    for ds in DATASETS:
        if ds not in suites:
            continue
        meta = sample_meta[ds]
        results = suites[ds]["results"]

        print(f"\n  {ds}:")
        print(f"  {'Tier':<8} {'Total':>8} {'EQU':>8} {'NEQ':>8} "
              f"{'UNK':>8} {'TMO':>8} {'PFail':>8} {'Error':>8}")
        print(f"  {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")

        for tier in prompt_tiers:
            subset = [r for r in results
                      if meta.get(str(r.get("pair_id", "")), {}).get("prompt_tier") == tier]
            if not subset:
                continue
            c = Counter(r.get("our_result", "ERROR") for r in subset)
            total_t = len(subset)
            print(f"  {tier:<8} {total_t:>8} {c.get('EQU', 0):>8} {c.get('NEQ', 0):>8} "
                  f"{c.get('UNKNOWN', 0):>8} {c.get('TMO', 0):>8} "
                  f"{c.get('PARSE_FAIL', 0):>8} {c.get('ERROR', 0):>8}")

        # Unmatched (no tier in sample)
        unmatched = [r for r in results
                     if meta.get(str(r.get("pair_id", "")), {}).get("prompt_tier") is None]
        if unmatched:
            c = Counter(r.get("our_result", "ERROR") for r in unmatched)
            print(f"  {'(none)':<8} {len(unmatched):>8} {c.get('EQU', 0):>8} "
                  f"{c.get('NEQ', 0):>8} {c.get('UNKNOWN', 0):>8} {c.get('TMO', 0):>8} "
                  f"{c.get('PARSE_FAIL', 0):>8} {c.get('ERROR', 0):>8}")

    # ══════════════════════════════════════════════════════════════
    # 5. TIMING DISTRIBUTION
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("TIMING DISTRIBUTION (per dataset)")
    print("=" * 120)

    for ds in DATASETS:
        if ds not in suites:
            continue
        s = suites[ds]["summary"]
        results = suites[ds]["results"]
        times = [r.get("time_ms", 0.0) for r in results]

        if not times:
            print(f"\n  {ds}: (no data)")
            continue

        times_sorted = sorted(times)
        p95_idx = int(len(times_sorted) * 0.95)

        print(f"\n  {ds}:")
        print(f"    Mean:                 {statistics.mean(times):,.1f} ms")
        print(f"    Median:               {statistics.median(times):,.1f} ms")
        print(f"    P95:                  {times_sorted[min(p95_idx, len(times_sorted)-1)]:,.1f} ms")
        print(f"    Max:                  {max(times):,.1f} ms")
        print(f"    Min:                  {min(times):,.1f} ms")
        if len(times) > 1:
            print(f"    Std dev:              {statistics.stdev(times):,.1f} ms")
        print(f"    Total:                {sum(times):,.1f} ms ({sum(times)/1000:.1f}s)")

        # Cross-check with summary
        print(f"    ✓ Summary check:")
        if s.get("mean_time_ms") is not None:
            print(f"      summary.mean_time_ms   = {s['mean_time_ms']:.1f} vs computed "
                  f"{statistics.mean(times):.1f}")
        if s.get("median_time_ms") is not None:
            print(f"      summary.median_time_ms = {s['median_time_ms']:.1f} vs computed "
                  f"{statistics.median(times):.1f}")
        if s.get("p95_time_ms") is not None:
            print(f"      summary.p95_time_ms    = {s['p95_time_ms']:.1f} vs computed "
                  f"{times_sorted[min(p95_idx, len(times_sorted)-1)]:.1f}")
        if s.get("max_time_ms") is not None:
            print(f"      summary.max_time_ms    = {s['max_time_ms']:.1f} vs computed "
                  f"{max(times):.1f}")

    # ══════════════════════════════════════════════════════════════
    # 6. VALIDATION ANALYSIS
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("VALIDATION ANALYSIS (NEQ pairs)")
    print("=" * 120)

    for ds in DATASETS:
        if ds not in suites:
            continue
        results = suites[ds]["results"]

        neq_pairs = [r for r in results if r.get("our_result") == "NEQ"]
        if not neq_pairs:
            print(f"\n  {ds}: 0 NEQ pairs")
            continue

        vs_counter: Counter = Counter()
        for r in neq_pairs:
            vs = r.get("validation_status", "unknown")
            vs_counter[vs] += 1

        has_witness = sum(1 for r in neq_pairs if r.get("witness_db"))
        no_witness = len(neq_pairs) - has_witness

        print(f"\n  {ds}: {len(neq_pairs)} NEQ pairs")
        print(f"    {'Status':<20} {'Count':>8} {'%':>8}")
        print(f"    {'-'*20} {'-'*8} {'-'*8}")
        for status, count in vs_counter.most_common():
            pct = count / len(neq_pairs) * 100
            print(f"    {status:<20} {count:>8} {pct:>7.1f}%")
        print(f"    {'─'*20} {'─'*8} {'─'*8}")
        print(f"    {'has witness_db':<20} {has_witness:>8}")
        print(f"    {'no witness_db':<20} {no_witness:>8}")

    # ══════════════════════════════════════════════════════════════
    # 7. CROSS-CHECKS (summary.json vs computed)
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("CROSS-CHECKS (summary.json vs computed)")
    print("=" * 120)

    for ds in DATASETS:
        if ds not in suites:
            continue
        s = suites[ds]["summary"]
        results = suites[ds]["results"]

        computed_total = len(results)
        computed_parsed = sum(1 for r in results if r.get("parse_ok", False))
        computed_equ = sum(1 for r in results if r.get("our_result") == "EQU")
        computed_neq = sum(1 for r in results if r.get("our_result") == "NEQ")
        computed_unk = sum(1 for r in results if r.get("our_result") == "UNKNOWN")
        computed_tmo = sum(1 for r in results if r.get("our_result") == "TMO")
        computed_pfail = sum(1 for r in results if r.get("our_result") == "PARSE_FAIL")
        computed_error = sum(1 for r in results if r.get("our_result") == "ERROR")

        checks = [
            ("total_pairs", s.get("total_pairs"), computed_total),
            ("parsed", s.get("parsed"), computed_parsed),
            ("our_equ", s.get("our_equ"), computed_equ),
            ("our_neq", s.get("our_neq"), computed_neq),
            ("our_unknown", s.get("our_unknown"), computed_unk),
            ("our_tmo", s.get("our_tmo"), computed_tmo),
            ("our_parse_fail", s.get("our_parse_fail"), computed_pfail),
            ("our_error", s.get("our_error"), computed_error),
        ]

        print(f"\n  {ds}:")
        for field, summary_val, computed_val in checks:
            if summary_val is None:
                print(f"    {field:<20} summary=N/A  computed={computed_val}")
                continue
            ok = "PASS" if summary_val == computed_val else "FAIL"
            print(f"    {field:<20} summary={summary_val}  computed={computed_val}  {ok}")

    print("\n" + "=" * 120)
    print("ALL CHECKS COMPLETE")
    print("=" * 120)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate Axis 3 tables and numbers.")
    parser.add_argument("--llm", action="store_true", help="Show Option 2 (LLM rewrites) only")
    parser.add_argument("--both", action="store_true", help="Show both Option 1 and Option 2")
    args = parser.parse_args()

    if args.both:
        _run_for_source("orig")
        _run_for_source("llm")
    elif args.llm:
        _run_for_source("llm")
    else:
        _run_for_source("orig")


if __name__ == "__main__":
    main()
