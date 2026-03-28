#!/usr/bin/env python3
"""Certificate replay credibility test using LeetCode traces.

Reads EQU pairs from existing evaluation traces (which contain per-pair
schemas), generates equivalence certificates, saves them to disk, then
replays each certificate independently.

Usage:
    python3 -m scripts.test_certificate_replay
    python3 -m scripts.test_certificate_replay --max-pairs 200
    python3 -m scripts.test_certificate_replay --suite calcite --max-pairs 50
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import time
from pathlib import Path

import hashlib

from src.optim.cegis.witness_synthesis import synthesize_witness_adaptive
from src.optim.eval.verieql_loader import load_verieql_entry
from src.optim.ir.normalization import normalize
from src.optim.ir.render_sql import render
from src.optim.parser.sql_to_ir import sql_to_ir
from src.optim.verify.certificate import Certificate, replay_certificate
from src.optim.verify.encode_z3 import BoundedScope

RESULTS_DIR = Path("results/certificate_replay_test")
CERTS_DIR = RESULTS_DIR / "certificates"

TRACE_PATHS = {
    "leetcode": "results/verieql_leetcode/traces.jsonl",
    "calcite": "results/verieql_calcite/traces.jsonl",
    "literature": "results/verieql_literature/traces.jsonl",
}


def load_equ_traces(suite: str, max_pairs: int) -> list[dict]:
    """Load EQU traces with embedded schema."""
    path = TRACE_PATHS[suite]
    traces = []
    with open(path) as f:
        for line in f:
            t = json.loads(line)
            if t.get("our_result") == "EQU" and t.get("schema"):
                traces.append(t)
                if len(traces) >= max_pairs:
                    break
    return traces


def trace_to_catalog_and_ir(trace: dict, scope: BoundedScope):
    """Build catalog and parse IRs from a trace entry."""
    # Reconstruct the entry format load_verieql_entry expects
    entry = {
        "schema": trace["schema"],
        "constraint": trace.get("constraints", []),
        "pair": [trace["sql1"], trace["sql2"]],
    }
    catalog, sql1, sql2 = load_verieql_entry(entry)

    ir1, err1 = sql_to_ir(sql1, dialect="sqlite", catalog=catalog)
    ir2, err2 = sql_to_ir(sql2, dialect="sqlite", catalog=catalog)

    if ir1 is None or ir2 is None:
        return None, None, None, None

    return catalog, normalize(ir1), normalize(ir2), trace.get("proven_k", scope.k_rows)


def main():
    parser = argparse.ArgumentParser(
        description="Certificate replay credibility test from evaluation traces.",
    )
    parser.add_argument("--suite", default="leetcode", choices=TRACE_PATHS.keys())
    parser.add_argument("--max-pairs", type=int, default=100)
    args = parser.parse_args()

    if CERTS_DIR.exists():
        shutil.rmtree(CERTS_DIR)
    CERTS_DIR.mkdir(parents=True)

    scope = BoundedScope(k_rows=2, solver_timeout_ms=10000)

    traces = load_equ_traces(args.suite, args.max_pairs)
    print(f"Loaded {len(traces)} EQU traces from {args.suite}")
    print(f"{'='*70}")

    # === Phase 1: Generate & Save Certificates ===
    print("\n=== Phase 1: Generate & Save Certificates ===\n")
    generated = 0
    skip_parse = 0
    skip_verify = 0
    skip_cert = 0

    for trace in traces:
        pair_id = str(trace["pair_index"])
        catalog, ir1, ir2, proven_k = trace_to_catalog_and_ir(trace, scope)

        if catalog is None:
            skip_parse += 1
            continue

        # Re-verify at k=3 (matching the original evaluation's --k-rows 3)
        pair_scope = BoundedScope(
            k_rows=3,
            solver_timeout_ms=scope.solver_timeout_ms,
        )
        orig_sql1 = render(ir1, dialect="sqlite")
        orig_sql2 = render(ir2, dialect="sqlite")
        result = synthesize_witness_adaptive(
            ir1, ir2, catalog, pair_scope,
            validate_witnesses=True,
            original_sql=(orig_sql1, orig_sql2),
            at_most_k=True,
            normalize_column_order=False,
        )
        if result.status != "unsat":
            skip_verify += 1
            continue

        # Build equivalence certificate directly (skip structural verify)
        try:
            from src.optim.verify.certificate import _compute_catalog_hash
            rewrite_sql = render(ir2, dialect="sqlite")
            ir2_json = ir2.model_dump(mode="json")
            ir2_str = json.dumps(ir2_json, sort_keys=True, default=str)

            orig_sql = render(ir1, dialect="sqlite")
            ir1_json = ir1.model_dump(mode="json")
            ir1_str = json.dumps(ir1_json, sort_keys=True, default=str)

            cert = Certificate(
                scope={
                    "k_rows": pair_scope.k_rows,
                    "int_bounds": list(pair_scope.int_bounds),
                    "string_symbols": pair_scope.string_symbols,
                    "date_values": pair_scope.date_values,
                    "null_semantics": pair_scope.null_semantics,
                    "solver_timeout_ms": pair_scope.solver_timeout_ms,
                },
                ir_json=ir2_json,
                sql=rewrite_sql,
                dialect="sqlite",
                constraints=[],
                solver_status="skipped",
                solver_time_ms=0,
                solver_stats={},
                ir_hash=hashlib.sha256(ir2_str.encode()).hexdigest()[:16],
                sql_hash=hashlib.sha256(rewrite_sql.encode()).hexdigest()[:16],
                catalog_hash=_compute_catalog_hash(catalog),
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                proof_kind="equivalence",
                original_ir_json=ir1_json,
                original_sql=orig_sql,
                original_ir_hash=hashlib.sha256(ir1_str.encode()).hexdigest()[:16],
                original_sql_hash=hashlib.sha256(orig_sql.encode()).hexdigest()[:16],
                equivalence_status=result.status,
                equivalence_solver_time_ms=result.solver_time_ms,
                equivalence_proven_k=getattr(result, 'proven_k', pair_scope.k_rows),
                equivalence_complete=True,
            )
        except Exception as e:
            skip_cert += 1
            continue

        cert_path = CERTS_DIR / f"pair_{pair_id}"
        cert.save(cert_path, catalog=catalog)
        generated += 1

        if generated % 20 == 0:
            print(f"  [{generated:>3d}] generated so far...")

    print(f"\n  Generated: {generated}")
    print(f"  Skipped — parse fail: {skip_parse}")
    print(f"  Skipped — verify !unsat: {skip_verify}")
    print(f"  Skipped — cert build fail: {skip_cert}")

    # === Phase 2: Replay Certificates ===
    print(f"\n{'='*70}")
    print("=== Phase 2: Replay Certificates (from disk, independent) ===\n")

    cert_dirs = sorted(CERTS_DIR.iterdir())
    passed = 0
    failed = 0
    replay_times = []
    failures = []

    for cert_dir in cert_dirs:
        if not cert_dir.is_dir():
            continue
        pair_id = cert_dir.name.replace("pair_", "")

        # Load certificate and catalog from disk only
        cert = Certificate.load(cert_dir)
        loaded_catalog = Certificate.load_catalog(cert_dir)
        if loaded_catalog is None:
            failed += 1
            failures.append((pair_id, "no catalog snapshot"))
            continue

        t0 = time.monotonic()
        replay = replay_certificate(cert, loaded_catalog)
        replay_ms = (time.monotonic() - t0) * 1000
        replay_times.append(replay_ms)

        if replay.valid:
            passed += 1
        else:
            failed += 1
            failures.append((pair_id, replay.errors))

    # === Results ===
    print(f"{'='*70}")
    print(f"=== Certificate Replay Results ===")
    print(f"  Suite:                 {args.suite}")
    print(f"  Input EQU traces:      {len(traces)}")
    print(f"  Certificates generated: {generated}")
    print(f"  Replay passed:          {passed}")
    print(f"  Replay failed:          {failed}")
    if replay_times:
        print(f"  Replay time — mean:     {statistics.mean(replay_times):.0f}ms")
        print(f"  Replay time — median:   {statistics.median(replay_times):.0f}ms")
        print(f"  Replay time — p95:      {sorted(replay_times)[int(len(replay_times)*0.95)]:.0f}ms")
        print(f"  Replay time — max:      {max(replay_times):.0f}ms")
    total = passed + failed
    print(f"  Pass rate:              {passed/total*100:.1f}%" if total else "  Pass rate: N/A")

    if failures:
        print(f"\n  Failures ({len(failures)}):")
        for pid, err in failures[:10]:
            print(f"    pair {pid}: {err}")

    # Save summary
    summary = {
        "suite": args.suite,
        "input_equ_traces": len(traces),
        "certificates_generated": generated,
        "skip_parse": skip_parse,
        "skip_verify": skip_verify,
        "skip_cert_build": skip_cert,
        "replay_passed": passed,
        "replay_failed": failed,
        "pass_rate": round(passed / total * 100, 1) if total else 0,
        "mean_replay_ms": round(statistics.mean(replay_times), 1) if replay_times else 0,
        "median_replay_ms": round(statistics.median(replay_times), 1) if replay_times else 0,
        "p95_replay_ms": round(sorted(replay_times)[int(len(replay_times)*0.95)], 1) if replay_times else 0,
        "max_replay_ms": round(max(replay_times), 1) if replay_times else 0,
    }
    (RESULTS_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\n  Summary saved to {RESULTS_DIR}/summary.json")


if __name__ == "__main__":
    main()
