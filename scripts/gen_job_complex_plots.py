#!/usr/bin/env python3
"""Generate JOB-Complex (Axis 2) evaluation figure for the paper.

Usage:
    python3 scripts/gen_job_complex_plots.py
    python3 scripts/gen_job_complex_plots.py paper/figures/

Produces a single PDF with two panels:
  (left)  Per-query cost reduction bar chart
  (right) Verification time ECDF (improved queries only)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import seaborn as sns
from matplotlib import rc

# ── Style ─────────────────────────────────────────────────────────
dpi = 300
rc("text", usetex=True)
sns.set_context("paper", font_scale=1.05)
sns.set_style("white")
plt.rcParams["font.family"] = "serif"

# ── Palette ───────────────────────────────────────────────────────
C_IMPROVED = "#66c2a5"   # green-ish (Spectral / Set2 style)
C_NOT_IMPROVED = "#e8e8e8"  # light grey

RESULTS_PATH = Path("results/job_complex/results.json")
SQL_PATH = Path("data/JOB-Complex/JOB-Complex/JOB-Complex.sql")


def count_tables(sql: str) -> int:
    """Count table aliases in the FROM/JOIN clause (before WHERE)."""
    upper = sql.upper()
    where_idx = upper.find(" WHERE ")
    from_clause = sql[:where_idx] if where_idx > 0 else sql
    from_idx = from_clause.upper().find(" FROM ")
    table_part = from_clause[from_idx + 6:] if from_idx >= 0 else from_clause
    return len(re.findall(r'\w+\s+AS\s+\w+', table_part, re.IGNORECASE))


def load_data():
    results = json.loads(RESULTS_PATH.read_text())
    sql_lines = SQL_PATH.read_text().strip().splitlines()
    table_counts = {}
    for i, line in enumerate(sql_lines, start=1):
        qid = f"JOB-C{i:02d}"
        table_counts[qid] = count_tables(line)
    return results, table_counts


def plot_cost_reduction(ax, results: list):
    """Left panel: per-query cost reduction bar chart."""
    ids = [r["id"] for r in results]
    short_ids = [r["id"].replace("JOB-", "") for r in results]
    reductions = []
    colors = []
    for r in results:
        c_orig = r["cost_original"]
        c_opt = r["cost_optimized"]
        pct = (c_orig - c_opt) / c_orig * 100 if c_orig > 0 else 0.0
        reductions.append(pct)
        colors.append(C_IMPROVED if r["improved"] else C_NOT_IMPROVED)

    x = np.arange(len(ids))
    bars = ax.bar(x, reductions, width=0.72, color=colors,
                  edgecolor="white", linewidth=0.3, zorder=3)

    # Annotate unimproved queries with a small "×"
    for i, r in enumerate(results):
        if not r["improved"]:
            ax.text(i, 0.3, r"$\times$", ha="center", va="bottom",
                    fontsize=5, color="0.55")

    ax.set_xticks(x)
    ax.set_xticklabels(short_ids, fontsize=4.5, rotation=90)
    ax.set_ylabel(r"Cost reduction (\%)", fontsize=7.5)
    ax.set_title("Cost Reduction per Query", fontsize=8, pad=4)
    ax.set_xlim(-0.6, len(ids) - 0.4)
    ax.set_ylim(0, max(reductions) * 1.15 if max(reductions) > 0 else 1)
    ax.tick_params(axis="y", labelsize=7)
    sns.despine(ax=ax, left=False)


def plot_time_ecdf(ax, results: list):
    """Right panel: verification time ECDF for improved queries."""
    times = [r["total_time_ms"] / 1000 for r in results
             if r["improved"] and r.get("total_time_ms", 0) > 0]
    if not times:
        return

    sorted_t = np.sort(times)
    ecdf = np.arange(1, len(sorted_t) + 1) / len(sorted_t)

    color = "#2166ac"
    ax.plot(sorted_t, ecdf, color=color, linewidth=1.4)

    ax.set_xscale("log")
    ax.set_xlabel("Time (s)", fontsize=7.5)
    ax.set_ylabel("CDF", fontsize=7.5)
    ax.set_xlim(min(sorted_t) * 0.5, max(sorted_t) * 2)
    ax.set_ylim(0, 1.02)
    ax.axhline(0.5, color="0.75", linestyle="--", linewidth=0.5, zorder=1)
    ax.axhline(0.95, color="0.75", linestyle=":", linewidth=0.5, zorder=1)

    # Annotate median and p95
    median_t = np.median(times)
    p95_t = np.percentile(times, 95)
    ax.plot(median_t, 0.5, "o", color=color, markersize=3, zorder=5)
    ax.plot(p95_t, 0.95, "s", color=color, markersize=3, zorder=5)
    ax.text(median_t * 1.8, 0.46, f"{median_t:.1f}s",
            fontsize=5.5, color="0.4", ha="left")
    ax.text(p95_t * 1.8, 0.91, f"{p95_t:.1f}s",
            fontsize=5.5, color="0.4", ha="left")

    ax.tick_params(axis="both", labelsize=7)
    ax.set_title("Verification Time (Improved)", fontsize=8, pad=4)
    sns.despine(ax=ax, left=False)


def main():
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("paper/figures")
    outdir.mkdir(parents=True, exist_ok=True)

    results, table_counts = load_data()
    n_improved = sum(1 for r in results if r["improved"])
    n_total = len(results)
    print(f"  Loaded {n_total} queries ({n_improved} improved, "
          f"{n_total - n_improved} not improved)")

    fig = plt.figure(figsize=(7.0, 2.5))
    gs = gridspec.GridSpec(1, 2, figure=fig,
                           width_ratios=[1.6, 1],
                           wspace=0.35)

    ax_bar = fig.add_subplot(gs[0, 0])
    ax_cdf = fig.add_subplot(gs[0, 1])

    plot_cost_reduction(ax_bar, results)
    plot_time_ecdf(ax_cdf, results)

    out_path = outdir / "fig_job_complex_results.pdf"
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  ✓ {out_path}")


if __name__ == "__main__":
    main()
