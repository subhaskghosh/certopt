#!/usr/bin/env python3
"""Generate semantically-equivalent LLM rewrites of SQLStorm queries.

Uses the Amp SDK to ask an LLM to produce structurally different but
semantically equivalent rewrites of SQLStorm source queries.  The output
is a JSONL file per dataset suitable for ``run_eval.py --benchmark sqlstorm``.

Usage:
    # Single dataset
    python3 -m scripts.gen_sqlstorm_llm_rewrites --datasets tpch --max-queries 500

    # All datasets (default: 500 queries each, aiming for ~400 non-trivial)
    python3 -m scripts.gen_sqlstorm_llm_rewrites

    # Dry-run (show prompt for first query, no API calls)
    python3 -m scripts.gen_sqlstorm_llm_rewrites --datasets tpch --dry-run --max-queries 1

Requirements:
    pip install amp-sdk
    Set AMP_API_KEY in .env or environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATASETS = ["tpch", "tpcds", "stackoverflow", "job"]
DATA_ROOT = Path("data/SQLStorm/v1.0")
PROMPTS_DIR = Path("data/SQLStorm/prompts")
OUTPUT_DIR = Path("scripts/sqlstorm_sample")

# Pre-filter: skip queries with unsupported constructs
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

# Triviality detection (same as build_sqlstorm_sample.py)
_COMMENT_PAT = re.compile(r"--[^\n]*")
_DATE_LITERAL_PAT = re.compile(r"'[0-9]{4}-[0-9]{2}-[0-9]{2}[^']*'")
_CURRENT_FN_PAT = re.compile(
    r"\b(getdate\(\)|current_date|current_timestamp|current_time|now\(\))\b",
    re.IGNORECASE,
)
_CAST_SHORT_PAT = re.compile(r"::\w+(\([^)]*\))?")

# Complexity classification
_CTE_PAT = re.compile(r"\bWITH\b", re.IGNORECASE)
_SUBQUERY_PAT = re.compile(r"\(\s*SELECT\b", re.IGNORECASE)
_JOIN_PAT = re.compile(r"\bJOIN\b", re.IGNORECASE)
_TABLE_REF_PAT = re.compile(r"(?:\bFROM\b|\bJOIN\b)\s+(\w+)", re.IGNORECASE)

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
# Helpers
# ---------------------------------------------------------------------------

def _prefilter(sql: str) -> str | None:
    for name, pat in _PREFILTER_PATTERNS.items():
        if pat.search(sql):
            return name
    return None


def _normalize_for_diff(sql: str) -> str:
    s = _COMMENT_PAT.sub("", sql)
    s = _DATE_LITERAL_PAT.sub("'__DATE__'", s)
    s = _CURRENT_FN_PAT.sub("__CURRENT__", s)
    s = _CAST_SHORT_PAT.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _is_structural_diff(sql1: str, sql2: str) -> bool:
    if sql1.strip() == sql2.strip():
        return False
    return _normalize_for_diff(sql1) != _normalize_for_diff(sql2)


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
    nested_cte = bool(re.search(
        r"\bWITH\b.*\bAS\s*\(.*\bWITH\b", sql, re.IGNORECASE | re.DOTALL,
    ))
    if (has_cte and has_subquery) or n_joins >= 7 or nested_cte:
        return "complex"
    if has_cte or has_subquery or n_joins >= 4:
        return "moderate"
    return "simple"


def _extract_sql_blocks(text: str) -> list[str]:
    """Extract SQL code blocks from LLM output."""
    blocks = re.findall(r"```(?:sql)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if blocks:
        return [b.strip() for b in blocks if b.strip()]
    # Fallback: try to find SELECT statements
    selects = re.findall(
        r"(SELECT\s.*?;)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    return [s.strip() for s in selects if s.strip()]


def _load_schema(dataset: str) -> str:
    """Load the schema DDL from the prompts YAML."""
    yaml_path = PROMPTS_DIR / f"{dataset}.yaml"
    with open(yaml_path) as f:
        cfg = yaml.safe_load(f)
    return cfg["schema"]


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_REWRITE_SYSTEM = """\
You are an expert SQL query optimizer. Given an SQL query and database schema, \
produce ONE semantically equivalent rewrite that is structurally different from \
the original. The rewrite MUST return exactly the same result set for all \
possible database states.

Rules:
1. The rewrite must be SEMANTICALLY EQUIVALENT — same rows, same columns, same \
   values, same ordering (if ORDER BY present).
2. The rewrite must be STRUCTURALLY DIFFERENT — use different SQL constructs. \
   Examples of valid transformations:
   - Subquery ↔ JOIN conversion
   - CTE ↔ inline subquery
   - EXISTS ↔ IN ↔ JOIN
   - UNION ↔ OR in WHERE
   - Predicate pushdown/pullup
   - Join reordering
   - Aggregate refactoring (e.g., HAVING ↔ subquery filter)
   - CASE WHEN simplification
   - Redundant join elimination
3. Do NOT just rename aliases, reformat whitespace, or change comments.
4. Do NOT add DISTINCT unless the original has it.
5. Do NOT change the result column names/aliases.
6. Output ONLY the rewritten SQL in a single ```sql code block, no explanation.
"""


def _build_rewrite_prompt(sql: str, schema: str) -> str:
    return f"""{_REWRITE_SYSTEM}

### Database Schema
{schema}

### Original Query
```sql
{sql}
```

### Rewritten Query (semantically equivalent, structurally different)
"""


# ---------------------------------------------------------------------------
# Amp SDK caller
# ---------------------------------------------------------------------------

async def _call_amp(prompt: str, timeout_s: float = 90.0) -> str | None:
    """Call Amp SDK and return the text content, or None on failure."""
    from amp_sdk import AmpOptions, execute

    options = AmpOptions(
        mode="smart",
        visibility="private",
        labels=["sqlstorm-rewrite"],
    )

    content = ""
    try:
        async def _collect():
            nonlocal content
            async for msg in execute(prompt, options):
                if msg.type == "assistant":
                    for c in msg.message.content:
                        if hasattr(c, "text"):
                            content += c.text
                elif msg.type == "result":
                    if msg.is_error:
                        logger.error("Amp SDK error: %s", msg.error)
                        return False
                    content += msg.result or ""
                    return True
            return bool(content.strip())

        ok = await asyncio.wait_for(_collect(), timeout=timeout_s)
        if not ok:
            return None
    except asyncio.TimeoutError:
        logger.warning("Amp SDK timed out after %.0fs", timeout_s)
        return None
    except Exception as e:
        logger.warning("Amp SDK call failed: %s", e)
        return None

    return content


# ---------------------------------------------------------------------------
# Main generation loop
# ---------------------------------------------------------------------------

async def _generate_for_dataset(
    dataset: str,
    max_queries: int,
    seed: int,
    dry_run: bool,
    concurrency: int,
    output_dir: Path | None = None,
) -> None:
    """Generate LLM rewrites for one dataset."""
    queries_dir = DATA_ROOT / dataset / "queries"
    if not queries_dir.is_dir():
        # Fallback: try queries_generated
        queries_dir = DATA_ROOT / dataset / "queries_generated"
    if not queries_dir.is_dir():
        print(f"  WARNING: {queries_dir} does not exist, skipping {dataset}")
        return

    schema = _load_schema(dataset)

    print(f"\n{'='*70}")
    print(f"  Dataset: {dataset}  (LLM rewrite generation)")
    print(f"  Source:  {queries_dir}")
    print(f"{'='*70}")

    # Collect candidate query files
    sql_files = sorted(queries_dir.glob("*.sql"))
    candidates: list[tuple[int, Path]] = []
    skip_reasons: Counter[str] = Counter()

    for sql_path in sql_files:
        stem = sql_path.stem
        if not stem.isdigit():
            continue
        qid = int(stem)
        sql = sql_path.read_text(encoding="utf-8").strip()
        if not sql:
            continue
        # Pre-filter
        hit = _prefilter(sql)
        if hit is not None:
            skip_reasons[hit] += 1
            continue
        # Skip very short queries (likely trivial)
        if len(sql) < 30:
            skip_reasons["too_short"] += 1
            continue
        candidates.append((qid, sql_path))

    print(f"  Total .sql files:     {len(sql_files)}")
    print(f"  After pre-filter:     {len(candidates)}")
    if skip_reasons:
        for reason, cnt in skip_reasons.most_common():
            print(f"    skip: {reason:<22s} {cnt:>6d}")

    # Sample
    rng = random.Random(seed)
    if len(candidates) > max_queries:
        candidates = rng.sample(candidates, max_queries)
        candidates.sort(key=lambda x: x[0])
    print(f"  Sampled:              {len(candidates)}")

    if dry_run:
        # Show one prompt
        qid, path = candidates[0]
        sql = path.read_text(encoding="utf-8").strip()
        prompt = _build_rewrite_prompt(sql, schema)
        print(f"\n--- DRY RUN: Prompt for qid={qid} ---")
        print(prompt)
        print("--- END DRY RUN ---")
        return

    # Generate rewrites with bounded concurrency
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict | None] = [None] * len(candidates)
    n_success = 0
    n_trivial = 0
    n_fail = 0
    n_prefilter_out = 0
    t_start = time.monotonic()

    async def _process(idx: int, qid: int, sql_path: Path):
        nonlocal n_success, n_trivial, n_fail, n_prefilter_out
        sql_orig = sql_path.read_text(encoding="utf-8").strip()
        prompt = _build_rewrite_prompt(sql_orig, schema)

        async with semaphore:
            response = await _call_amp(prompt)

        if response is None:
            n_fail += 1
            return

        blocks = _extract_sql_blocks(response)
        if not blocks:
            n_fail += 1
            return

        sql_rewrite = blocks[0]

        # Pre-filter the rewrite too
        hit = _prefilter(sql_rewrite)
        if hit is not None:
            n_prefilter_out += 1
            return

        # Check structural diff
        if not _is_structural_diff(sql_orig, sql_rewrite):
            n_trivial += 1
            return

        tier = _prompt_tier(qid)
        complexity = _classify_complexity(sql_orig)
        n_joins = _count_joins(sql_orig)
        n_tables = _count_tables(sql_orig)
        has_cte = bool(_CTE_PAT.search(sql_orig))
        has_subquery = bool(_SUBQUERY_PAT.search(sql_orig))
        has_window = bool(re.search(r"\bOVER\s*\(", sql_orig, re.IGNORECASE))

        results[idx] = {
            "pair_id": str(qid),
            "sql1": sql_orig,
            "sql2": sql_rewrite,
            "edit_type": "llm_rewrite",
            "prompt_tier": tier,
            "complexity": complexity,
            "n_tables": n_tables,
            "n_joins": n_joins,
            "has_cte": has_cte,
            "has_subquery": has_subquery,
            "has_window": has_window,
        }
        n_success += 1

        done = n_success + n_trivial + n_fail + n_prefilter_out
        elapsed = time.monotonic() - t_start
        print(
            f"  [{done:4d}/{len(candidates)}] qid={qid:>6d}: "
            f"structural ✓  ({n_success} ok, {n_trivial} trivial, {n_fail} fail)  "
            f"[{elapsed:.0f}s]"
        )

    tasks = []
    for idx, (qid, path) in enumerate(candidates):
        tasks.append(_process(idx, qid, path))

    await asyncio.gather(*tasks)

    # Collect non-None results
    pairs = [r for r in results if r is not None]
    pairs.sort(key=lambda p: int(p["pair_id"]))

    elapsed_total = time.monotonic() - t_start

    # Write output
    out_dir = output_dir or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{dataset}_llm.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in pairs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\n  Results:")
    print(f"    Structural rewrites:  {n_success}")
    print(f"    Trivial (skipped):    {n_trivial}")
    print(f"    Failed (no response): {n_fail}")
    print(f"    Pre-filtered out:     {n_prefilter_out}")
    print(f"    Total pairs written:  {len(pairs)}")
    print(f"    Output:               {out_path}")
    print(f"    Time:                 {elapsed_total:.1f}s ({elapsed_total/60:.1f}min)")

    # Complexity distribution
    complexity_counts = Counter(p["complexity"] for p in pairs)
    print(f"\n  Complexity distribution:")
    for bucket in ("simple", "moderate", "complex"):
        print(f"    {bucket:<10s}: {complexity_counts.get(bucket, 0):>4d}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate LLM rewrites of SQLStorm queries via Amp SDK.",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        default=DATASETS,
        choices=DATASETS,
        help="Datasets to process (default: all four)",
    )
    parser.add_argument(
        "--max-queries",
        type=int,
        default=500,
        help="Max source queries to process per dataset (default: 500)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed (default: 42)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show prompt for first query, no API calls",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Max concurrent Amp SDK calls (default: 5)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Override output directory (default: scripts/sqlstorm_sample)",
    )

    args = parser.parse_args(argv)

    # Load .env if present
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

    if not os.environ.get("AMP_API_KEY"):
        print("ERROR: AMP_API_KEY not set. Set in .env or environment.", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output_dir) if args.output_dir else None
    for ds in args.datasets:
        asyncio.run(_generate_for_dataset(
            ds,
            max_queries=args.max_queries,
            seed=args.seed,
            dry_run=args.dry_run,
            concurrency=args.concurrency,
            output_dir=out_dir,
        ))

    print(f"\nDone. LLM rewrite samples written to {out_dir or OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
