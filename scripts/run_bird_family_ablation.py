"""Family pruning ablation on BIRD LLM candidates.

Compares solver invocations with and without family pruning:
  - WITHOUT pruning: every candidate is verified individually against candidate[0].
  - WITH pruning: when one candidate in a family returns SAT, all remaining
    candidates in that family are pruned without calling the solver.

Usage:
    python3 -m scripts.run_bird_family_ablation [--max-queries N] [--k-rows K]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

from src.optim.cegis.witness_synthesis import (
    WitnessResult,
    synthesize_witness,
)
from src.optim.ir.types import SemType
from src.optim.parser.sql_to_ir import sql_to_ir
from src.optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from src.optim.verify.encode_z3 import BoundedScope

logger = logging.getLogger(__name__)

DATA_PATH = Path("scripts/bird_llm_candidates/candidates_with_schema.jsonl")
OUT_DIR = Path("results/abl_bird_family_pruning")

# Map BIRD uppercase type strings to SemType
_TYPE_MAP: dict[str, SemType] = {
    "INTEGER": SemType.INT,
    "INT": SemType.INT,
    "BIGINT": SemType.INT,
    "SMALLINT": SemType.INT,
    "TINYINT": SemType.INT,
    "TEXT": SemType.STRING,
    "VARCHAR": SemType.STRING,
    "CHAR": SemType.STRING,
    "REAL": SemType.FLOAT,
    "FLOAT": SemType.FLOAT,
    "DOUBLE": SemType.FLOAT,
    "NUMERIC": SemType.DECIMAL,
    "DECIMAL": SemType.DECIMAL,
    "DATE": SemType.DATE,
    "DATETIME": SemType.TIMESTAMP,
    "TIMESTAMP": SemType.TIMESTAMP,
    "BOOLEAN": SemType.BOOL,
    "BLOB": SemType.STRING,
}


def _build_catalog(schema: dict) -> Catalog:
    """Build a Catalog from the BIRD schema dict."""
    tables: dict[str, TableInfo] = {}
    for tname, cols_raw in schema["tables"].items():
        columns = []
        pks = []
        for col in cols_raw:
            sem_type = _TYPE_MAP.get(col["type"].upper(), SemType.UNKNOWN)
            columns.append(ColumnInfo(
                name=col["name"],
                sem_type=sem_type,
                nullable=col.get("nullable", True),
                is_primary_key=col.get("is_pk", False),
            ))
            if col.get("is_pk", False):
                pks.append(col["name"])
        tables[tname] = TableInfo(name=tname, columns=columns, primary_keys=pks)

    fks = []
    for fk in schema.get("foreign_keys", []):
        if not all(fk.get(k) for k in ("src_table", "src_column", "dst_table", "dst_column")):
            continue
        fks.append(ForeignKey(
            src_table=fk["src_table"],
            src_column=fk["src_column"],
            dst_table=fk["dst_table"],
            dst_column=fk["dst_column"],
        ))
    return Catalog(tables=tables, foreign_keys=fks)


def _run_one_query(
    query_entry: dict,
    scope: BoundedScope,
    enable_pruning: bool,
) -> dict:
    """Verify all candidates for one query. Returns per-query stats."""
    candidates = query_entry["candidates"]
    schema = query_entry["schema"]
    query_id = query_entry["query_id"]

    if len(candidates) < 2:
        return {
            "query_id": query_id,
            "n_candidates": len(candidates),
            "solver_calls": 0,
            "pruned": 0,
            "sat": 0,
            "unsat": 0,
            "unknown": 0,
            "parse_errors": 0,
            "time_ms": 0.0,
        }

    catalog = _build_catalog(schema)

    # Parse the spec (candidate[0])
    spec_sql = candidates[0]["sql"]
    spec_ir, spec_err = sql_to_ir(spec_sql, dialect="sqlite", catalog=catalog)
    if spec_ir is None:
        return {
            "query_id": query_id,
            "n_candidates": len(candidates),
            "solver_calls": 0,
            "pruned": 0,
            "sat": 0,
            "unsat": 0,
            "unknown": 0,
            "parse_errors": len(candidates) - 1,
            "time_ms": 0.0,
            "spec_parse_error": spec_err,
        }

    # Group non-spec candidates by source family
    families: dict[str, list[dict]] = defaultdict(list)
    for c in candidates[1:]:
        families[c["source"]].append(c)

    stats = {
        "query_id": query_id,
        "n_candidates": len(candidates) - 1,
        "solver_calls": 0,
        "pruned": 0,
        "sat": 0,
        "unsat": 0,
        "unknown": 0,
        "parse_errors": 0,
        "time_ms": 0.0,
    }

    # Track which families have been rejected (SAT found)
    rejected_families: set[str] = set()

    for family_name, members in families.items():
        for c in members:
            # Family pruning: skip if this family already rejected
            if enable_pruning and family_name in rejected_families:
                stats["pruned"] += 1
                continue

            # Parse candidate
            cand_ir, cand_err = sql_to_ir(c["sql"], dialect="sqlite", catalog=catalog)
            if cand_ir is None:
                stats["parse_errors"] += 1
                continue

            # Verify
            t0 = time.monotonic()
            try:
                result: WitnessResult = synthesize_witness(
                    spec_ir, cand_ir, catalog, scope=scope,
                )
            except Exception as e:
                logger.warning("Query %s candidate %s: solver error: %s",
                               query_id, c["id"], e)
                stats["unknown"] += 1
                stats["solver_calls"] += 1
                stats["time_ms"] += (time.monotonic() - t0) * 1000
                continue

            elapsed = (time.monotonic() - t0) * 1000
            stats["solver_calls"] += 1
            stats["time_ms"] += elapsed

            if result.status == "sat":
                stats["sat"] += 1
                if enable_pruning:
                    rejected_families.add(family_name)
            elif result.status == "unsat":
                stats["unsat"] += 1
            else:
                stats["unknown"] += 1

    return stats


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="BIRD family pruning ablation")
    parser.add_argument("--max-queries", type=int, default=None)
    parser.add_argument("--k-rows", type=int, default=2)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    # Load dataset
    entries: list[dict] = []
    with open(DATA_PATH) as f:
        for line in f:
            entries.append(json.loads(line))
    if args.max_queries:
        entries = entries[:args.max_queries]

    logger.info("Loaded %d queries from %s", len(entries), DATA_PATH)

    scope = BoundedScope(k_rows=args.k_rows, solver_timeout_ms=args.timeout_ms)

    # Run both conditions
    all_results: dict[str, list[dict]] = {}
    for condition in ("no_pruning", "with_pruning"):
        enable = condition == "with_pruning"
        logger.info("=== Running condition: %s ===", condition)
        results = []
        t_start = time.monotonic()
        for i, entry in enumerate(entries):
            if (i + 1) % 50 == 0 or i == 0:
                logger.info("  [%s] query %d/%d (%s)",
                            condition, i + 1, len(entries), entry["query_id"])
            r = _run_one_query(entry, scope, enable_pruning=enable)
            results.append(r)
        wall_time = time.monotonic() - t_start
        all_results[condition] = results

        # Aggregate
        total_solver = sum(r["solver_calls"] for r in results)
        total_pruned = sum(r["pruned"] for r in results)
        total_sat = sum(r["sat"] for r in results)
        total_unsat = sum(r["unsat"] for r in results)
        total_unknown = sum(r["unknown"] for r in results)
        total_parse_err = sum(r["parse_errors"] for r in results)
        total_time = sum(r["time_ms"] for r in results)

        logger.info("  %s: solver_calls=%d, pruned=%d, sat=%d, unsat=%d, "
                     "unknown=%d, parse_errors=%d, solver_time=%.1fs, wall_time=%.1fs",
                     condition, total_solver, total_pruned, total_sat, total_unsat,
                     total_unknown, total_parse_err, total_time / 1000, wall_time)

    # Build summary
    def _agg(results: list[dict]) -> dict:
        return {
            "solver_calls": sum(r["solver_calls"] for r in results),
            "pruned": sum(r["pruned"] for r in results),
            "sat": sum(r["sat"] for r in results),
            "unsat": sum(r["unsat"] for r in results),
            "unknown": sum(r["unknown"] for r in results),
            "parse_errors": sum(r["parse_errors"] for r in results),
            "solver_time_ms": round(sum(r["time_ms"] for r in results), 1),
            "n_candidates": sum(r["n_candidates"] for r in results),
        }

    no_prune = _agg(all_results["no_pruning"])
    with_prune = _agg(all_results["with_pruning"])

    saved_calls = no_prune["solver_calls"] - with_prune["solver_calls"]
    pct_saved = (saved_calls / no_prune["solver_calls"] * 100) if no_prune["solver_calls"] > 0 else 0
    time_saved_ms = no_prune["solver_time_ms"] - with_prune["solver_time_ms"]

    summary = {
        "benchmark": "BIRD-LLM-Candidates",
        "n_queries": len(entries),
        "n_candidates_total": no_prune["n_candidates"],
        "k_rows": args.k_rows,
        "timeout_ms": args.timeout_ms,
        "no_pruning": no_prune,
        "with_pruning": with_prune,
        "savings": {
            "solver_calls_saved": saved_calls,
            "pct_solver_calls_saved": round(pct_saved, 1),
            "time_saved_ms": round(time_saved_ms, 1),
            "candidates_pruned": with_prune["pruned"],
        },
    }

    # Save results
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    with open(OUT_DIR / "per_query_no_pruning.json", "w") as f:
        json.dump(all_results["no_pruning"], f, indent=2)
    with open(OUT_DIR / "per_query_with_pruning.json", "w") as f:
        json.dump(all_results["with_pruning"], f, indent=2)

    # Generate report
    report = f"""# BIRD Family Pruning Ablation

## Dataset
| Metric | Value |
|---|---|
| Queries | {len(entries)} |
| Total candidates | {no_prune['n_candidates']} |
| Mean candidates/query | {no_prune['n_candidates'] / len(entries):.1f} |
| k_rows | {args.k_rows} |
| timeout_ms | {args.timeout_ms} |

## Results

| Metric | No Pruning | With Pruning | Savings |
|---|---|---|---|
| Solver calls | {no_prune['solver_calls']} | {with_prune['solver_calls']} | {saved_calls} ({pct_saved:.1f}%) |
| Candidates pruned | 0 | {with_prune['pruned']} | — |
| SAT (rejected) | {no_prune['sat']} | {with_prune['sat']} | — |
| UNSAT (equivalent) | {no_prune['unsat']} | {with_prune['unsat']} | — |
| Unknown/timeout | {no_prune['unknown']} | {with_prune['unknown']} | — |
| Parse errors | {no_prune['parse_errors']} | {with_prune['parse_errors']} | — |
| Solver time | {no_prune['solver_time_ms'] / 1000:.1f}s | {with_prune['solver_time_ms'] / 1000:.1f}s | {time_saved_ms / 1000:.1f}s |

## Conclusion

Family pruning reduced solver calls by **{pct_saved:.1f}%** ({saved_calls} calls saved),
pruning **{with_prune['pruned']}** candidates without invoking the solver.
"""

    with open(OUT_DIR / "report.md", "w") as f:
        f.write(report)

    # Print summary to stdout
    print("\n" + "=" * 60)
    print("BIRD Family Pruning Ablation Results")
    print("=" * 60)
    print(f"Queries: {len(entries)}, Candidates: {no_prune['n_candidates']}")
    print(f"\nNo pruning:   {no_prune['solver_calls']} solver calls, "
          f"{no_prune['solver_time_ms'] / 1000:.1f}s")
    print(f"With pruning: {with_prune['solver_calls']} solver calls, "
          f"{with_prune['solver_time_ms'] / 1000:.1f}s, "
          f"{with_prune['pruned']} pruned")
    print(f"\nSavings: {saved_calls} calls ({pct_saved:.1f}%), "
          f"{time_saved_ms / 1000:.1f}s time saved")
    print("=" * 60)


if __name__ == "__main__":
    main()
