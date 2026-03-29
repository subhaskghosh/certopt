"""Unified evaluation runner for Proof-Carrying Query Optimization.

Supports VeriEQL equivalence-checking benchmarks (Calcite, LeetCode, Literature)
and JOB-Complex query optimization benchmarks via a single entry point.

Usage:
    # VeriEQL benchmarks
    python3 -m scripts.run_eval --benchmark verieql --suite calcite
    python3 -m scripts.run_eval --benchmark verieql --suite all --validate
    python3 -m scripts.run_eval --benchmark verieql --suite literature --verbose
    python3 -m scripts.run_eval --benchmark verieql --suite calcite --max-pairs 20 --k-rows 3
    python3 -m scripts.run_eval --benchmark verieql --config configs/verieql_calcite.json

    # JOB-Complex benchmark
    python3 -m scripts.run_eval --benchmark job-complex --data-dir data/JOB-Complex
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import statistics
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VeriEQL benchmark files
# ---------------------------------------------------------------------------

VERIEQL_SUITE_FILES = {
    "calcite": "data/VeriEQL/benchmarks/calcite/calcite2.jsonlines",
    "literature": "data/VeriEQL/benchmarks/literature/literature.jsonlines",
    "leetcode": "data/VeriEQL/benchmarks/leetcode/leetcode.jsonlines",
}

VERIEQL_RESULT_FILES = {
    "calcite": "data/VeriEQL/experiments/2025_10_31/calcite.out",
    "literature": "data/VeriEQL/experiments/2025_10_31/literature.out",
    "leetcode": "data/VeriEQL/experiments/2025_10_31/leetcode.out",
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None):
    p = argparse.ArgumentParser(
        prog="run_eval",
        description="Unified evaluation runner for Proof-Carrying Query Optimization",
    )

    # Benchmark selection
    p.add_argument("--benchmark",
                   choices=["verieql", "job-complex", "sqlstorm"],
                   help="Benchmark to run (required unless --config provides it)")

    # VeriEQL-specific
    p.add_argument("--suite",
                   choices=["calcite", "literature", "leetcode", "all"],
                   default="calcite",
                   help="VeriEQL suite (default: calcite)")
    p.add_argument("--max-pairs", type=int, default=None,
                   help="Limit number of pairs (verieql) or queries (job-complex)")
    p.add_argument("--random-sample", action="store_true",
                   help="Randomly sample --max-pairs entries instead of taking first N")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed for --random-sample (default: 42)")
    p.add_argument("--pair-indices", type=str, default=None,
                   help="Comma-separated pair indices to run (e.g., '7,44,103')")
    p.add_argument("--validate", action="store_true",
                   help="Validate SAT witnesses via DuckDB+SQLite")
    p.add_argument("--ignore-constraints", action="store_true",
                   help="Strip PK/FK/NOT-NULL constraints (ablation study)")

    # SQLStorm-specific
    p.add_argument("--dataset",
                   choices=["stackoverflow", "tpcds", "tpch", "job"],
                   default="stackoverflow",
                   help="SQLStorm dataset (default: stackoverflow)")
    p.add_argument("--sqlstorm-source",
                   choices=["orig", "llm"],
                   default="orig",
                   help="SQLStorm sample source: orig (orig_vs_rewritten) or llm (LLM rewrites)")
    p.add_argument("--sample-dir",
                   default=None,
                   help="Directory containing SQLStorm sample JSONL files (default: scripts/sqlstorm_sample)")

    # JOB-Complex-specific
    p.add_argument("--data-dir", default="data/JOB-Complex",
                   help="Path to JOB-Complex directory")
    p.add_argument("--enable-llm", action="store_true",
                   help="Enable LLM rewrite candidates")
    p.add_argument("--llm-mode", choices=["smart", "deep"], default="smart")
    p.add_argument("--llm-n-candidates", type=int, default=5)
    p.add_argument("--no-preprocessing", action="store_true")
    p.add_argument("--no-family-pruning", action="store_true")
    p.add_argument("--no-verification", action="store_true")
    p.add_argument("--enable-compositional", action="store_true")
    p.add_argument("--at-most-k", action="store_true",
                   help="Use SpotIt-style at-most-K semantics (dense k=1..K schedule)")
    p.add_argument("--ablation", type=str, default=None)

    # Shared
    p.add_argument("--k-rows", type=int, default=2,
                   help="k_rows for BoundedScope (default: 2)")
    p.add_argument("--timeout-ms", type=int, default=30000,
                   help="Solver timeout in ms (default: 30000)")
    p.add_argument("--dialect", default=None,
                   help="SQL dialect (default: sqlite for verieql, postgres for job-complex)")
    p.add_argument("--output", default=None,
                   help="Output directory (default: auto-generated)")
    p.add_argument("--checkpoint-every", type=int, default=100,
                   help="Save intermediate results every N pairs")
    p.add_argument("--verbose", action="store_true",
                   help="Enable DEBUG logging to stderr")
    p.add_argument("--config", type=str, default=None,
                   help="Load run config from JSON file (overrides CLI args)")

    return p.parse_args(argv)


def load_config_file(config_path: str, args):
    """Load a JSON config file and override args with its values."""
    with open(config_path) as f:
        cfg = json.load(f)

    direct_map = {
        "benchmark": "benchmark",
        "suite": "suite",
        "dataset": "dataset",
        "k_rows": "k_rows",
        "timeout_ms": "timeout_ms",
        "max_pairs": "max_pairs",
        "dialect": "dialect",
        "data_dir": "data_dir",
        "output": "output",
        "checkpoint_every": "checkpoint_every",
        "seed": "seed",
        "pair_indices": "pair_indices",
        "llm_mode": "llm_mode",
        "llm_n_candidates": "llm_n_candidates",
        "ablation": "ablation",
        "sqlstorm_source": "sqlstorm_source",
    }

    bool_map = {
        "validate": "validate",
        "verbose": "verbose",
        "enable_llm": "enable_llm",
        "enable_compositional": "enable_compositional",
        "at_most_k": "at_most_k",
        "random_sample": "random_sample",
        "no_preprocessing": "no_preprocessing",
        "no_family_pruning": "no_family_pruning",
        "no_verification": "no_verification",
        "ignore_constraints": "ignore_constraints",
    }

    nullable_keys = {"max_pairs", "output", "dialect", "pair_indices", "ablation"}

    for json_key, val in cfg.items():
        if json_key.startswith("_"):
            continue
        if val is None and json_key in nullable_keys:
            continue
        if json_key in direct_map:
            setattr(args, direct_map[json_key], val)
        elif json_key in bool_map:
            setattr(args, bool_map[json_key], val)


# ---------------------------------------------------------------------------
# Auto output directory
# ---------------------------------------------------------------------------

def _auto_output_dir(args) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    if args.benchmark == "verieql":
        suite = args.suite if args.suite != "all" else "all"
        parts = [f"verieql_{suite}", ts, f"k{args.k_rows}"]
        if args.validate:
            parts.append("validated")
        return f"results/{'_'.join(parts)}"
    else:
        parts = [ts, f"k{args.k_rows}", args.dialect or "postgres"]
        return f"results/{'_'.join(parts)}"


# ---------------------------------------------------------------------------
# VeriEQL benchmark runner
# ---------------------------------------------------------------------------

def _map_witness_status(status: str) -> str:
    return {
        "unsat": "EQU",
        "sat": "NEQ",
        "unknown": "UNKNOWN",
        "timeout": "TMO",
    }.get(status, "UNKNOWN")


def _classify(our: str, theirs: str) -> str:
    if our == "PARSE_FAIL":
        return "PARSE_FAIL"
    if our == "ERROR":
        return "ERROR"
    if theirs == "MISSING":
        return "NO_VQ_DATA"
    if theirs in ("NSE", "SYN", "ERR", "NIE"):
        if our in ("EQU", "NEQ"):
            return "OUR_STRONGER"
        return "BOTH_SKIP"
    if our in ("UNKNOWN", "TMO") and theirs in ("EQU", "NEQ"):
        return "OUR_WEAKER"
    if theirs in ("UNKNOWN", "TMO") and our in ("EQU", "NEQ"):
        return "OUR_STRONGER"
    if our == theirs:
        return "AGREE"
    if our in ("UNKNOWN", "TMO") and theirs in ("UNKNOWN", "TMO"):
        return "BOTH_UNDECIDED"
    return "DISAGREE"


def _novelty_label(our: str, vq: str) -> str | None:
    """Return novelty claim string if we decided where VeriEQL couldn't."""
    if our not in ("EQU", "NEQ"):
        return None
    if vq == "NSE":
        return "our_stronger_nse"
    if vq in ("ERR", "NIE"):
        return "our_stronger_err"
    if vq == "TMO":
        return "our_stronger_tmo"
    return None


def _run_verieql_suite(suite_name: str, args) -> tuple[list[dict], list[dict], dict]:
    """Run VeriEQL benchmark for one suite.

    Returns (results_list, traces_list, summary_dict).
    """
    from optim.eval.verieql_loader import (
        load_verieql_entry,
        load_verieql_results,
        load_verieql_suite,
        verieql_verdict,
    )
    from optim.parser.sql_to_ir import sql_to_ir
    from optim.cegis.witness_synthesis import synthesize_witness_adaptive
    from optim.verify.encode_z3 import BoundedScope

    dialect = args.dialect or "sqlite"
    scope = BoundedScope(k_rows=args.k_rows, solver_timeout_ms=args.timeout_ms)
    entries = load_verieql_suite(VERIEQL_SUITE_FILES[suite_name])
    verieql_results = load_verieql_results(VERIEQL_RESULT_FILES[suite_name])

    # Tag each entry with its original line position for VQ lookup
    for _pos, _e in enumerate(entries):
        _e["_line_pos"] = _pos

    if args.pair_indices is not None:
        target_indices = set(int(x.strip()) for x in args.pair_indices.split(","))
        # First apply random-sample reordering (if --seed 42 was used for baseline),
        # then filter by the pair_index within that reordered list.
        if args.random_sample and args.max_pairs:
            import random
            rng = random.Random(args.seed)
            entries = rng.sample(entries, min(args.max_pairs, len(entries)))
        entries = [(i, e) for i, e in enumerate(entries) if i in target_indices]
        entries = [e for _, e in entries]
    elif args.max_pairs is not None:
        if args.random_sample:
            import random
            rng = random.Random(args.seed)
            entries = rng.sample(entries, min(args.max_pairs, len(entries)))
        else:
            entries = entries[: args.max_pairs]

    results: list[dict] = []
    traces: list[dict] = []

    # Counters
    n_equ = n_neq = n_unknown = n_tmo = n_parse_fail = 0
    n_agree = n_disagree = n_our_stronger = n_our_weaker = 0
    n_error = 0
    n_both_skip = n_both_undecided = n_no_vq_data = 0
    n_nse_handled = n_err_handled = n_tmo_handled = 0
    all_times: list[float] = []

    print(f"\n{'='*70}")
    print(f"  VeriEQL Benchmark: {suite_name} ({len(entries)} pairs)")
    print(f"  k_rows={args.k_rows}  timeout_ms={args.timeout_ms}  validate={args.validate}")
    print(f"{'='*70}\n")

    t_start = time.monotonic()

    for i, raw_entry in enumerate(entries):
        entry: dict = {
            "pair_index": i,
            "suite": suite_name,
            "parse_ok": False,
            "our_result": None,
            "verieql_result": None,
            "comparison": None,
            "time_ms": 0.0,
            "error": None,
            "sql1": None,
            "sql2": None,
            "k_rows_used": args.k_rows,
            "validation_status": None,
            "witness_db": None,
            "novelty": None,
        }
        trace: dict = {
            "pair_index": i,
            "suite": suite_name,
            "sql1": None,
            "sql2": None,
            "schema": raw_entry.get("schema"),
            "constraints": raw_entry.get("constraint"),
            "our_result": None,
            "verieql_result": None,
            "comparison": None,
            "time_ms": 0.0,
            "k_rows_used": args.k_rows,
            "adaptive_schedule": list(range(1, args.k_rows + 1)) if getattr(args, 'at_most_k', False) else [2, 4, 8],
            "validation_result": None,
            "witness_db": None,
            "error": None,
            "novelty": None,
        }

        try:
            if getattr(args, 'ignore_constraints', False):
                raw_entry["constraint"] = []
            catalog, sql1, sql2 = load_verieql_entry(raw_entry)
            entry["sql1"] = sql1
            entry["sql2"] = sql2
            trace["sql1"] = sql1
            trace["sql2"] = sql2

            # Parse
            t0 = time.monotonic()
            ir1, err1 = sql_to_ir(sql1, dialect=dialect, catalog=catalog)
            ir2, err2 = sql_to_ir(sql2, dialect=dialect, catalog=catalog)

            if ir1 is None or ir2 is None:
                parse_err = err1 or err2 or "unknown parse error"
                entry["our_result"] = "PARSE_FAIL"
                entry["error"] = parse_err
                entry["time_ms"] = round((time.monotonic() - t0) * 1000, 1)
                trace["our_result"] = "PARSE_FAIL"
                trace["error"] = parse_err
                trace["time_ms"] = entry["time_ms"]
                n_parse_fail += 1
                logger.debug("PARSE_FAIL [%d]: %s", i, parse_err)
            else:
                entry["parse_ok"] = True

                witness_result = synthesize_witness_adaptive(
                    ir1, ir2, catalog, scope,
                    validate_witnesses=args.validate,
                    original_sql=(sql1, sql2),
                    at_most_k=getattr(args, 'at_most_k', False),
                    normalize_column_order=False,  # column order matters for formal equivalence
                )
                entry["time_ms"] = round((time.monotonic() - t0) * 1000, 1)

                # FIX.28b: incomplete UNSAT (lower-k only) → UNKNOWN
                if witness_result.status == "unsat" and not witness_result.complete:
                    entry["our_result"] = "UNKNOWN"
                else:
                    entry["our_result"] = _map_witness_status(witness_result.status)
                entry["proven_k"] = witness_result.proven_k
                entry["complete"] = witness_result.complete
                trace["proven_k"] = witness_result.proven_k
                trace["complete"] = witness_result.complete

                if witness_result.witness_db is not None:
                    entry["witness_db"] = witness_result.witness_db
                    trace["witness_db"] = witness_result.witness_db

                if entry["our_result"] == "EQU":
                    n_equ += 1
                    entry["validation_status"] = "proved_equivalent"
                elif entry["our_result"] == "NEQ":
                    effective_validate = args.validate or getattr(args, 'at_most_k', False)
                    has_witness = witness_result.witness_db is not None
                    if effective_validate and has_witness:
                        n_neq += 1
                        entry["validation_status"] = "confirmed"
                    elif effective_validate and not has_witness:
                        # SAT but no extractable witness — downgrade to UNKNOWN
                        entry["our_result"] = "UNKNOWN"
                        n_unknown += 1
                        entry["validation_status"] = "no_witness_downgraded"
                    elif has_witness:
                        n_neq += 1
                        entry["validation_status"] = "unvalidated"
                    else:
                        n_neq += 1
                        entry["validation_status"] = "no_witness"
                elif entry["our_result"] == "UNKNOWN":
                    n_unknown += 1
                    effective_validate = args.validate or getattr(args, 'at_most_k', False)
                    entry["validation_status"] = "spurious_downgraded" if effective_validate else None
                elif entry["our_result"] == "TMO":
                    n_tmo += 1
                elif entry["our_result"] == "ERROR":
                    n_error += 1

                trace["our_result"] = entry["our_result"]
                trace["time_ms"] = entry["time_ms"]
                trace["validation_result"] = entry["validation_status"]

            # VeriEQL verdict — keyed by original line position in suite
            vq_entry = verieql_results.get(raw_entry.get("_line_pos", i), {})
            vq_result = verieql_verdict(vq_entry) if vq_entry else "MISSING"
            entry["verieql_result"] = vq_result
            trace["verieql_result"] = vq_result

            # Classify
            entry["comparison"] = _classify(entry["our_result"], vq_result)
            trace["comparison"] = entry["comparison"]

            if entry["comparison"] == "AGREE":
                n_agree += 1
            elif entry["comparison"] == "DISAGREE":
                n_disagree += 1
            elif entry["comparison"] == "OUR_STRONGER":
                n_our_stronger += 1
                if vq_result == "NSE":
                    n_nse_handled += 1
                elif vq_result in ("ERR", "NIE"):
                    n_err_handled += 1
                elif vq_result == "TMO":
                    n_tmo_handled += 1
            elif entry["comparison"] == "OUR_WEAKER":
                n_our_weaker += 1
            elif entry["comparison"] == "NO_VQ_DATA":
                n_no_vq_data += 1
            elif entry["comparison"] == "BOTH_SKIP":
                n_both_skip += 1
            elif entry["comparison"] == "BOTH_UNDECIDED":
                n_both_undecided += 1
            elif entry["comparison"] == "PARSE_FAIL":
                pass  # already counted
            elif entry["comparison"] == "ERROR":
                pass  # already counted

            # Novelty
            novelty = _novelty_label(entry["our_result"], vq_result)
            entry["novelty"] = novelty
            trace["novelty"] = novelty

        except Exception as exc:
            entry["our_result"] = entry.get("our_result") or "ERROR"
            entry["error"] = str(exc)
            if entry["time_ms"] == 0.0:
                entry["time_ms"] = round((time.monotonic() - t_start) * 1000, 1)
            trace["our_result"] = entry["our_result"]
            trace["error"] = str(exc)
            trace["time_ms"] = entry["time_ms"]
            logger.debug("ERROR [%d]: %s", i, exc, exc_info=True)

        all_times.append(entry["time_ms"])
        results.append(entry)
        traces.append(trace)

        # Progress
        done = i + 1
        elapsed = time.monotonic() - t_start
        eta = (len(entries) - done) / (done / elapsed) if elapsed > 0 else 0
        sym = {"EQU": "≡", "NEQ": "≠", "TMO": "⏱", "UNKNOWN": "?",
               "PARSE_FAIL": "✗", "ERROR": "!"}.get(entry["our_result"], "·")
        vs_label = f"vs {entry['verieql_result'] or '?'}"
        cmp_label = entry["comparison"] or ""
        print(
            f"  [{done:5d}/{len(entries)}] {sym} pair {entry['pair_index']:5d}: "
            f"{entry['our_result']:10s} {vs_label:8s} → {cmp_label:14s} "
            f"[{entry['time_ms']:.0f}ms] "
            f"agree={n_agree}/{done} ({n_agree/done*100:.0f}%) "
            f"[~{eta:.0f}s rem]"
        )

        # Structured PROGRESS log line (for check_progress.py)
        logger.info(
            "PROGRESS [%d/%d] pair_index=%d: status=%s vs=%s comparison=%s "
            "time_ms=%.1f agree=%d/%d (%d%%)",
            done, len(entries), i, entry["our_result"], entry["verieql_result"],
            entry["comparison"], entry["time_ms"],
            n_agree, done, n_agree / done * 100,
        )

        # Checkpoint + memory cleanup
        if args.checkpoint_every and done % args.checkpoint_every == 0:
            logger.info(
                "CHECKPOINT %d/%d: EQU=%d NEQ=%d UNKNOWN=%d agree=%d/%d (%.0f%%)",
                done, len(entries), n_equ, n_neq, n_unknown,
                n_agree, done, n_agree / done * 100,
            )
            # Release Z3 internal caches to prevent OOM on large runs
            import gc
            gc.collect()
            try:
                import z3
                z3._main_ctx = None
                z3.main_ctx()
            except Exception:
                pass

    t_total = time.monotonic() - t_start

    # Timing stats
    sorted_times = sorted(all_times)
    mean_time = statistics.mean(sorted_times) if sorted_times else 0.0
    median_time = statistics.median(sorted_times) if sorted_times else 0.0
    p95_idx = min(int(len(sorted_times) * 0.95), len(sorted_times) - 1) if sorted_times else 0
    p95_time = sorted_times[p95_idx] if sorted_times else 0.0
    max_time = max(sorted_times) if sorted_times else 0.0

    n_both_equ = sum(1 for r in results if r["our_result"] == "EQU" and r["verieql_result"] == "EQU")
    n_both_neq = sum(1 for r in results if r["our_result"] == "NEQ" and r["verieql_result"] == "NEQ")
    n_our_neq_vs_vq_equ = sum(1 for r in results if r["our_result"] == "NEQ" and r["verieql_result"] == "EQU")
    parsed = sum(1 for r in results if r["parse_ok"])
    decided_pairs = n_agree + n_disagree
    agreement_rate = n_agree / decided_pairs * 100 if decided_pairs > 0 else 0.0

    # VeriEQL EQU total for false rejection rate
    vq_equ_total = sum(1 for r in results if r["verieql_result"] == "EQU")
    false_neq = sum(1 for r in results
                    if r["our_result"] == "NEQ" and r["verieql_result"] == "EQU")
    false_rejection_rate = false_neq / vq_equ_total * 100 if vq_equ_total > 0 else 0.0

    # Load VeriEQL baseline times from reference file
    baseline_path = Path(__file__).resolve().parent.parent / "results" / "verieql_baseline_times.json"
    speedup_vs_verieql = 0.0
    try:
        baseline_data = json.loads(baseline_path.read_text())
        suite_baseline = baseline_data.get(suite_name)
        if suite_baseline:
            num_pairs = suite_baseline["num_pairs"]
            per_pair = suite_baseline["per_pair_times_s"]
            if len(results) == num_pairs:
                # Full-suite run: use total from baseline
                vq_time = suite_baseline["total_time_s"]
            else:
                # Subset run: sum per-pair times for the first N pairs
                # If per_pair list is shorter than N, repeat last value
                n = len(results)
                times = per_pair[:n]
                if len(times) < n:
                    times += [per_pair[-1]] * (n - len(times))
                vq_time = sum(times)
            if t_total > 0:
                speedup_vs_verieql = round(vq_time / t_total, 1)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    # Summary
    print(f"\n{'='*70}")
    print(f"  Summary: {suite_name}")
    print(f"{'='*70}")
    print(f"  Total pairs:         {len(results)}")
    print(f"  Parsed:              {parsed}")
    print(f"  Our EQU:             {n_equ}")
    print(f"  Our NEQ:             {n_neq}")
    print(f"  Our UNKNOWN:         {n_unknown}")
    print(f"  Our TMO:             {n_tmo}")
    print(f"  Our PARSE_FAIL:      {n_parse_fail}")
    if n_error > 0:
        print(f"  Our ERROR:           {n_error}")
    print(f"  ---")
    n_our_unk_vs_vq_equ = sum(1 for r in results if r.get("our_result") == "UNKNOWN" and r.get("verieql_result") == "EQU")
    n_our_unk_vs_vq_neq = sum(1 for r in results if r.get("our_result") == "UNKNOWN" and r.get("verieql_result") == "NEQ")
    n_our_tmo_vs_vq_equ = sum(1 for r in results if r.get("our_result") == "TMO" and r.get("verieql_result") == "EQU")
    print(f"  Both proved EQU:     {n_both_equ}")
    print(f"  Both proved NEQ:     {n_both_neq}")
    print(f"  Our NEQ vs VQ EQU:   {n_our_neq_vs_vq_equ}  (false rejection rate: {false_rejection_rate:.1f}%)")
    print(f"  Our UNK vs VQ EQU:   {n_our_unk_vs_vq_equ}")
    print(f"  Our UNK/TMO vs VQ:   {n_our_weaker}  (UNK-vs-NEQ={n_our_unk_vs_vq_neq}, TMO-vs-EQU={n_our_tmo_vs_vq_equ})")
    print(f"  We decided, VQ not:  {n_our_stronger}")
    if n_no_vq_data > 0:
        print(f"  No VQ data:          {n_no_vq_data}")
    print(f"  ---")
    print(f"  NSE handled:         {n_nse_handled}")
    print(f"  ERR handled:         {n_err_handled}")
    print(f"  TMO handled:         {n_tmo_handled}")
    print(f"  ---")
    print(f"  Mean time:           {mean_time:.0f}ms")
    print(f"  Median time:         {median_time:.0f}ms")
    print(f"  P95 time:            {p95_time:.0f}ms")
    print(f"  Max time:            {max_time:.0f}ms")
    print(f"  Total time:          {t_total:.1f}s")
    print(f"  Speedup vs VeriEQL:  {speedup_vs_verieql}×")
    print(f"{'='*70}\n")

    # Invariant: status counters must sum to total
    status_sum = n_equ + n_neq + n_unknown + n_tmo + n_parse_fail + n_error
    if status_sum != len(results):
        logger.warning("METRIC INVARIANT VIOLATION: status sum %d != total %d", status_sum, len(results))

    summary = {
        "benchmark": "verieql",
        "suite": suite_name,
        "total_pairs": len(results),
        "parsed": parsed,
        "our_equ": n_equ,
        "our_neq": n_neq,
        "our_unknown": n_unknown,
        "our_tmo": n_tmo,
        "our_parse_fail": n_parse_fail,
        "our_error": n_error,
        "both_skip": n_both_skip,
        "both_undecided": n_both_undecided,
        "no_vq_data": n_no_vq_data,
        "agree": n_agree,
        "disagree": n_disagree,
        "both_equ": n_both_equ,
        "both_neq": n_both_neq,
        "our_neq_vs_vq_equ": n_our_neq_vs_vq_equ,
        "our_stronger": n_our_stronger,
        "our_weaker": n_our_weaker,
        "our_unk_vs_vq_equ": n_our_unk_vs_vq_equ,
        "our_unk_vs_vq_neq": n_our_unk_vs_vq_neq,
        "our_tmo_vs_vq_equ": n_our_tmo_vs_vq_equ,
        "agreement_rate": round(agreement_rate, 2),
        "false_rejection_rate": round(false_rejection_rate, 2),
        "nse_handled": n_nse_handled,
        "err_handled": n_err_handled,
        "tmo_handled": n_tmo_handled,
        "total_time_s": round(t_total, 2),
        "mean_time_ms": round(mean_time, 1),
        "median_time_ms": round(median_time, 1),
        "p95_time_ms": round(p95_time, 1),
        "max_time_ms": round(max_time, 1),
        "speedup_vs_verieql": speedup_vs_verieql,
        "verieql_baseline_source": "results/verieql_baseline_times.json",
        "k_rows": args.k_rows,
        "timeout_ms": args.timeout_ms,
        "validate": args.validate,
    }

    return results, traces, summary


def _generate_verieql_report(
    results: list[dict],
    summary: dict,
    config: dict,
) -> str:
    """Generate markdown report for VeriEQL benchmark."""
    suite = summary["suite"]
    lines = [
        f"# VeriEQL Benchmark: {suite}",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total pairs | {summary['total_pairs']} |",
        f"| Parsed | {summary['parsed']} ({summary['parsed']/summary['total_pairs']*100:.1f}%) |",
        f"| Our EQU | {summary['our_equ']} |",
        f"| Our NEQ (witness-validated) | {summary['our_neq']} |",
        f"| Our UNKNOWN | {summary['our_unknown']} |",
        f"| Our TMO | {summary['our_tmo']} |",
        f"| Our PARSE_FAIL | {summary['our_parse_fail']} |",
        f"| Both proved EQU | {summary['both_equ']} |",
        f"| Both proved NEQ | {summary['both_neq']} |",
        f"| Our NEQ vs VeriEQL EQU | {summary['our_neq_vs_vq_equ']} (false rej: {summary['false_rejection_rate']:.1f}%) |",
        f"| Speedup vs VeriEQL | {summary['speedup_vs_verieql']}× |",
        f"| Total time | {summary['total_time_s']}s |",
        "",
    ]

    # Cross-tabulation
    vq_categories = ["EQU", "NEQ", "NSE", "ERR"]  # ERR includes NIE
    def _vq_bucket(r):
        v = r["verieql_result"]
        if v in ("ERR", "NIE"):
            return "ERR"
        if v in ("EQU", "NEQ", "NSE"):
            return v
        return "OTHER"

    vq_buckets = {}
    for cat in ["EQU", "NEQ", "NSE", "ERR", "OTHER"]:
        vq_buckets[cat] = [r for r in results if _vq_bucket(r) == cat]

    def _count(subset, our_val):
        return sum(1 for r in subset if r["our_result"] == our_val)

    cols = ["EQU", "NEQ", "NSE", "ERR", "OTHER"]
    header = "|  | " + " | ".join(f"VQ {c}" for c in cols) + " | Total |"
    sep = "|---" + "|---" * (len(cols) + 1) + "|"

    lines.extend([
        "## Cross-Tabulation (Our vs VeriEQL)",
        "",
        header,
        sep,
    ])
    for our_val in ["EQU", "NEQ", "UNKNOWN", "TMO", "PARSE_FAIL"]:
        counts = [_count(vq_buckets[c], our_val) for c in cols]
        c_total = sum(1 for r in results if r["our_result"] == our_val)
        row = f"| **Our {our_val}** | " + " | ".join(str(c) for c in counts) + f" | **{c_total}** |"
        lines.append(row)
    totals = [len(vq_buckets[c]) for c in cols]
    lines.append(f"| **Total** | " + " | ".join(f"**{t}**" for t in totals) + f" | **{summary['total_pairs']}** |")
    lines.append("")

    # Novelty coverage
    nse_decided = sum(1 for r in results
                      if r["verieql_result"] == "NSE" and r["our_result"] in ("EQU", "NEQ"))
    err_decided = sum(1 for r in results
                      if r["verieql_result"] in ("ERR", "NIE") and r["our_result"] in ("EQU", "NEQ"))
    lines.extend([
        "## Novelty Coverage (VeriEQL NSE/ERR → We Decided)",
        "",
        "| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |",
        "|---|---|---|---|---|",
        f"| VeriEQL NSE | {_count(vq_buckets['NSE'], 'EQU')} | {_count(vq_buckets['NSE'], 'NEQ')} "
        f"| {_count(vq_buckets['NSE'], 'UNKNOWN')} | {len(vq_buckets['NSE'])} |",
        f"| VeriEQL ERR | {_count(vq_buckets['ERR'], 'EQU')} | {_count(vq_buckets['ERR'], 'NEQ')} "
        f"| {_count(vq_buckets['ERR'], 'UNKNOWN')} | {len(vq_buckets['ERR'])} |",
        f"| **Total decided** | — | — | — | **{nse_decided + err_decided}** |",
        "",
    ])

    # Timing
    lines.extend([
        "## Timing",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Mean time per pair | {summary['mean_time_ms']:.0f}ms |",
        f"| Median time per pair | {summary['median_time_ms']:.0f}ms |",
        f"| P95 time per pair | {summary['p95_time_ms']:.0f}ms |",
        f"| Max time per pair | {summary['max_time_ms']:.0f}ms |",
        f"| Total wall time | {summary['total_time_s']:.1f}s |",
        "",
    ])

    # Config
    lines.extend([
        "## Configuration",
        "",
        "```json",
        json.dumps(config, indent=2, default=str),
        "```",
    ])

    return "\n".join(lines)


def _save_verieql_outputs(
    results: list[dict],
    traces: list[dict],
    summary: dict,
    args,
    output_dir: str,
) -> None:
    """Save all outputs for a VeriEQL run."""
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)

    config = {
        "benchmark": "verieql",
        "suite": args.suite,
        "k_rows": args.k_rows,
        "timeout_ms": args.timeout_ms,
        "validate": args.validate,
        "max_pairs": args.max_pairs,
        "dialect": args.dialect or "sqlite",
        # FIX.28a: Include subset/sampling params for reproducibility
        "pair_indices": getattr(args, "pair_indices", None),
        "random_sample": getattr(args, "random_sample", False),
        "seed": getattr(args, "seed", None),
        "at_most_k": getattr(args, "at_most_k", False),
        "ignore_constraints": getattr(args, "ignore_constraints", False),
    }

    # config.json
    (path / "config.json").write_text(json.dumps(config, indent=2))
    print(f"Config  saved to {path / 'config.json'}")

    # results.json (includes witness_db as proof of NEQ)
    (path / "results.json").write_text(json.dumps(results, indent=2, default=str))
    print(f"Results saved to {path / 'results.json'}")

    # summary.json
    (path / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Summary saved to {path / 'summary.json'}")

    # report.md
    report = _generate_verieql_report(results, summary, config)
    (path / "report.md").write_text(report)
    print(f"Report  saved to {path / 'report.md'}")

    # traces.jsonl
    with open(path / "traces.jsonl", "w") as f:
        for t in traces:
            f.write(json.dumps(t, default=str) + "\n")
    print(f"Traces  saved to {path / 'traces.jsonl'}")


# ---------------------------------------------------------------------------
# JOB-Complex benchmark runner (delegates to existing code)
# ---------------------------------------------------------------------------

def _run_job_complex(args) -> None:
    """Run JOB-Complex benchmark using existing evaluation logic."""
    import os
    from optim.verify.encode_z3 import BoundedScope

    dialect = args.dialect or "postgres"
    output_dir = args.output or _auto_output_dir(args)

    # Load benchmark
    try:
        from optim.eval.benchmark import load_job_complex
        suite = load_job_complex(args.data_dir)
        queries = suite.queries
    except Exception:
        from optim.eval.benchmark import load_job_complex_queries
        sql_file = os.path.join(args.data_dir, "JOB-Complex", "JOB-Complex.sql")
        queries = load_job_complex_queries(sql_file)

    if args.max_pairs is not None:
        queries = queries[: args.max_pairs]

    from optim.eval.schema_imdb import get_imdb_catalog
    catalog = get_imdb_catalog(args.data_dir)

    from optim.optimizer.loop import optimize_with_config
    from optim.config import OptimizerConfig
    config = OptimizerConfig(
        scope=BoundedScope(k_rows=args.k_rows),
        dialect=dialect,
        enable_llm_rewrites=getattr(args, 'enable_llm', False),
        llm_mode=getattr(args, 'llm_mode', 'smart'),
        llm_n_candidates=getattr(args, 'llm_n_candidates', 5),
        enable_preprocessing=not getattr(args, 'no_preprocessing', False),
        enable_family_pruning=not getattr(args, 'no_family_pruning', False),
        enable_witness_synthesis=not getattr(args, 'no_verification', False),
        enable_compositional=getattr(args, 'enable_compositional', False),
    )
    if getattr(args, 'ablation', None):
        config = OptimizerConfig.ablation(args.ablation)
        config.scope = BoundedScope(k_rows=args.k_rows)
        config.dialect = dialect

    results: list[dict] = []
    traces: list[dict] = []
    n_improved = 0
    n_errors = 0
    total_speedup = 0.0
    all_times: list[float] = []

    print(f"\n{'='*70}")
    print(f"  JOB-Complex Evaluation: {len(queries)} queries")
    print(f"  dialect={dialect}  k_rows={args.k_rows}")
    print(f"  Output: {output_dir}")
    print(f"{'='*70}\n")

    t_start = time.monotonic()

    for i, bq in enumerate(queries):
        try:
            result = optimize_with_config(bq.sql, catalog, config)

            rejection_reasons: dict[str, int] = {}
            for rej in result.rejected:
                rejection_reasons[rej.reason] = rejection_reasons.get(rej.reason, 0) + 1

            entry = {
                "id": bq.id,
                "original_sql": result.original_sql,
                "optimized_sql": result.optimized_sql,
                "improved": result.improved,
                "speedup": round(result.speedup, 3),
                "cost_original": result.cost_original.total_cost,
                "cost_optimized": result.cost_optimized.total_cost,
                "total_candidates": result.total_candidates,
                "n_verified": result.n_verified,
                "n_rejected": result.n_rejected,
                "rejection_reasons": rejection_reasons,
                "solver_time_ms": round(result.solver_time_ms, 1),
                "total_time_ms": round(result.total_time_ms, 1),
                "error": None,
            }
            trace = dict(entry)

            if result.improved:
                n_improved += 1
                total_speedup += result.speedup
                status_label = (
                    f"IMPROVED {result.speedup:.2f}× "
                    f"(cost {result.cost_original.total_cost:.1f} → "
                    f"{result.cost_optimized.total_cost:.1f})"
                )
            else:
                status_label = (
                    f"no improvement "
                    f"({result.total_candidates} candidates, "
                    f"{result.n_verified} verified)"
                )

            logger.info(
                "PROGRESS [%d/%d] %s: status=%s speedup=%.3f cost=%.1f→%.1f "
                "candidates=%d verified=%d rejected=%d solver_ms=%.0f total_ms=%.0f",
                i + 1, len(queries), bq.id,
                "improved" if result.improved else "no_improvement",
                result.speedup,
                result.cost_original.total_cost, result.cost_optimized.total_cost,
                result.total_candidates, result.n_verified, result.n_rejected,
                result.solver_time_ms, result.total_time_ms,
            )

            elapsed = time.monotonic() - t_start
            eta = (len(queries) - i - 1) / ((i + 1) / elapsed) if elapsed > 0 else 0
            status_sym = "✓" if result.improved else "✗"
            print(
                f"  [{i+1:3d}/{len(queries)}] {status_sym} {bq.id}: "
                f"{status_label} | improved={n_improved}/{i+1} ({n_improved/(i+1)*100:.0f}%) "
                f"[{result.total_time_ms:.0f}ms, ~{eta:.0f}s rem]"
            )

        except Exception as exc:
            entry = {
                "id": bq.id,
                "original_sql": bq.sql,
                "optimized_sql": None,
                "improved": False,
                "speedup": 1.0,
                "cost_original": None,
                "cost_optimized": None,
                "total_candidates": 0,
                "n_verified": 0,
                "n_rejected": 0,
                "rejection_reasons": {},
                "solver_time_ms": 0.0,
                "total_time_ms": 0.0,
                "error": str(exc),
            }
            trace = dict(entry)
            n_errors += 1
            logger.error("PROGRESS [%d/%d] %s: status=error error=%s",
                        i + 1, len(queries), bq.id, exc)
            elapsed = time.monotonic() - t_start
            eta = (len(queries) - i - 1) / ((i + 1) / elapsed) if elapsed > 0 else 0
            print(
                f"  [{i+1:3d}/{len(queries)}] ! {bq.id}: ERROR: {exc} "
                f"| improved={n_improved}/{i+1} ({n_improved/(i+1)*100:.0f}%) "
                f"[~{eta:.0f}s rem]"
            )

        all_times.append(entry.get("total_time_ms", 0))
        results.append(entry)
        traces.append(trace)

    t_total = time.monotonic() - t_start
    avg_speedup = round(total_speedup / n_improved, 3) if n_improved else 1.0

    # Timing stats
    sorted_times = sorted(t for t in all_times if t > 0)
    mean_time = statistics.mean(sorted_times) if sorted_times else 0.0
    median_time = statistics.median(sorted_times) if sorted_times else 0.0
    p95_idx = min(int(len(sorted_times) * 0.95), len(sorted_times) - 1) if sorted_times else 0
    p95_time = sorted_times[p95_idx] if sorted_times else 0.0
    max_time = max(sorted_times) if sorted_times else 0.0

    # Summary
    print(f"\n{'='*70}")
    print(f"  Summary")
    print(f"{'='*70}")
    print(f"  Total queries:    {len(queries)}")
    print(f"  Improved:         {n_improved}")
    print(f"  Errors:           {n_errors}")
    if n_improved > 0:
        print(f"  Avg speedup:      {avg_speedup:.2f}×")
    print(f"  Total time:       {t_total:.1f}s")
    print(f"{'='*70}\n")

    summary = {
        "benchmark": "JOB-Complex",
        "total_queries": len(queries),
        "n_improved": n_improved,
        "n_errors": n_errors,
        "avg_speedup": avg_speedup,
        "total_time_s": round(t_total, 2),
        "mean_time_ms": round(mean_time, 1),
        "median_time_ms": round(median_time, 1),
        "p95_time_ms": round(p95_time, 1),
        "max_time_ms": round(max_time, 1),
        "k_rows": args.k_rows,
        "dialect": dialect,
    }

    config = {
        "benchmark": "job-complex",
        "dialect": dialect,
        "k_rows": args.k_rows,
        "max_pairs": args.max_pairs,
        "data_dir": args.data_dir,
    }

    # Save outputs
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)

    (path / "config.json").write_text(json.dumps(config, indent=2))
    (path / "results.json").write_text(json.dumps(results, indent=2))
    (path / "summary.json").write_text(json.dumps(summary, indent=2))

    # report.md
    md_lines = [
        f"# JOB-Complex Evaluation",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total queries | {len(queries)} |",
        f"| Improved | {n_improved} |",
        f"| Errors | {n_errors} |",
        f"| Avg speedup | {avg_speedup:.3f}× |",
        f"| Total time | {t_total:.1f}s |",
        "",
        "## Timing",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Mean time per query | {mean_time:.0f}ms |",
        f"| Median time per query | {median_time:.0f}ms |",
        f"| P95 time per query | {p95_time:.0f}ms |",
        f"| Max time per query | {max_time:.0f}ms |",
        "",
        "## Configuration",
        "",
        "```json",
        json.dumps(config, indent=2),
        "```",
    ]
    (path / "report.md").write_text("\n".join(md_lines))

    # traces.jsonl
    with open(path / "traces.jsonl", "w") as f:
        for t in traces:
            f.write(json.dumps(t, default=str) + "\n")

    print(f"Results saved to {output_dir}/")


# ---------------------------------------------------------------------------
# Multiprocessing-based hard timeout for Z3 calls
# ---------------------------------------------------------------------------

def _mp_verify_pair(conn, sql1, sql2, dialect, catalog_dict, k_rows, timeout_ms, validate, at_most_k):
    """Worker function that runs in a child process.

    Sends result dict back via ``conn`` (a multiprocessing.Connection).
    The parent can kill this process to enforce a hard wall-clock limit
    even when Z3 C code ignores signals.
    """
    try:
        from optim.eval.schema_sqlstorm import get_sqlstorm_catalog
        from optim.parser.sql_to_ir import sql_to_ir
        from optim.cegis.witness_synthesis import synthesize_witness_adaptive
        from optim.verify.encode_z3 import BoundedScope

        catalog = get_sqlstorm_catalog(catalog_dict) if isinstance(catalog_dict, str) else catalog_dict
        scope = BoundedScope(k_rows=k_rows, solver_timeout_ms=timeout_ms)

        ir1, err1 = sql_to_ir(sql1, dialect=dialect, catalog=catalog)
        ir2, err2 = sql_to_ir(sql2, dialect=dialect, catalog=catalog)

        if ir1 is None or ir2 is None:
            conn.send({
                "parse_ok": False,
                "our_result": "PARSE_FAIL",
                "error": err1 or err2 or "unknown parse error",
            })
            return

        result = synthesize_witness_adaptive(
            ir1, ir2, catalog, scope,
            validate_witnesses=validate,
            original_sql=(sql1, sql2),
            at_most_k=at_most_k,
        )

        out = {
            "parse_ok": True,
            "status": result.status,
            "complete": result.complete,
            "proven_k": result.proven_k,
            "witness_db": result.witness_db,
        }
        conn.send(out)
    except Exception as exc:
        conn.send({"parse_ok": False, "our_result": "ERROR", "error": str(exc)})


def _run_pair_with_timeout(sql1, sql2, dialect, dataset_name, k_rows, timeout_ms, validate, at_most_k, wall_limit_s):
    """Run a single pair verification in a child process with hard timeout.

    Returns a dict with keys: parse_ok, our_result, error, proven_k, complete, witness_db.
    """
    parent_conn, child_conn = multiprocessing.Pipe()
    proc = multiprocessing.Process(
        target=_mp_verify_pair,
        args=(child_conn, sql1, sql2, dialect, dataset_name, k_rows, timeout_ms, validate, at_most_k),
        daemon=True,
    )
    proc.start()
    if parent_conn.poll(wall_limit_s):
        result = parent_conn.recv()
    else:
        # Hard kill — terminates Z3 C code regardless of state
        proc.kill()
        proc.join(timeout=2)
        result = {"parse_ok": False, "our_result": "TMO", "error": "pair wall-clock timeout (killed)"}
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=2)
    parent_conn.close()
    child_conn.close()
    return result


# ---------------------------------------------------------------------------
# SQLStorm benchmark runner
# ---------------------------------------------------------------------------

def _run_sqlstorm(args) -> None:
    """Run SQLStorm rewrite-pair equivalence verification."""
    dataset = args.dataset
    dialect = args.dialect or "postgres"

    # Load from curated sample directory if available,
    # otherwise fall back to raw SQLStorm data.
    source = getattr(args, "sqlstorm_source", "orig")
    sample_filename = f"{dataset}_llm.jsonl" if source == "llm" else f"{dataset}.jsonl"
    sample_dir = Path(getattr(args, "sample_dir", None) or "scripts/sqlstorm_sample")
    sample_path = sample_dir / sample_filename
    if sample_path.exists():
        pairs = []
        with sample_path.open() as f:
            for line in f:
                if line.strip():
                    pairs.append(json.loads(line))
        if args.max_pairs is not None:
            pairs = pairs[:args.max_pairs]
        logger.info("Loaded %d pairs from curated sample %s", len(pairs), sample_path)
    else:
        from optim.eval.sqlstorm_loader import load_sqlstorm_pairs
        pairs = load_sqlstorm_pairs(dataset, max_pairs=args.max_pairs)

    if not pairs:
        print(f"No SQLStorm pairs found for dataset={dataset}")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    output_dir = args.output or f"results/sqlstorm_{dataset}_{ts}_k{args.k_rows}"
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    traces: list[dict] = []
    n_equ = n_neq = n_unknown = n_tmo = n_parse_fail = n_error = 0
    all_times: list[float] = []

    print(f"\n{'='*70}")
    print(f"  SQLStorm Benchmark: {dataset} ({len(pairs)} pairs)")
    print(f"  k_rows={args.k_rows}  timeout_ms={args.timeout_ms}  validate={args.validate}")
    print(f"  dialect={dialect}  output={output_dir}")
    print(f"{'='*70}\n")

    t_start = time.monotonic()

    for i, pair in enumerate(pairs):
        entry: dict = {
            "pair_index": i,
            "pair_id": pair["pair_id"],
            "dataset": dataset,
            "parse_ok": False,
            "our_result": None,
            "time_ms": 0.0,
            "error": None,
            "sql1": pair["sql1"],
            "sql2": pair["sql2"],
            "witness_db": None,
            "validation_status": None,
        }

        trace: dict = {
            "pair_index": i,
            "pair_id": pair["pair_id"],
            "our_result": None,
            "time_ms": 0.0,
        }

        # Per-pair hard wall-clock timeout via multiprocessing.
        # 2× solver timeout to allow encoding + solving, but actually
        # enforced by killing the child process.
        pair_wall_limit_s = max(args.timeout_ms * 2 / 1000, 12)

        t0 = time.monotonic()
        try:
            mp_result = _run_pair_with_timeout(
                pair["sql1"], pair["sql2"], dialect, dataset,
                args.k_rows, args.timeout_ms, args.validate,
                getattr(args, 'at_most_k', False),
                pair_wall_limit_s,
            )
            entry["time_ms"] = round((time.monotonic() - t0) * 1000, 1)

            if mp_result.get("our_result") == "PARSE_FAIL":
                entry["our_result"] = "PARSE_FAIL"
                entry["error"] = mp_result.get("error")
                n_parse_fail += 1
            elif mp_result.get("our_result") == "TMO":
                entry["our_result"] = "TMO"
                entry["error"] = mp_result.get("error")
                n_tmo += 1
            elif mp_result.get("our_result") == "ERROR":
                entry["our_result"] = "ERROR"
                entry["error"] = mp_result.get("error")
                n_error += 1
            elif mp_result.get("parse_ok"):
                entry["parse_ok"] = True
                status = mp_result["status"]
                complete = mp_result.get("complete", True)

                # FIX.28b: incomplete UNSAT (lower-k only) → UNKNOWN
                if status == "unsat" and not complete:
                    entry["our_result"] = "UNKNOWN"
                else:
                    entry["our_result"] = _map_witness_status(status)
                entry["proven_k"] = mp_result.get("proven_k")
                entry["complete"] = complete

                if mp_result.get("witness_db") is not None:
                    entry["witness_db"] = mp_result["witness_db"]

                if entry["our_result"] == "EQU":
                    n_equ += 1
                    entry["validation_status"] = "proved_equivalent"
                elif entry["our_result"] == "NEQ":
                    n_neq += 1
                    has_witness = mp_result.get("witness_db") is not None
                    effective_validate = args.validate or getattr(args, 'at_most_k', False)
                    if effective_validate and has_witness:
                        entry["validation_status"] = "confirmed"
                    elif has_witness:
                        entry["validation_status"] = "unvalidated"
                    else:
                        entry["validation_status"] = "no_witness"
                elif entry["our_result"] == "UNKNOWN":
                    n_unknown += 1
                elif entry["our_result"] == "TMO":
                    n_tmo += 1
            else:
                entry["our_result"] = "ERROR"
                entry["error"] = mp_result.get("error", "unknown worker error")
                n_error += 1

        except Exception as exc:
            entry["our_result"] = "ERROR"
            entry["error"] = str(exc)
            entry["time_ms"] = round((time.monotonic() - t0) * 1000, 1)
            n_error += 1
            logger.debug("ERROR [%d]: %s", i, exc, exc_info=True)

        trace["our_result"] = entry["our_result"]
        trace["time_ms"] = entry["time_ms"]

        all_times.append(entry["time_ms"])
        results.append(entry)
        traces.append(trace)

        done = i + 1
        elapsed = time.monotonic() - t_start
        eta = (len(pairs) - done) / (done / elapsed) if elapsed > 0 else 0
        sym = {"EQU": "≡", "NEQ": "≠", "TMO": "⏱", "UNKNOWN": "?",
               "PARSE_FAIL": "✗", "ERROR": "!"}.get(entry["our_result"], "·")

        print(
            f"  [{done:5d}/{len(pairs)}] {sym} pair {entry['pair_id']:>6s}: "
            f"{entry['our_result']:10s} [{entry['time_ms']:.0f}ms] "
            f"EQU={n_equ} NEQ={n_neq} UNK={n_unknown} "
            f"[~{eta:.0f}s rem]"
        )

        # Structured PROGRESS log line (for check_progress.py)
        logger.info(
            "PROGRESS [%d/%d] pair_index=%d: status=%s time_ms=%.1f "
            "EQU=%d NEQ=%d UNK=%d TMO=%d PFAIL=%d",
            done, len(pairs), i, entry["our_result"], entry["time_ms"],
            n_equ, n_neq, n_unknown, n_tmo, n_parse_fail,
        )

        # Checkpoint + memory cleanup
        if args.checkpoint_every and done % args.checkpoint_every == 0:
            logger.info(
                "CHECKPOINT %d/%d: EQU=%d NEQ=%d UNKNOWN=%d TMO=%d PARSE_FAIL=%d",
                done, len(pairs), n_equ, n_neq, n_unknown, n_tmo, n_parse_fail,
            )
            # Save intermediate results
            with open(out_path / "results.json", "w") as f:
                json.dump(results, f, indent=2, default=str)
            import gc
            gc.collect()
            try:
                import z3
                z3._main_ctx = None
                z3.main_ctx()
            except Exception:
                pass

    t_total = time.monotonic() - t_start

    # Timing stats
    parsed = sum(1 for r in results if r["parse_ok"])
    sorted_times = sorted(all_times)
    mean_time = statistics.mean(sorted_times) if sorted_times else 0.0
    median_time = statistics.median(sorted_times) if sorted_times else 0.0
    p95_idx = min(int(len(sorted_times) * 0.95), len(sorted_times) - 1) if sorted_times else 0
    p95_time = sorted_times[p95_idx] if sorted_times else 0.0
    max_time = max(sorted_times) if sorted_times else 0.0

    # Invariant: status counters must sum to total
    status_sum = n_equ + n_neq + n_unknown + n_tmo + n_parse_fail + n_error
    if status_sum != len(results):
        logger.warning("METRIC INVARIANT VIOLATION: status sum %d != total %d", status_sum, len(results))

    summary = {
        "benchmark": "sqlstorm",
        "dataset": dataset,
        "total_pairs": len(pairs),
        "parsed": parsed,
        "our_equ": n_equ,
        "our_neq": n_neq,
        "our_unknown": n_unknown,
        "our_tmo": n_tmo,
        "our_parse_fail": n_parse_fail,
        "our_error": n_error,
        "total_time_s": round(t_total, 2),
        "mean_time_ms": round(mean_time, 1),
        "median_time_ms": round(median_time, 1),
        "p95_time_ms": round(p95_time, 1),
        "max_time_ms": round(max_time, 1),
        "k_rows": args.k_rows,
        "timeout_ms": args.timeout_ms,
        "validate": args.validate,
    }

    print(f"\n{'='*70}")
    print(f"  Summary: sqlstorm/{dataset}")
    print(f"{'='*70}")
    print(f"  Total pairs:         {len(results)}")
    print(f"  Parsed:              {parsed}")
    print(f"  Our EQU:             {n_equ}")
    print(f"  Our NEQ:             {n_neq}")
    print(f"  Our UNKNOWN:         {n_unknown}")
    print(f"  Our TMO:             {n_tmo}")
    print(f"  Our PARSE_FAIL:      {n_parse_fail}")
    if n_error > 0:
        print(f"  Our ERROR:           {n_error}")
    print(f"  ---")
    print(f"  Mean time:           {mean_time:.0f}ms")
    print(f"  Median time:         {median_time:.0f}ms")
    print(f"  P95 time:            {p95_time:.0f}ms")
    print(f"  Max time:            {max_time:.0f}ms")
    print(f"  Total time:          {t_total:.1f}s")
    print(f"{'='*70}\n")

    # Save outputs (same structure as VeriEQL)
    config = {
        "benchmark": "sqlstorm",
        "dataset": dataset,
        "k_rows": args.k_rows,
        "timeout_ms": args.timeout_ms,
        "validate": args.validate,
        "max_pairs": args.max_pairs,
        "dialect": dialect,
        "at_most_k": getattr(args, "at_most_k", False),
    }

    (out_path / "config.json").write_text(json.dumps(config, indent=2))
    print(f"Config  saved to {out_path / 'config.json'}")

    (out_path / "results.json").write_text(json.dumps(results, indent=2, default=str))
    print(f"Results saved to {out_path / 'results.json'}")

    (out_path / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Summary saved to {out_path / 'summary.json'}")

    report = _generate_sqlstorm_report(results, summary, config)
    (out_path / "report.md").write_text(report)
    print(f"Report  saved to {out_path / 'report.md'}")

    with open(out_path / "traces.jsonl", "w") as f:
        for t in traces:
            f.write(json.dumps(t, default=str) + "\n")
    print(f"Traces  saved to {out_path / 'traces.jsonl'}")


def _generate_sqlstorm_report(
    results: list[dict],
    summary: dict,
    config: dict,
) -> str:
    """Generate markdown report for SQLStorm benchmark."""
    dataset = summary["dataset"]
    lines = [
        f"# SQLStorm Benchmark: {dataset}",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total pairs | {summary['total_pairs']} |",
        f"| Parsed | {summary['parsed']} ({summary['parsed']/summary['total_pairs']*100:.1f}%) |",
        f"| Our EQU | {summary['our_equ']} |",
        f"| Our NEQ (witness-validated) | {summary['our_neq']} |",
        f"| Our UNKNOWN | {summary['our_unknown']} |",
        f"| Our TMO | {summary['our_tmo']} |",
        f"| Our PARSE_FAIL | {summary['our_parse_fail']} |",
        f"| Total time | {summary['total_time_s']}s |",
        "",
        "## Timing",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Mean time per pair | {summary['mean_time_ms']:.0f}ms |",
        f"| Median time per pair | {summary['median_time_ms']:.0f}ms |",
        f"| P95 time per pair | {summary['p95_time_ms']:.0f}ms |",
        f"| Max time per pair | {summary['max_time_ms']:.0f}ms |",
        f"| Total wall time | {summary['total_time_s']:.1f}s |",
        "",
        "## Configuration",
        "",
        "```json",
        json.dumps(config, indent=2, default=str),
        "```",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.config:
        load_config_file(args.config, args)

    if not args.benchmark:
        print("error: --benchmark is required (via CLI or --config)", file=sys.stderr)
        sys.exit(1)

    # --- Configure logging ---
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")

    if args.benchmark == "verieql":
        log_file = log_dir / f"eval_{ts}_verieql_{args.suite}_k{args.k_rows}.log"
    elif args.benchmark == "sqlstorm":
        log_file = log_dir / f"eval_{ts}_sqlstorm_{args.dataset}_k{args.k_rows}.log"
    else:
        dialect = args.dialect or "postgres"
        log_file = log_dir / f"eval_{ts}_job_{dialect}_k{args.k_rows}.log"

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stderr) if args.verbose else logging.NullHandler(),
        ],
    )
    logging.getLogger("optim").setLevel(logging.DEBUG)
    logging.getLogger("z3").setLevel(logging.WARNING)
    logging.getLogger("sqlglot").setLevel(logging.WARNING)

    logger.info("Starting evaluation: benchmark=%s k_rows=%s timeout_ms=%s",
                args.benchmark, args.k_rows, args.timeout_ms)
    logger.info("Log file: %s", log_file)

    # --- Run ---
    if args.benchmark == "verieql":
        suites = list(VERIEQL_SUITE_FILES.keys()) if args.suite == "all" else [args.suite]

        for suite_name in suites:
            args_copy = argparse.Namespace(**vars(args))
            args_copy.suite = suite_name
            output_dir = args.output or _auto_output_dir(args_copy)

            results, traces, summary = _run_verieql_suite(suite_name, args)
            _save_verieql_outputs(results, traces, summary, args_copy, output_dir)

    elif args.benchmark == "sqlstorm":
        _run_sqlstorm(args)

    elif args.benchmark == "job-complex":
        _run_job_complex(args)

    print(f"Log file: {log_file}")


if __name__ == "__main__":
    main()
