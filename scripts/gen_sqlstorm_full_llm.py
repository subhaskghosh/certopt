#!/usr/bin/env python3
"""Generate LLM rewrites for the exact queries in scripts/sqlstorm_full/{ds}.jsonl.

Reads sql1 from each JSONL record (the 11,112 queries from the full original
run) and asks the LLM to produce structurally different rewrites.  Output goes
to scripts/sqlstorm_full/{ds}_llm.jsonl.

Usage:
    python3 -m scripts.gen_sqlstorm_full_llm
    python3 -m scripts.gen_sqlstorm_full_llm --datasets tpch --dry-run
    python3 -m scripts.gen_sqlstorm_full_llm --datasets tpch tpcds --concurrency 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

# Reuse helpers from the existing gen script
from scripts.gen_sqlstorm_llm_rewrites import (
    _build_rewrite_prompt,
    _call_amp,
    _classify_complexity,
    _count_joins,
    _count_tables,
    _extract_sql_blocks,
    _is_structural_diff,
    _load_schema,
    _prefilter,
    _CTE_PAT,
    _SUBQUERY_PAT,
)

logger = logging.getLogger(__name__)

DATASETS = ["tpch", "tpcds", "stackoverflow", "job"]
FULL_DIR = Path("scripts/sqlstorm_full")


async def _generate_for_dataset(
    dataset: str,
    concurrency: int,
    dry_run: bool,
) -> None:
    """Generate LLM rewrites for one dataset from its full JSONL."""
    src_path = FULL_DIR / f"{dataset}.jsonl"
    if not src_path.exists():
        print(f"  WARNING: {src_path} not found, skipping {dataset}")
        return

    # Load source queries
    records: list[dict] = []
    with open(src_path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    schema = _load_schema(dataset)

    print(f"\n{'='*70}")
    print(f"  Dataset: {dataset}  (full LLM rewrite generation)")
    print(f"  Source:  {src_path}  ({len(records)} queries)")
    print(f"{'='*70}")

    if dry_run:
        rec = records[0]
        prompt = _build_rewrite_prompt(rec["sql1"], schema)
        print(f"\n--- DRY RUN: Prompt for pair_id={rec['pair_id']} ---")
        print(prompt[:500] + "\n...")
        print("--- END DRY RUN ---")
        return

    # Generate rewrites
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict | None] = [None] * len(records)
    stats = Counter()
    t_start = time.monotonic()

    async def _process(idx: int, rec: dict):
        sql_orig = rec["sql1"]
        pair_id = rec["pair_id"]

        # Pre-filter
        hit = _prefilter(sql_orig)
        if hit is not None:
            stats["prefilter"] += 1
            return

        if len(sql_orig.strip()) < 30:
            stats["too_short"] += 1
            return

        prompt = _build_rewrite_prompt(sql_orig, schema)

        async with semaphore:
            response = await _call_amp(prompt)

        if response is None:
            stats["fail"] += 1
            return

        blocks = _extract_sql_blocks(response)
        if not blocks:
            stats["fail"] += 1
            return

        sql_rewrite = blocks[0]

        # Pre-filter the rewrite
        hit = _prefilter(sql_rewrite)
        if hit is not None:
            stats["prefilter_rewrite"] += 1
            return

        if not _is_structural_diff(sql_orig, sql_rewrite):
            stats["trivial"] += 1
            return

        import re
        results[idx] = {
            "pair_id": str(pair_id),
            "sql1": sql_orig,
            "sql2": sql_rewrite,
            "edit_type": "llm_rewrite",
            "complexity": _classify_complexity(sql_orig),
            "n_tables": _count_tables(sql_orig),
            "n_joins": _count_joins(sql_orig),
            "has_cte": bool(_CTE_PAT.search(sql_orig)),
            "has_subquery": bool(_SUBQUERY_PAT.search(sql_orig)),
            "has_window": bool(re.search(r"\bOVER\s*\(", sql_orig, re.IGNORECASE)),
        }
        stats["success"] += 1

        done = sum(stats.values())
        if done % 100 == 0 or done == len(records):
            elapsed = time.monotonic() - t_start
            print(
                f"  [{done:5d}/{len(records)}] "
                f"{stats['success']} ok, {stats['trivial']} trivial, "
                f"{stats['fail']} fail, {stats['prefilter']} filtered  "
                f"[{elapsed:.0f}s]"
            )

    tasks = [_process(idx, rec) for idx, rec in enumerate(records)]
    await asyncio.gather(*tasks)

    # Collect results
    pairs = [r for r in results if r is not None]
    pairs.sort(key=lambda p: p["pair_id"])

    elapsed_total = time.monotonic() - t_start

    # Write output
    out_path = FULL_DIR / f"{dataset}_llm.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in pairs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\n  Results for {dataset}:")
    print(f"    Source queries:       {len(records)}")
    print(f"    Structural rewrites:  {stats['success']}")
    print(f"    Trivial (skipped):    {stats['trivial']}")
    print(f"    Failed (no response): {stats['fail']}")
    print(f"    Pre-filtered (orig):  {stats['prefilter']}")
    print(f"    Pre-filtered (rewr):  {stats['prefilter_rewrite']}")
    print(f"    Too short:            {stats['too_short']}")
    print(f"    Total pairs written:  {len(pairs)}")
    print(f"    Output:               {out_path}")
    print(f"    Time:                 {elapsed_total:.1f}s ({elapsed_total/60:.1f}min)")

    complexity_counts = Counter(p["complexity"] for p in pairs)
    print(f"\n  Complexity distribution:")
    for bucket in ("simple", "moderate", "complex"):
        print(f"    {bucket:<10s}: {complexity_counts.get(bucket, 0):>4d}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate LLM rewrites for full SQLStorm JSONL queries.",
    )
    parser.add_argument(
        "--datasets", nargs="+", default=DATASETS, choices=DATASETS,
    )
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    # Load .env
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

    if not os.environ.get("AMP_API_KEY"):
        print("ERROR: AMP_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    for ds in args.datasets:
        asyncio.run(_generate_for_dataset(ds, args.concurrency, args.dry_run))

    print(f"\nDone. Full LLM rewrites written to {FULL_DIR}/")


if __name__ == "__main__":
    main()
