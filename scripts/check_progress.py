#!/usr/bin/env python3
"""Check progress of a running or completed evaluation.

Supports both JOB-Complex and VeriEQL benchmarks with auto-detection.

Usage:
    python3 -m scripts.check_progress                              # latest log (auto-detect)
    python3 -m scripts.check_progress logs/eval_*.log              # specific log
    python3 -m scripts.check_progress results/verieql_calcite_*/   # from saved results dir
    python3 -m scripts.check_progress results/*/summary.json       # from summary
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# JOB-Complex rejection reason keys we track
REJECTION_REASONS = [
    "non_equivalent",
    "structural",
    "solver_unknown",
    "compositional_inconclusive",
    "family_pruned",
]

# VeriEQL result categories
VERIEQL_STATUSES = ["EQU", "NEQ", "UNKNOWN", "TMO", "PARSE_FAIL"]


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------

def _detect_benchmark_from_log(log_path: str) -> str:
    """Peek at a log file to decide if it's VeriEQL, SQLStorm, or JOB-Complex."""
    with open(log_path) as f:
        for line in f:
            if "PROGRESS" not in line:
                continue
            if re.search(r"comparison=(AGREE|DISAGREE|OUR_STRONGER|OUR_WEAKER|BOTH_SKIP|BOTH_UNDECIDED|PARSE_FAIL)", line):
                return "verieql"
            if re.search(r"status=(EQU|NEQ|UNKNOWN|TMO|PARSE_FAIL)\b", line) and "vs=" in line:
                return "verieql"
            # SQLStorm: structured PROGRESS with EQU=/NEQ=/UNK= counters but no vs=
            if re.search(r"status=(EQU|NEQ|UNKNOWN|TMO|PARSE_FAIL)\b", line) and "EQU=" in line and "vs=" not in line:
                return "sqlstorm"
            if re.search(r"status=(improved|no_improvement|error)\b", line, re.IGNORECASE):
                return "job-complex"
            # Old-style lines: [  1/30] JOB-C01 ...
            if re.search(r"JOB-C\d+", line):
                return "job-complex"
    return "job-complex"  # default


def _detect_benchmark_from_summary(summary: dict) -> str:
    """Detect benchmark type from a summary.json dict."""
    if summary.get("benchmark") == "verieql":
        return "verieql"
    if summary.get("benchmark") == "sqlstorm":
        return "sqlstorm"
    if summary.get("benchmark") == "JOB-Complex":
        return "job-complex"
    # Heuristic: VeriEQL summaries have "suite" and "our_equ" fields
    if "our_equ" in summary or "suite" in summary:
        return "verieql"
    return "job-complex"


def _detect_benchmark_from_results_dir(results_dir: Path) -> str:
    """Detect benchmark from a results directory."""
    summary_path = results_dir / "summary.json"
    if summary_path.exists():
        summary = json.load(open(summary_path))
        return _detect_benchmark_from_summary(summary)
    # Heuristic: dir name
    if "verieql" in results_dir.name.lower():
        return "verieql"
    if "sqlstorm" in results_dir.name.lower():
        return "sqlstorm"
    return "job-complex"


# ---------------------------------------------------------------------------
# JOB-Complex log parsing (original)
# ---------------------------------------------------------------------------

def parse_job_log(log_path: str) -> dict:
    """Parse a JOB-Complex eval log file to extract progress."""
    seen: dict[str, dict] = {}
    total_target = None
    last_line = ""
    rejection_counts: Counter = Counter()

    with open(log_path) as f:
        for line in f:
            # Match progress lines: [  1/30] JOB-C01 ... IMPROVED|no improvement|ERROR
            m = re.search(
                r"\[\s*(\d+)/(\d+)\]\s+(JOB-C\d+)\s+\.\.\.\s+(.*)", line
            )
            if not m:
                # Also try structured PROGRESS lines
                m2 = re.search(
                    r"PROGRESS\s+\[(\d+)/(\d+)\]\s+(JOB-C\d+):\s+(.*)", line
                )
                if m2:
                    m = m2

            if not m:
                # Check for rejection reason lines in debug log
                rej_m = re.search(
                    r"rejected.*reason=(\w+)", line, re.IGNORECASE
                )
                if rej_m:
                    reason = rej_m.group(1).lower()
                    rejection_counts[reason] += 1
                continue

            idx, target, query_id, rest = m.groups()
            total_target = int(target)

            if query_id in seen:
                continue

            entry: dict = {"id": query_id, "index": int(idx)}

            # Parse IMPROVED
            imp = re.search(
                r"IMPROVED\s+([\d.]+)×\s+\(cost\s+([\d.]+)\s*→\s*([\d.]+)\)\s+\[(\d+)ms\]",
                rest,
            )
            if imp:
                entry["status"] = "improved"
                entry["speedup"] = float(imp.group(1))
                entry["cost_original"] = float(imp.group(2))
                entry["cost_optimized"] = float(imp.group(3))
                entry["time_ms"] = int(imp.group(4))
                seen[query_id] = entry
                last_line = line.strip()
                continue

            # Parse no improvement
            no_imp = re.search(
                r"no improvement\s+\((\d+)\s+candidates?,\s+(\d+)\s+verified\)\s+\[(\d+)ms\]",
                rest,
            )
            if no_imp:
                entry["status"] = "no_improvement"
                entry["speedup"] = 1.0
                entry["total_candidates"] = int(no_imp.group(1))
                entry["n_verified"] = int(no_imp.group(2))
                entry["time_ms"] = int(no_imp.group(3))
                seen[query_id] = entry
                last_line = line.strip()
                continue

            # Parse ERROR
            err = re.search(r"ERROR:\s*(.*)", rest)
            if err:
                entry["status"] = "error"
                entry["speedup"] = 1.0
                entry["error"] = err.group(1).strip()
                entry["time_ms"] = 0
                seen[query_id] = entry
                last_line = line.strip()
                continue

            # Structured PROGRESS: status=IMPROVED|NO_IMPROVEMENT|ERROR
            st = re.search(r"status=(\w+)", rest)
            if st:
                status_raw = st.group(1).upper()
                if status_raw == "IMPROVED":
                    entry["status"] = "improved"
                elif status_raw == "NO_IMPROVEMENT":
                    entry["status"] = "no_improvement"
                elif status_raw == "ERROR":
                    entry["status"] = "error"
                else:
                    entry["status"] = status_raw.lower()

                sp = re.search(r"speedup=([\d.]+)", rest)
                entry["speedup"] = float(sp.group(1)) if sp else 1.0

                tm = re.search(r"time_ms=(\d+)", rest)
                entry["time_ms"] = int(tm.group(1)) if tm else 0

                seen[query_id] = entry
                last_line = line.strip()

    entries = list(seen.values())
    return {
        "benchmark": "job-complex",
        "entries": entries,
        "total": len(entries),
        "target": total_target,
        "rejection_counts": rejection_counts,
        "last_line": last_line,
    }


def parse_job_results_dir(results_dir: str) -> dict:
    """Parse a saved JOB-Complex results JSON file."""
    results_path = Path(results_dir) / "results.json"
    if not results_path.exists():
        # Try the path itself as JSON
        results_path = Path(results_dir)
        if not results_path.exists() or results_path.suffix != ".json":
            print(f"No results.json in {results_dir}")
            sys.exit(1)

    data = json.load(open(results_path))

    # Handle wrapped format: {"results": [...], ...}
    if isinstance(data, dict) and "results" in data:
        result_list = data["results"]
    elif isinstance(data, list):
        result_list = data
    else:
        print(f"Unexpected format in {results_path}")
        sys.exit(1)

    entries = []
    rejection_counts: Counter = Counter()

    for i, r in enumerate(result_list, 1):
        entry: dict = {
            "id": r.get("id", f"Q{i:03d}"),
            "index": i,
            "speedup": r.get("speedup", 1.0),
            "time_ms": r.get("total_time_ms", 0),
            "cost_original": r.get("cost_original"),
            "cost_optimized": r.get("cost_optimized"),
            "total_candidates": r.get("total_candidates", 0),
            "n_verified": r.get("n_verified", 0),
            "n_rejected": r.get("n_rejected", 0),
        }

        if r.get("error"):
            entry["status"] = "error"
            entry["error"] = r["error"]
        elif r.get("improved"):
            entry["status"] = "improved"
        else:
            entry["status"] = "no_improvement"

        # Collect rejection reasons if present
        for reason, count in r.get("rejection_reasons", {}).items():
            rejection_counts[reason] += count

        entries.append(entry)

    return {
        "benchmark": "job-complex",
        "entries": entries,
        "total": len(entries),
        "target": len(entries),
        "rejection_counts": rejection_counts,
        "last_line": "(from saved results)",
    }


def display_job(data: dict) -> None:
    """Display JOB-Complex progress summary and per-query breakdown."""
    entries = data["entries"]
    total = data["total"]
    target = data["target"] or total
    rejection_counts = data["rejection_counts"]

    n_improved = sum(1 for e in entries if e["status"] == "improved")
    n_errors = sum(1 for e in entries if e["status"] == "error")
    n_no_improvement = sum(1 for e in entries if e["status"] == "no_improvement")
    speedups = [e["speedup"] for e in entries if e["status"] == "improved"]
    avg_speedup = sum(speedups) / len(speedups) if speedups else 0.0
    times = [e.get("time_ms", 0) for e in entries if e.get("time_ms", 0) > 0]
    avg_time = sum(times) / len(times) if times else 0.0

    pct = lambda n, d: f"{n/d*100:.1f}%" if d > 0 else "N/A"

    # --- Overall progress ---
    print("=" * 70)
    print(f"  Progress: {total} / {target}")
    print("=" * 70)

    print(f"\n  {'Metric':<30} {'Value':>16}")
    print(f"  {'-'*30} {'-'*16}")
    print(f"  {'Improved':<30} {f'{n_improved} ({pct(n_improved, total)})':>16}")
    print(f"  {'No improvement':<30} {f'{n_no_improvement}':>16}")
    print(f"  {'Errors':<30} {f'{n_errors}':>16}")
    if speedups:
        print(f"  {'Avg speedup (improved)':<30} {f'{avg_speedup:.2f}×':>16}")
        print(f"  {'Max speedup':<30} {f'{max(speedups):.2f}×':>16}")
    if times:
        print(f"  {'Avg time per query':<30} {f'{avg_time:.0f}ms':>16}")

    # --- Per-query breakdown ---
    print(f"\n  {'#':<4} {'Query':<12} {'Status':<16} {'Speedup':>8} "
          f"{'Cost':>20} {'Time':>8}")
    print(f"  {'-'*4} {'-'*12} {'-'*16} {'-'*8} {'-'*20} {'-'*8}")

    for e in sorted(entries, key=lambda x: x["index"]):
        status_str = e["status"].upper() if e["status"] == "improved" else e["status"]
        speedup_str = f"{e['speedup']:.2f}×" if e["status"] == "improved" else "-"

        cost_orig = e.get("cost_original")
        cost_opt = e.get("cost_optimized")
        if cost_orig is not None and cost_opt is not None:
            cost_str = f"{cost_orig:.1f}→{cost_opt:.1f}"
        else:
            cost_str = "-"

        time_str = f"{e.get('time_ms', 0)}ms" if e.get("time_ms", 0) > 0 else "-"

        print(f"  {e['index']:<4} {e['id']:<12} {status_str:<16} {speedup_str:>8} "
              f"{cost_str:>20} {time_str:>8}")

    # --- Rejection reason breakdown ---
    if rejection_counts:
        print(f"\n  Rejection Reasons")
        print(f"  {'-'*40}")
        for reason in REJECTION_REASONS:
            count = rejection_counts.get(reason, 0)
            if count > 0:
                print(f"  {reason:<35} {count:>5}")
        # Any reasons not in our known list
        for reason, count in rejection_counts.most_common():
            if reason not in REJECTION_REASONS:
                print(f"  {reason:<35} {count:>5}")

    if data["last_line"] and data["last_line"] != "(from saved results)":
        print(f"\n  Last: {data['last_line'][:200]}")


# ---------------------------------------------------------------------------
# SQLStorm log parsing
# ---------------------------------------------------------------------------

def parse_sqlstorm_log(log_path: str) -> dict:
    """Parse a SQLStorm eval log file to extract progress."""
    entries: list[dict] = []
    seen_indices: set[int] = set()
    total_target = None
    last_line = ""

    with open(log_path) as f:
        for line in f:
            m = re.search(
                r"PROGRESS\s+\[(\d+)/(\d+)\]\s+pair_index=(\d+):\s+(.*)", line
            )
            if not m:
                continue

            idx, target, pair_index, rest = m.groups()
            total_target = int(target)
            pair_idx = int(pair_index)

            if pair_idx in seen_indices:
                continue
            seen_indices.add(pair_idx)

            entry: dict = {"index": pair_idx}

            st = re.search(r"status=(\w+)", rest)
            entry["our_result"] = st.group(1) if st else "UNKNOWN"

            tm = re.search(r"time_ms=([\d.]+)", rest)
            entry["time_ms"] = float(tm.group(1)) if tm else 0.0

            entry["parse_ok"] = entry["our_result"] != "PARSE_FAIL"

            entries.append(entry)
            last_line = line.strip()

    return {
        "benchmark": "sqlstorm",
        "entries": entries,
        "total": len(entries),
        "target": total_target,
        "last_line": last_line,
    }


def parse_sqlstorm_results_dir(results_dir: str) -> dict:
    """Parse a saved SQLStorm results directory."""
    rdir = Path(results_dir)

    summary_path = rdir / "summary.json"
    summary: dict | None = None
    if summary_path.exists():
        summary = json.load(open(summary_path))

    entries: list[dict] = []
    results_json = rdir / "results.json"

    if results_json.exists():
        data = json.load(open(results_json))
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict) and "results" in data:
            entries = data["results"]

    if not entries and summary:
        return {
            "benchmark": "sqlstorm",
            "entries": [],
            "total": summary.get("total_pairs", 0),
            "target": summary.get("total_pairs", 0),
            "summary": summary,
            "last_line": "(from saved results)",
        }

    return {
        "benchmark": "sqlstorm",
        "entries": entries,
        "total": len(entries),
        "target": summary.get("total_pairs", len(entries)) if summary else len(entries),
        "summary": summary,
        "last_line": "(from saved results)",
    }


def _sqlstorm_stats(data: dict) -> dict:
    """Compute SQLStorm metrics from entries or summary."""
    entries = data["entries"]
    summary = data.get("summary")
    total = data["total"]
    target = data["target"] or total

    if summary and not entries:
        return {
            "total": total,
            "target": target,
            "parsed": summary.get("parsed", total),
            "n_equ": summary.get("our_equ", 0),
            "n_neq": summary.get("our_neq", 0),
            "n_unknown": summary.get("our_unknown", 0),
            "n_tmo": summary.get("our_tmo", 0),
            "n_parse_fail": summary.get("our_parse_fail", 0),
            "n_error": summary.get("our_error", 0),
            "mean_time_ms": summary.get("mean_time_ms", 0.0),
            "median_time_ms": summary.get("median_time_ms", 0.0),
            "p95_time_ms": summary.get("p95_time_ms", 0.0),
            "max_time_ms": summary.get("max_time_ms", 0.0),
            "total_time_s": summary.get("total_time_s", 0.0),
            "dataset": summary.get("dataset", "?"),
        }

    n_parsed = sum(1 for e in entries if e.get("parse_ok", True))
    status_counts: Counter = Counter()
    times: list[float] = []

    for e in entries:
        our = e.get("our_result", "UNKNOWN")
        status_counts[our] += 1
        t = e.get("time_ms", 0.0)
        if t > 0:
            times.append(t)

    mean_time = sum(times) / len(times) if times else 0.0
    sorted_times = sorted(times)
    median_time = sorted_times[len(sorted_times) // 2] if sorted_times else 0.0
    p95_idx = min(int(len(sorted_times) * 0.95), len(sorted_times) - 1) if sorted_times else 0
    p95_time = sorted_times[p95_idx] if sorted_times else 0.0
    max_time = max(sorted_times) if sorted_times else 0.0
    total_time_s = sum(times) / 1000.0

    return {
        "total": total,
        "target": target,
        "parsed": n_parsed,
        "n_equ": status_counts.get("EQU", 0),
        "n_neq": status_counts.get("NEQ", 0),
        "n_unknown": status_counts.get("UNKNOWN", 0),
        "n_tmo": status_counts.get("TMO", 0),
        "n_parse_fail": status_counts.get("PARSE_FAIL", 0),
        "n_error": status_counts.get("ERROR", 0),
        "mean_time_ms": mean_time,
        "median_time_ms": median_time,
        "p95_time_ms": p95_time,
        "max_time_ms": max_time,
        "total_time_s": total_time_s,
        "dataset": summary.get("dataset", "?") if summary else "?",
    }


def display_sqlstorm(data: dict) -> None:
    """Display SQLStorm progress summary."""
    s = _sqlstorm_stats(data)
    total = s["total"]
    target = s["target"]

    pct = lambda n, d: f"{n/d*100:.1f}%" if d > 0 else "N/A"

    print("=" * 64)
    print(f"  SQLStorm Benchmark Progress ({s['dataset']}): {total} / {target}")
    print("=" * 64)

    print(f"\n  {'Metric':<36} {'Value':>16}")
    print(f"  {'-'*36} {'-'*16}")
    print(f"  {'Parsed':<36} {f'{s['parsed']} ({pct(s['parsed'], total)})':>16}")
    print(f"  {'EQU':<36} {f'{s['n_equ']} ({pct(s['n_equ'], total)})':>16}")
    print(f"  {'NEQ (validated)':<36} {f'{s['n_neq']} ({pct(s['n_neq'], total)})':>16}")
    print(f"  {'UNKNOWN':<36} {f'{s['n_unknown']} ({pct(s['n_unknown'], total)})':>16}")
    print(f"  {'TMO':<36} {f'{s['n_tmo']} ({pct(s['n_tmo'], total)})':>16}")
    print(f"  {'PARSE_FAIL':<36} {f'{s['n_parse_fail']} ({pct(s['n_parse_fail'], total)})':>16}")
    if s["n_error"] > 0:
        print(f"  {'ERROR':<36} {f'{s['n_error']} ({pct(s['n_error'], total)})':>16}")

    print(f"\n  Timing")
    print(f"  {'-'*36} {'-'*16}")
    if s["mean_time_ms"] > 0:
        print(f"  {'Mean time per pair':<36} {f'{s['mean_time_ms']:.0f}ms':>16}")
    if s["median_time_ms"] > 0:
        print(f"  {'Median time per pair':<36} {f'{s['median_time_ms']:.0f}ms':>16}")
    if s["p95_time_ms"] > 0:
        print(f"  {'P95 time per pair':<36} {f'{s['p95_time_ms']:.0f}ms':>16}")
    if s["max_time_ms"] > 0:
        print(f"  {'Max time per pair':<36} {f'{s['max_time_ms']:.0f}ms':>16}")
    if s["total_time_s"] > 0:
        print(f"  {'Total elapsed':<36} {f'{s['total_time_s']:.0f}s':>16}")

    if data["last_line"] and data["last_line"] != "(from saved results)":
        print(f"\n  Last: {data['last_line'][:200]}")


# ---------------------------------------------------------------------------
# VeriEQL log parsing
# ---------------------------------------------------------------------------

def parse_verieql_log(log_path: str) -> dict:
    """Parse a VeriEQL eval log file to extract progress."""
    entries: list[dict] = []
    seen_indices: set[int] = set()
    total_target = None
    last_line = ""

    with open(log_path) as f:
        for line in f:
            # Match PROGRESS lines:
            # PROGRESS [1/397] pair_index=0: status=EQU vs=EQU comparison=AGREE time_ms=123.4 agree=1/1 (100%)
            m = re.search(
                r"PROGRESS\s+\[(\d+)/(\d+)\]\s+pair_index=(\d+):\s+(.*)", line
            )
            if not m:
                continue

            idx, target, pair_index, rest = m.groups()
            total_target = int(target)
            pair_idx = int(pair_index)

            if pair_idx in seen_indices:
                continue
            seen_indices.add(pair_idx)

            entry: dict = {"index": pair_idx}

            # Parse fields from rest
            st = re.search(r"status=(\w+)", rest)
            entry["our_result"] = st.group(1) if st else "UNKNOWN"

            vs = re.search(r"vs=(\w+)", rest)
            entry["verieql_result"] = vs.group(1) if vs else None

            cmp = re.search(r"comparison=(\w+)", rest)
            entry["comparison"] = cmp.group(1) if cmp else None

            tm = re.search(r"time_ms=([\d.]+)", rest)
            entry["time_ms"] = float(tm.group(1)) if tm else 0.0

            entry["parse_ok"] = entry["our_result"] != "PARSE_FAIL"

            entries.append(entry)
            last_line = line.strip()

    return {
        "benchmark": "verieql",
        "entries": entries,
        "total": len(entries),
        "target": total_target,
        "last_line": last_line,
    }


def parse_verieql_results_dir(results_dir: str) -> dict:
    """Parse a saved VeriEQL results directory (results.jsonl + summary.json)."""
    rdir = Path(results_dir)

    # Load summary if present
    summary_path = rdir / "summary.json"
    summary: dict | None = None
    if summary_path.exists():
        summary = json.load(open(summary_path))

    # Load per-pair results (results.jsonl or results.json)
    entries: list[dict] = []
    results_jsonl = rdir / "results.jsonl"
    results_json = rdir / "results.json"

    if results_jsonl.exists():
        with open(results_jsonl) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    elif results_json.exists():
        data = json.load(open(results_json))
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict) and "results" in data:
            entries = data["results"]

    if not entries and summary:
        # No per-pair data, synthesize from summary only
        return {
            "benchmark": "verieql",
            "entries": [],
            "total": summary.get("total_pairs", 0),
            "target": summary.get("total_pairs", 0),
            "summary": summary,
            "last_line": "(from saved results)",
        }

    return {
        "benchmark": "verieql",
        "entries": entries,
        "total": len(entries),
        "target": summary.get("total_pairs", len(entries)) if summary else len(entries),
        "summary": summary,
        "last_line": "(from saved results)",
    }


def _verieql_stats(data: dict) -> dict:
    """Compute VeriEQL metrics from entries or summary."""
    entries = data["entries"]
    summary = data.get("summary")
    total = data["total"]
    target = data["target"] or total

    # If we have a pre-computed summary and no entries, use the summary directly
    if summary and not entries:
        return {
            "total": total,
            "target": target,
            "parsed": summary.get("parsed", total),
            "n_equ": summary.get("our_equ", 0),
            "n_neq": summary.get("our_neq", 0),
            "n_unknown": summary.get("our_unknown", 0),
            "n_tmo": summary.get("our_tmo", 0),
            "n_parse_fail": summary.get("our_parse_fail", 0),
            "n_agree": summary.get("agree", 0),
            "n_disagree": summary.get("disagree", 0),
            "n_both_equ": summary.get("both_equ", summary.get("agree", 0)),
            "n_both_neq": summary.get("both_neq", 0),
            "n_our_neq_vs_vq_equ": summary.get("our_neq_vs_vq_equ", summary.get("disagree", 0)),
            "n_our_stronger": summary.get("our_stronger", 0),
            "n_our_weaker": summary.get("our_weaker", 0),
            "n_unk_vs_vq_equ": summary.get("our_unk_vs_vq_equ", 0),
            "n_unk_vs_vq_neq": summary.get("our_unk_vs_vq_neq", 0),
            "n_tmo_vs_vq_equ": summary.get("our_tmo_vs_vq_equ", 0),
            "false_neq": 0,
            "false_neq_denom": 0,
            "nse_handled": summary.get("nse_handled", 0),
            "err_handled": 0,
            "tmo_handled": 0,
            "mean_time_ms": 0.0,
            "total_time_s": summary.get("total_time_s", 0.0),
            "agreement_rate": summary.get("agreement_rate", 0.0),
        }

    # Compute from entries
    n_parsed = sum(1 for e in entries if e.get("parse_ok", True))
    status_counts: Counter = Counter()
    comparison_counts: Counter = Counter()
    times: list[float] = []
    false_neq = 0
    nse_handled = 0
    err_handled = 0
    tmo_handled = 0

    for e in entries:
        our = e.get("our_result", "UNKNOWN")
        status_counts[our] += 1

        cmp = e.get("comparison")
        if cmp:
            comparison_counts[cmp] += 1

        t = e.get("time_ms", 0.0)
        if t > 0:
            times.append(t)

        vq = e.get("verieql_result", "")

        # False NEQ: we said NEQ but VeriEQL said EQU
        if our == "NEQ" and vq == "EQU":
            false_neq += 1

        # Novelty: VeriEQL couldn't handle but we decided
        if vq == "NSE" and our in ("EQU", "NEQ"):
            nse_handled += 1
        if vq == "ERR" and our in ("EQU", "NEQ"):
            err_handled += 1
        if vq == "TMO" and our in ("EQU", "NEQ"):
            tmo_handled += 1

    n_agree = comparison_counts.get("AGREE", 0)
    n_disagree = comparison_counts.get("DISAGREE", 0)
    decided = n_agree + n_disagree
    agreement_rate = n_agree / decided * 100 if decided > 0 else 0.0

    # False NEQ denominator: pairs where VeriEQL said EQU
    false_neq_denom = sum(
        1 for e in entries if e.get("verieql_result") == "EQU"
    )

    mean_time = sum(times) / len(times) if times else 0.0
    total_time_s = sum(times) / 1000.0

    # Precise weaker-case breakdown
    n_unk_vs_vq_equ = sum(1 for e in entries if e.get("our_result") == "UNKNOWN" and e.get("verieql_result") == "EQU")
    n_unk_vs_vq_neq = sum(1 for e in entries if e.get("our_result") == "UNKNOWN" and e.get("verieql_result") == "NEQ")
    n_tmo_vs_vq_equ = sum(1 for e in entries if e.get("our_result") == "TMO" and e.get("verieql_result") == "EQU")
    n_tmo_vs_vq_neq = sum(1 for e in entries if e.get("our_result") == "TMO" and e.get("verieql_result") == "NEQ")

    # Precise disagree breakdown
    n_our_neq_vs_vq_equ = sum(1 for e in entries if e.get("our_result") == "NEQ" and e.get("verieql_result") == "EQU")
    # Precise agree: both EQU / both NEQ
    n_both_equ = sum(1 for e in entries if e.get("our_result") == "EQU" and e.get("verieql_result") == "EQU")
    n_both_neq = sum(1 for e in entries if e.get("our_result") == "NEQ" and e.get("verieql_result") == "NEQ")

    return {
        "total": total,
        "target": target,
        "parsed": n_parsed,
        "n_equ": status_counts.get("EQU", 0),
        "n_neq": status_counts.get("NEQ", 0),
        "n_unknown": status_counts.get("UNKNOWN", 0),
        "n_tmo": status_counts.get("TMO", 0),
        "n_parse_fail": status_counts.get("PARSE_FAIL", 0),
        "n_agree": n_agree,
        "n_disagree": n_disagree,
        "n_both_equ": n_both_equ,
        "n_both_neq": n_both_neq,
        "n_our_neq_vs_vq_equ": n_our_neq_vs_vq_equ,
        "n_our_stronger": comparison_counts.get("OUR_STRONGER", 0),
        "n_our_weaker": comparison_counts.get("OUR_WEAKER", 0),
        "n_unk_vs_vq_equ": n_unk_vs_vq_equ,
        "n_unk_vs_vq_neq": n_unk_vs_vq_neq,
        "n_tmo_vs_vq_equ": n_tmo_vs_vq_equ,
        "false_neq": false_neq,
        "false_neq_denom": false_neq_denom,
        "nse_handled": nse_handled,
        "err_handled": err_handled,
        "tmo_handled": tmo_handled,
        "mean_time_ms": mean_time,
        "total_time_s": total_time_s,
        "agreement_rate": agreement_rate,
    }


def display_verieql(data: dict) -> None:
    """Display VeriEQL progress summary."""
    s = _verieql_stats(data)
    total = s["total"]
    target = s["target"]

    pct = lambda n, d: f"{n/d*100:.1f}%" if d > 0 else "N/A"

    print("=" * 64)
    print(f"  VeriEQL Benchmark Progress: {total} / {target}")
    print("=" * 64)

    print(f"\n  {'Metric':<36} {'Value':>16}")
    print(f"  {'-'*36} {'-'*16}")
    print(f"  {'Parsed':<36} {f'{s['parsed']} ({pct(s['parsed'], total)})':>16}")
    print(f"  {'EQU':<36} {f'{s['n_equ']} ({pct(s['n_equ'], total)})':>16}")
    print(f"  {'NEQ (validated)':<36} {f'{s['n_neq']} ({pct(s['n_neq'], total)})':>16}")
    print(f"  {'UNKNOWN':<36} {f'{s['n_unknown']} ({pct(s['n_unknown'], total)})':>16}")
    print(f"  {'TMO':<36} {f'{s['n_tmo']} ({pct(s['n_tmo'], total)})':>16}")
    print(f"  {'PARSE_FAIL':<36} {f'{s['n_parse_fail']} ({pct(s['n_parse_fail'], total)})':>16}")
    print(f"  ---")

    # Cross-comparison (validated — neither system assumed ground truth)
    # EQU = UNSAT proof, NEQ = witness confirmed by DuckDB+SQLite
    print(f"  {'Both proved EQU':<36} {s['n_both_equ']:>16}")
    print(f"  {'Both proved NEQ':<36} {s['n_both_neq']:>16}")
    print(f"  {'Our NEQ vs VQ EQU':<36} {s['n_our_neq_vs_vq_equ']:>16}")
    print(f"  {'Our UNK vs VQ EQU':<36} {s['n_unk_vs_vq_equ']:>16}")
    print(f"  {'Our TMO vs VQ EQU':<36} {s['n_tmo_vs_vq_equ']:>16}")
    if s['n_unk_vs_vq_neq'] > 0 or s.get('n_tmo_vs_vq_neq', 0) > 0:
        print(f"  {'Our UNK vs VQ NEQ':<36} {s['n_unk_vs_vq_neq']:>16}")
    label_decided = "We decided, VQ couldn't"
    print(f"  {label_decided:<36} {s['n_our_stronger']:>16}")

    # Timing
    print(f"\n  Timing")
    print(f"  {'-'*36} {'-'*16}")
    if s["mean_time_ms"] > 0:
        print(f"  {'Mean time per pair':<36} {f'{s['mean_time_ms']:.0f}ms':>16}")
    if s["total_time_s"] > 0:
        print(f"  {'Total elapsed':<36} {f'{s['total_time_s']:.0f}s':>16}")

    # Novelty coverage
    print(f"\n  Novelty Coverage")
    print(f"  {'-'*36} {'-'*16}")
    print(f"  {'VeriEQL NSE → we decided':<36} {s['nse_handled']:>16}")
    print(f"  {'VeriEQL ERR → we decided':<36} {s['err_handled']:>16}")
    print(f"  {'VeriEQL TMO → we decided':<36} {s['tmo_handled']:>16}")

    if data["last_line"] and data["last_line"] != "(from saved results)":
        print(f"\n  Last: {data['last_line'][:200]}")


# ---------------------------------------------------------------------------
# Auto-find latest log
# ---------------------------------------------------------------------------

def find_latest_log() -> str | None:
    """Find the most recent eval log in logs/."""
    logs_dir = Path("logs")
    if not logs_dir.exists():
        return None

    # Try eval_*.log first, then verieql_*.log, then any *.log
    for pattern in ("eval_*.log", "verieql_*.log", "*.log"):
        logs = sorted(
            logs_dir.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if logs:
            return str(logs[0])

    return None


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = find_latest_log()
        if not path:
            print("No log files found in logs/")
            sys.exit(1)
        print(f"Using: {path}\n")

    p = Path(path)

    if p.is_dir():
        # Results directory
        benchmark = _detect_benchmark_from_results_dir(p)
        if benchmark == "verieql":
            data = parse_verieql_results_dir(path)
        elif benchmark == "sqlstorm":
            data = parse_sqlstorm_results_dir(path)
        else:
            data = parse_job_results_dir(path)
    elif p.suffix == ".json":
        # Direct summary.json or results.json
        json_data = json.load(open(p))
        benchmark = _detect_benchmark_from_summary(json_data) if isinstance(json_data, dict) else "job-complex"
        if benchmark == "sqlstorm":
            if p.name == "summary.json" and p.parent.is_dir():
                data = parse_sqlstorm_results_dir(str(p.parent))
            else:
                data = {
                    "benchmark": "sqlstorm",
                    "entries": [],
                    "total": json_data.get("total_pairs", 0),
                    "target": json_data.get("total_pairs", 0),
                    "summary": json_data,
                    "last_line": "(from saved results)",
                }
        elif isinstance(json_data, dict) and ("our_equ" in json_data or "suite" in json_data):
            # VeriEQL summary — treat parent dir as results dir
            if p.name == "summary.json" and p.parent.is_dir():
                data = parse_verieql_results_dir(str(p.parent))
            else:
                data = {
                    "benchmark": "verieql",
                    "entries": [],
                    "total": json_data.get("total_pairs", 0),
                    "target": json_data.get("total_pairs", 0),
                    "summary": json_data,
                    "last_line": "(from saved results)",
                }
        else:
            data = parse_job_results_dir(path)
    else:
        # Log file
        benchmark = _detect_benchmark_from_log(path)
        if benchmark == "verieql":
            data = parse_verieql_log(path)
        elif benchmark == "sqlstorm":
            data = parse_sqlstorm_log(path)
        else:
            data = parse_job_log(path)

    if data["total"] == 0:
        print("No progress data found.")
        sys.exit(1)

    if data.get("benchmark") == "verieql":
        display_verieql(data)
    elif data.get("benchmark") == "sqlstorm":
        display_sqlstorm(data)
    else:
        display_job(data)


if __name__ == "__main__":
    main()
