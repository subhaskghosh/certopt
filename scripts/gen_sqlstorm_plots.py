#!/usr/bin/env python3
"""Generate SQLStorm evaluation figure for the paper.

Usage:
    python3 scripts/gen_sqlstorm_plots.py
    python3 scripts/gen_sqlstorm_plots.py paper/figures/

Produces a single PDF with three panels (1 row × 3 columns):
  (a) Stacked bar chart — decision rates by dataset
  (b) Stacked bar chart — decision rates by complexity
  (c) Solve-time ECDF per dataset (decided pairs only)
"""
from __future__ import annotations

import json
import sys
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

# ── Palette (Spectral 7-class, endpoints + grey) ─────────────────
PAL = sns.color_palette("Spectral", n_colors=7)
C_EQU   = PAL[0]    # cool blue
C_NEQ   = PAL[6]    # warm red
C_UNDEC = "#e8e8e8"  # neutral light grey

DATASETS = ["tpch", "tpcds", "stackoverflow", "job"]
DISPLAY_NAMES = {
    "tpch": "TPC-H",
    "tpcds": "TPC-DS",
    "stackoverflow": "StackOverflow",
    "job": "JOB",
}
RESULT_DIR_ORIG = "results/sqlstorm_{dataset}"
RESULT_DIR_LLM = "results/sqlstorm_{dataset}_llm"
RESULT_DIR_FULL_ORIG = "results/sqlstorm_full_{dataset}"
RESULT_DIR_FULL_LLM = "results/sqlstorm_full_{dataset}_llm"
SAMPLE_DIR = "scripts/sqlstorm_sample"
FULL_SAMPLE_DIR = "scripts/sqlstorm_full"


def load_results(dataset: str, source: str = "orig") -> list[dict]:
    template = RESULT_DIR_LLM if source == "llm" else RESULT_DIR_ORIG
    p = Path(template.format(dataset=dataset)) / "results.json"
    return json.loads(p.read_text())


def load_summary(dataset: str, source: str = "orig") -> dict:
    template = RESULT_DIR_LLM if source == "llm" else RESULT_DIR_ORIG
    p = Path(template.format(dataset=dataset)) / "summary.json"
    return json.loads(p.read_text())


def load_sample_metadata(dataset: str, source: str = "orig") -> dict[str, dict]:
    """Return a dict keyed by pair_id with metadata from the sample JSONL."""
    suffix = "_llm" if source == "llm" else ""
    p = Path(SAMPLE_DIR) / f"{dataset}{suffix}.jsonl"
    meta = {}
    for line in p.read_text().strip().splitlines():
        rec = json.loads(line)
        meta[str(rec["pair_id"])] = rec
    return meta


def load_merged_results(dataset: str, source: str = "orig") -> list[dict]:
    """Load results from sample + full runs, deduplicating on (sql1, sql2).

    Sample results take priority over full-run results.
    """
    sample_template = RESULT_DIR_LLM if source == "llm" else RESULT_DIR_ORIG
    full_template = RESULT_DIR_FULL_LLM if source == "llm" else RESULT_DIR_FULL_ORIG

    # Start with full-run results (lower priority)
    merged: dict[tuple[str, str], dict] = {}
    full_path = Path(full_template.format(dataset=dataset)) / "results.json"
    if full_path.exists():
        for r in json.loads(full_path.read_text()):
            key = (r["sql1"].strip(), r["sql2"].strip())
            merged[key] = r

    # Override with sample results (higher priority)
    sample_path = Path(sample_template.format(dataset=dataset)) / "results.json"
    if sample_path.exists():
        for r in json.loads(sample_path.read_text()):
            key = (r["sql1"].strip(), r["sql2"].strip())
            merged[key] = r

    return list(merged.values())


def load_merged_metadata(dataset: str, source: str = "orig") -> dict[str, dict]:
    """Load metadata from sample + full JSONL sources, deduplicating on pair_id."""
    suffix = "_llm" if source == "llm" else ""

    meta: dict[str, dict] = {}

    # Full-run metadata first (lower priority)
    full_path = Path(FULL_SAMPLE_DIR) / f"{dataset}{suffix}.jsonl"
    if full_path.exists():
        for line in full_path.read_text().strip().splitlines():
            rec = json.loads(line)
            meta[str(rec["pair_id"])] = rec

    # Sample metadata overrides
    sample_path = Path(SAMPLE_DIR) / f"{dataset}{suffix}.jsonl"
    if sample_path.exists():
        for line in sample_path.read_text().strip().splitlines():
            rec = json.loads(line)
            meta[str(rec["pair_id"])] = rec

    return meta


def build_dataset_bar_df(all_results: dict[str, list[dict]]) -> pd.DataFrame:
    """Tidy DataFrame for the per-dataset stacked bar chart."""
    rows = []
    for ds in DATASETS:
        results = all_results[ds]
        total = len(results)
        equ = sum(1 for r in results if r["our_result"] == "EQU")
        neq = sum(1 for r in results if r["our_result"] == "NEQ")
        undec = total - equ - neq
        name = DISPLAY_NAMES[ds]
        rows.append(dict(Dataset=name, Category="EQU", Count=equ, Total=total))
        rows.append(dict(Dataset=name, Category="NEQ", Count=neq, Total=total))
        rows.append(dict(Dataset=name, Category="Undecided", Count=undec, Total=total))
    return pd.DataFrame(rows)


def build_complexity_bar_df(
    all_results: dict[str, list[dict]],
    all_meta: dict[str, dict[str, dict]],
) -> pd.DataFrame:
    """Tidy DataFrame for the per-complexity stacked bar chart."""
    from collections import Counter

    counts: dict[str, Counter] = {
        "Simple": Counter(),
        "Moderate": Counter(),
        "Complex": Counter(),
    }
    totals: dict[str, int] = {"Simple": 0, "Moderate": 0, "Complex": 0}

    for ds in DATASETS:
        meta = all_meta[ds]
        for r in all_results[ds]:
            pid = str(r["pair_id"])
            m = meta.get(pid)
            if m is None:
                continue
            cplx = m["complexity"].capitalize()
            if cplx not in counts:
                continue
            totals[cplx] += 1
            result = r["our_result"]
            if result == "EQU":
                counts[cplx]["EQU"] += 1
            elif result == "NEQ":
                counts[cplx]["NEQ"] += 1
            else:
                counts[cplx]["Undecided"] += 1

    rows = []
    for cplx in ["Simple", "Moderate", "Complex"]:
        total = totals[cplx]
        rows.append(dict(Complexity=cplx, Category="EQU",
                         Count=counts[cplx]["EQU"], Total=total))
        rows.append(dict(Complexity=cplx, Category="NEQ",
                         Count=counts[cplx]["NEQ"], Total=total))
        rows.append(dict(Complexity=cplx, Category="Undecided",
                         Count=counts[cplx]["Undecided"], Total=total))
    return pd.DataFrame(rows)


def plot_dataset_bars(ax, all_results: dict[str, list[dict]]):
    """Panel (a): stacked bars by dataset."""
    df = build_dataset_bar_df(all_results)
    cat_order = ["EQU", "NEQ", "Undecided"]
    color_map = {"EQU": C_EQU, "NEQ": C_NEQ, "Undecided": C_UNDEC}
    ds_order = [DISPLAY_NAMES[d] for d in DATASETS]

    pivot = df.pivot(index="Dataset", columns="Category",
                     values="Count").reindex(ds_order)[cat_order]
    totals = df.pivot(index="Dataset", columns="Category",
                      values="Total").reindex(ds_order).iloc[:, 0]

    x = np.arange(len(ds_order))
    w = 0.55
    bottom = np.zeros(len(ds_order))
    for cat in cat_order:
        vals = pivot[cat].values.astype(float)
        ax.bar(x, vals, w, bottom=bottom, color=color_map[cat],
               edgecolor="white", linewidth=0.4, zorder=3)
        bottom += vals

    # Annotate decided/total (n/N format, matching Axis 1 style)
    for i, ds_name in enumerate(ds_order):
        row = pivot.loc[ds_name]
        total = int(totals.iloc[i])
        decided = int(row["EQU"] + row["NEQ"])
        ax.text(i, total + max(totals) * 0.02,
                f"\\textsf{{{decided}/{total}}}",
                ha="center", va="bottom", fontsize=4.5, color="0.35")

    ax.set_xticks(x)
    ax.set_xticklabels(ds_order, fontsize=6, rotation=15, ha="right")
    ax.set_ylim(0, max(totals) * 1.18)
    ax.set_ylabel("Pairs", fontsize=7.5)
    ax.set_title("(a) By dataset", fontsize=8, pad=4)
    ax.tick_params(axis="y", labelsize=7)
    sns.despine(ax=ax, left=False)


def plot_complexity_bars(ax, all_results, all_meta):
    """Panel (b): stacked bars by complexity."""
    df = build_complexity_bar_df(all_results, all_meta)
    cat_order = ["EQU", "NEQ", "Undecided"]
    color_map = {"EQU": C_EQU, "NEQ": C_NEQ, "Undecided": C_UNDEC}
    cplx_order = ["Simple", "Moderate", "Complex"]

    pivot = df.pivot(index="Complexity", columns="Category",
                     values="Count").reindex(cplx_order)[cat_order]
    totals = df.pivot(index="Complexity", columns="Category",
                      values="Total").reindex(cplx_order).iloc[:, 0]

    x = np.arange(len(cplx_order))
    w = 0.55
    bottom = np.zeros(len(cplx_order))
    for cat in cat_order:
        vals = pivot[cat].values.astype(float)
        ax.bar(x, vals, w, bottom=bottom, color=color_map[cat],
               edgecolor="white", linewidth=0.4, zorder=3)
        bottom += vals

    # Annotate decided/total (n/N format, matching Axis 1 style)
    for i, cplx in enumerate(cplx_order):
        row = pivot.loc[cplx]
        total = int(totals.iloc[i])
        decided = int(row["EQU"] + row["NEQ"])
        ax.text(i, total + max(totals) * 0.02,
                f"\\textsf{{{decided}/{total}}}",
                ha="center", va="bottom", fontsize=4.5, color="0.35")

    ax.set_xticks(x)
    ax.set_xticklabels(cplx_order, fontsize=6)
    ax.set_ylim(0, max(totals) * 1.18)
    ax.set_ylabel("Pairs", fontsize=7.5)
    ax.set_title("(b) By complexity", fontsize=8, pad=4)
    ax.tick_params(axis="y", labelsize=7)
    sns.despine(ax=ax, left=False)


def plot_time_ecdf(ax, all_results: dict[str, list[dict]]):
    """Panel (c): solve-time ECDF per dataset (decided pairs only)."""
    cdf_colors = ["#2166ac", "#b2182b", "#1b7837", "#762a83"]

    for ds, color in zip(DATASETS, cdf_colors):
        times = [r["time_ms"] / 1000 for r in all_results[ds]
                 if r["our_result"] in ("EQU", "NEQ")
                 and r.get("time_ms", 0) > 0]
        if not times:
            continue

        sorted_t = np.sort(times)
        ecdf = np.arange(1, len(sorted_t) + 1) / len(sorted_t)
        ax.plot(sorted_t, ecdf, color=color, linewidth=1.4,
                label=DISPLAY_NAMES[ds])

    ax.set_xscale("log")
    ax.set_xlabel("Time (s)", fontsize=7.5)
    ax.set_ylabel("CDF", fontsize=7.5)
    ax.set_ylim(0, 1.02)
    ax.axhline(0.5, color="0.75", linestyle="--", linewidth=0.5, zorder=1)
    ax.axhline(0.95, color="0.75", linestyle=":", linewidth=0.5, zorder=1)

    ax.legend(fontsize=5, frameon=False, loc="lower right")
    ax.tick_params(axis="both", labelsize=7)
    ax.set_title("(c) Solve time (decided)", fontsize=8, pad=4)
    sns.despine(ax=ax, left=False)


def _generate_figure(source: str, outdir: Path, suffix: str = ""):
    """Generate the 3-panel figure for a given source."""
    label = "Option 1: orig_vs_rewritten" if source == "orig" else "Option 2: LLM rewrites"
    print(f"\n  Generating figure for {label}")

    template = RESULT_DIR_LLM if source == "llm" else RESULT_DIR_ORIG
    all_results: dict[str, list[dict]] = {}
    all_meta: dict[str, dict[str, dict]] = {}

    for ds in DATASETS:
        sample_rfile = Path(template.format(dataset=ds)) / "results.json"
        full_template = RESULT_DIR_FULL_LLM if source == "llm" else RESULT_DIR_FULL_ORIG
        full_rfile = Path(full_template.format(dataset=ds)) / "results.json"
        if not sample_rfile.exists() and not full_rfile.exists():
            print(f"  Skipping {DISPLAY_NAMES[ds]} (no results found)")
            continue
        all_results[ds] = load_merged_results(ds, source=source)
        all_meta[ds] = load_merged_metadata(ds, source=source)
        n = len(all_results[ds])
        decided = sum(1 for r in all_results[ds]
                      if r["our_result"] in ("EQU", "NEQ"))
        print(f"  Loaded {DISPLAY_NAMES[ds]}: {n} pairs ({decided} decided)")

    if not all_results:
        print("No results found.")
        return

    fig = plt.figure(figsize=(7.0, 2.4))
    gs = gridspec.GridSpec(1, 3, figure=fig,
                           width_ratios=[1, 1, 1.2],
                           wspace=0.42)

    ax_ds   = fig.add_subplot(gs[0, 0])
    ax_cplx = fig.add_subplot(gs[0, 1])
    ax_cdf  = fig.add_subplot(gs[0, 2])

    plot_dataset_bars(ax_ds, all_results)
    plot_complexity_bars(ax_cplx, all_results, all_meta)
    plot_time_ecdf(ax_cdf, all_results)

    # Shared legend for bar charts — top center
    legend_elements = [
        Patch(facecolor=C_EQU, edgecolor="white", linewidth=0.6, label="EQU"),
        Patch(facecolor=C_NEQ, edgecolor="white", linewidth=0.6, label="NEQ"),
        Patch(facecolor=C_UNDEC, edgecolor="white", linewidth=0.4,
              label="Undecided"),
    ]
    fig.legend(
        handles=legend_elements, loc="lower center",
        bbox_to_anchor=(0.35, 0.96), ncol=3,
        fontsize=6.5, frameon=False,
        handlelength=1.0, handletextpad=0.3, columnspacing=1.2,
    )
    fig.subplots_adjust(top=0.88)

    fname = f"sqlstorm_eval{suffix}.pdf"
    out_path = outdir / fname
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  ✓ {out_path}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate SQLStorm evaluation figures.")
    parser.add_argument("outdir", nargs="?", default="paper/figures",
                        help="Output directory for PDF figures")
    parser.add_argument("--llm", action="store_true", help="Generate Option 2 (LLM) figure only")
    parser.add_argument("--both", action="store_true", help="Generate both Option 1 and Option 2 figures")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.both:
        _generate_figure("orig", outdir, suffix="")
        _generate_figure("llm", outdir, suffix="_llm")
    elif args.llm:
        _generate_figure("llm", outdir, suffix="_llm")
    else:
        _generate_figure("orig", outdir, suffix="")


if __name__ == "__main__":
    main()
