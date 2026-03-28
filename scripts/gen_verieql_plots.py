#!/usr/bin/env python3
"""Generate VeriEQL benchmark figures for the paper.

Usage:
    python3 scripts/gen_verieql_plots.py          # uses default result dirs
    python3 scripts/gen_verieql_plots.py paper/figures/

Produces a single PDF with two panels:
  (left)  Decided-pair counts per suite × system (seaborn catplot style)
  (right) Solve-time ECDF per suite
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib import rc
from matplotlib.patches import Patch

# ── Style ─────────────────────────────────────────────────────────
dpi = 300
rc("text", usetex=True)
sns.set_context("paper", font_scale=1.05)
sns.set_style("white")
plt.rcParams["font.family"] = "serif"

# ── Palette (Spectral 5-class, middle 3) ──────────────────────────
PAL = sns.color_palette("Spectral", n_colors=7)
C_EQU   = PAL[0]    # cool blue
C_NEQ   = PAL[6]    # warm red
C_UNDEC = "#e8e8e8"  # neutral light grey

RESULT_DIRS = {
    "Calcite": "results/verieql_calcite",
    "Literature": "results/verieql_literature",
    "LeetCode": "results/verieql_leetcode",
}


def load_suite(result_dir: str) -> dict:
    p = Path(result_dir)
    summary = json.loads((p / "summary.json").read_text())
    results = json.loads((p / "results.json").read_text())
    return {"summary": summary, "results": results}


def build_bar_df(suites: dict[str, dict]) -> pd.DataFrame:
    """Build a tidy DataFrame for the stacked bar chart."""
    rows = []
    for name, data in suites.items():
        s = data["summary"]
        total = s["total_pairs"]

        # CertOpt
        rows.append(dict(Suite=name, System=r"\textsc{CertOpt}",
                         Category="EQU", Count=s["our_equ"], Total=total))
        rows.append(dict(Suite=name, System=r"\textsc{CertOpt}",
                         Category="NEQ", Count=s["our_neq"], Total=total))
        undec = s["our_unknown"] + s["our_tmo"] + s.get("our_parse_fail", 0)
        rows.append(dict(Suite=name, System=r"\textsc{CertOpt}",
                         Category="Undecided", Count=undec, Total=total))

        # VeriEQL
        vq = Counter(r.get("verieql_result", "?") for r in data["results"])
        rows.append(dict(Suite=name, System=r"\textsc{VeriEQL}",
                         Category="EQU", Count=vq.get("EQU", 0), Total=total))
        rows.append(dict(Suite=name, System=r"\textsc{VeriEQL}",
                         Category="NEQ", Count=vq.get("NEQ", 0), Total=total))
        v_undec = (vq.get("NSE", 0) + vq.get("ERR", 0)
                   + vq.get("NIE", 0) + vq.get("TMO", 0) + vq.get("?", 0))
        rows.append(dict(Suite=name, System=r"\textsc{VeriEQL}",
                         Category="Undecided", Count=v_undec, Total=total))
    return pd.DataFrame(rows)


def plot_decided_bars(axes: list, suites: dict[str, dict]):
    """One faceted panel per suite — stacked bars via seaborn."""
    df = build_bar_df(suites)
    cat_order = ["EQU", "NEQ", "Undecided"]
    color_map = {"EQU": C_EQU, "NEQ": C_NEQ, "Undecided": C_UNDEC}
    sys_order = [r"\textsc{CertOpt}", r"\textsc{VeriEQL}"]

    for ax, (suite_name, sdf) in zip(axes, df.groupby("Suite", sort=False)):
        total = sdf["Total"].iloc[0]

        # Pivot to get stacking data
        pivot = sdf.pivot(index="System", columns="Category",
                          values="Count").reindex(sys_order)[cat_order]

        # Draw stacked bars using seaborn-styled axes
        bottom = np.zeros(len(sys_order))
        x = np.arange(len(sys_order))
        w = 0.50
        for cat in cat_order:
            vals = pivot[cat].values.astype(float)
            ax.bar(x, vals, w, bottom=bottom, color=color_map[cat],
                   edgecolor="white", linewidth=0.4, zorder=3)
            bottom += vals

        # Decided-count annotations
        for i, sys_name in enumerate(sys_order):
            row = pivot.loc[sys_name]
            decided = int(row["EQU"] + row["NEQ"])
            ax.text(i, decided + total * 0.025,
                    f"\\textsf{{{decided}/{total}}}",
                    ha="center", va="bottom", fontsize=4.5, color="0.35")

        ax.set_xticks(x)
        ax.set_xticklabels(sys_order, fontsize=7)
        ax.set_ylim(0, total * 1.15)
        ax.set_ylabel("Pairs", fontsize=7.5)
        ax.set_title(suite_name, fontsize=9, pad=5)
        ax.tick_params(axis="y", labelsize=7)
        sns.despine(ax=ax, left=False)


def plot_time_cdf_single(ax, name: str, data: dict, color: str):
    """ECDF of per-pair solve time for a single suite."""
    times = [r.get("time_ms", 0) / 1000 for r in data["results"]
             if r.get("time_ms", 0) > 0]
    if not times:
        return

    sorted_t = np.sort(times)
    ecdf = np.arange(1, len(sorted_t) + 1) / len(sorted_t)
    ax.plot(sorted_t, ecdf, color=color, linewidth=1.4)

    ax.set_xscale("log")
    ax.set_xlabel("Time (s)", fontsize=7.5)
    ax.set_ylabel("CDF", fontsize=7.5)
    ax.set_xlim(1e-3, max(sorted_t[-1] * 1.5, 1))
    ax.set_ylim(0, 1.02)
    ax.axhline(0.5, color="0.75", linestyle="--", linewidth=0.5, zorder=1)
    ax.axhline(0.95, color="0.75", linestyle=":", linewidth=0.5, zorder=1)

    # Annotate median and p95
    median_t = np.median(times)
    p95_t = np.percentile(times, 95)
    ax.plot(median_t, 0.5, "o", color=color, markersize=3, zorder=5)
    ax.plot(p95_t, 0.95, "s", color=color, markersize=3, zorder=5)
    ax.text(median_t * 1.8, 0.46, f"{median_t:.2f}s",
            fontsize=5.5, color="0.4", ha="left")
    ax.text(p95_t * 1.8, 0.91, f"{p95_t:.1f}s",
            fontsize=5.5, color="0.4", ha="left")

    ax.tick_params(axis="both", labelsize=7)
    ax.set_title(f"Solve time", fontsize=8, pad=4)
    sns.despine(ax=ax, left=False)


def main():
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("paper/figures")
    outdir.mkdir(parents=True, exist_ok=True)

    suites: dict[str, dict] = {}
    for name, rdir in RESULT_DIRS.items():
        p = Path(rdir)
        if (p / "summary.json").exists() and (p / "results.json").exists():
            suites[name] = load_suite(rdir)
            print(f"  Loaded {name}: {suites[name]['summary']['total_pairs']} pairs")
        else:
            print(f"  Skipping {name} (not found at {rdir})")

    if not suites:
        print("No results found.")
        sys.exit(1)

    suite_names = list(suites.keys())
    n_suites = len(suite_names)

    # Distinct colors per suite for CDF lines
    cdf_colors = ["#2166ac", "#b2182b", "#1b7837"]  # blue, red, dark green

    fig = plt.figure(figsize=(7.0, 4.0))
    gs = gridspec.GridSpec(2, n_suites, figure=fig,
                           height_ratios=[1.1, 1],
                           hspace=0.50, wspace=0.45)

    # Top row: bar charts
    bar_axes = [fig.add_subplot(gs[0, i]) for i in range(n_suites)]
    plot_decided_bars(bar_axes, suites)

    # Bottom row: per-suite CDF, each below its bar chart
    for i, name in enumerate(suite_names):
        ax_cdf = fig.add_subplot(gs[1, i])
        plot_time_cdf_single(ax_cdf, name, suites[name],
                             cdf_colors[i % len(cdf_colors)])
        if i > 0:
            ax_cdf.set_ylabel("")  # only leftmost gets y-label

    # Shared legend for bar charts — top center
    legend_elements = [
        Patch(facecolor=C_EQU, edgecolor="white", linewidth=0.6, label="EQU"),
        Patch(facecolor=C_NEQ, edgecolor="white", linewidth=0.6, label="NEQ"),
        Patch(facecolor=C_UNDEC, edgecolor="white", linewidth=0.4,
              label="Undecided"),
    ]
    fig.legend(
        handles=legend_elements, loc="lower center",
        bbox_to_anchor=(0.5, 0.96), ncol=3,
        fontsize=6.5, frameon=False,
        handlelength=1.0, handletextpad=0.3, columnspacing=1.2,
    )
    fig.subplots_adjust(top=0.92)

    out_path = outdir / "fig_verieql_results.pdf"
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  ✓ {out_path}")


if __name__ == "__main__":
    main()
