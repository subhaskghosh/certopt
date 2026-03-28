#!/usr/bin/env python3
"""Generate ablation study figures for the paper.

Usage:
    python3 scripts/gen_ablation_plots.py
    python3 scripts/gen_ablation_plots.py paper/figures/

Produces a single PDF with two panels (1 row × 2 columns):
  (a) Line plot — decision rate vs k for three VeriEQL suites
  (b) Bar chart — false rejections under three configurations
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import seaborn as sns
from matplotlib import rc
from matplotlib.patches import Patch

# ── Style ─────────────────────────────────────────────────────────
dpi = 300
rc("text", usetex=True)
sns.set_context("paper", font_scale=1.05)
sns.set_style("white")
plt.rcParams["font.family"] = "serif"

# ── Palette ───────────────────────────────────────────────────────
SUITE_COLORS = {
    "Calcite": "#2166ac",
    "Literature": "#1b7837",
    "LeetCode-1K": "#b2182b",
}

CONFIG_COLORS = {
    "Full": "#2166ac",
    r"$-$Validation": "#f4a582",
    r"$-$Constraints": "#b2182b",
}


def load_summary(path: str) -> dict:
    return json.loads(Path(path).read_text())


def main():
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("paper/figures")
    outdir.mkdir(parents=True, exist_ok=True)

    # ── Data: k-sweep ─────────────────────────────────────────────
    ks = [1, 2, 3, 4, 5]
    suites = {
        "Calcite": {"dir_template": "results/abl_k{k}_calcite", "total": 397},
        "Literature": {"dir_template": "results/abl_k{k}_literature", "total": 64},
        "LeetCode-1K": {"dir_template": "results/abl_k{k}_leetcode1k", "total": 1000},
    }

    decision_rates: dict[str, list[float]] = {}
    for suite_name, info in suites.items():
        rates = []
        for k in ks:
            p = Path(info["dir_template"].format(k=k)) / "summary.json"
            if not p.exists():
                print(f"  ⚠ Missing {p}")
                rates.append(0.0)
                continue
            s = load_summary(str(p))
            decided = s["our_equ"] + s["our_neq"]
            total = info["total"]
            rates.append(decided / total * 100)
        decision_rates[suite_name] = rates

    # ── Data: safety ablation ─────────────────────────────────────
    # False rejections = our_neq_vs_vq_equ
    configs = ["Full", r"$-$Validation", r"$-$Constraints"]
    config_dirs = {
        "Full": {
            "Calcite": "results/abl_k3_calcite",
            "Literature": "results/abl_k3_literature",
            "LeetCode": "results/abl_k3_leetcode1k",
        },
        r"$-$Validation": {
            "Calcite": "results/abl_noval_calcite",
            "Literature": "results/abl_noval_literature",
            "LeetCode": "results/abl_noval_leetcode1k",
        },
        r"$-$Constraints": {
            "Calcite": "results/abl_noconstr_calcite",
            "Literature": "results/abl_noconstr_literature",
            "LeetCode": "results/abl_noconstr_leetcode1k",
        },
    }

    fr_per_config: dict[str, int] = {}
    fr_per_suite: dict[str, dict[str, int]] = {}
    for cfg in configs:
        total_fr = 0
        fr_per_suite[cfg] = {}
        for suite, rdir in config_dirs[cfg].items():
            p = Path(rdir) / "summary.json"
            if not p.exists():
                print(f"  ⚠ Missing {p}")
                fr_per_suite[cfg][suite] = 0
                continue
            s = load_summary(str(p))
            fr = s.get("our_neq_vs_vq_equ", 0)
            fr_per_suite[cfg][suite] = fr
            total_fr += fr
        fr_per_config[cfg] = total_fr

    # ── Figure ────────────────────────────────────────────────────
    fig = plt.figure(figsize=(7.0, 2.6))
    gs = gridspec.GridSpec(1, 2, figure=fig,
                           width_ratios=[1.2, 1],
                           wspace=0.35)

    # Panel (a): k-sweep line plot
    ax_k = fig.add_subplot(gs[0, 0])
    markers = {"Calcite": "o", "Literature": "s", "LeetCode-1K": "D"}
    for suite_name, rates in decision_rates.items():
        ax_k.plot(ks, rates, marker=markers[suite_name],
                  color=SUITE_COLORS[suite_name], linewidth=1.4,
                  markersize=4, label=suite_name, zorder=3)

    # Highlight k=3 with a vertical band
    ax_k.axvspan(2.8, 3.2, alpha=0.08, color="0.3", zorder=1)
    ax_k.text(3.0, max(max(r) for r in decision_rates.values()) + 1.5,
              r"$k{=}3$", ha="center", fontsize=6.5, color="0.4")

    ax_k.set_xlabel(r"Instance bound $k$", fontsize=7.5)
    ax_k.set_ylabel("Decision rate (\\%)", fontsize=7.5)
    ax_k.set_xticks(ks)
    ax_k.set_title(r"(a) Decision rate vs.\ instance bound", fontsize=8, pad=4)
    ax_k.legend(fontsize=6, frameon=False, loc="lower left")
    ax_k.tick_params(axis="both", labelsize=7)
    ax_k.set_ylim(50, 100)
    sns.despine(ax=ax_k, left=False)

    # Panel (b): safety ablation bar chart
    ax_fr = fig.add_subplot(gs[0, 1])
    x = np.arange(len(configs))
    w = 0.50
    bar_colors = [CONFIG_COLORS[c] for c in configs]
    vals = [fr_per_config[c] for c in configs]
    bars = ax_fr.bar(x, vals, w, color=bar_colors,
                     edgecolor="white", linewidth=0.4, zorder=3)

    # Annotate values
    for i, (bar, v) in enumerate(zip(bars, vals)):
        ax_fr.text(bar.get_x() + bar.get_width() / 2, v + max(vals) * 0.02,
                   f"\\textsf{{{v}}}",
                   ha="center", va="bottom", fontsize=6, color="0.25")

    ax_fr.set_xticks(x)
    ax_fr.set_xticklabels(configs, fontsize=6.5)
    ax_fr.set_ylabel("False rejections", fontsize=7.5)
    ax_fr.set_title("(b) Safety: false rejections at $k{=}3$", fontsize=8, pad=4)
    ax_fr.tick_params(axis="both", labelsize=7)
    ax_fr.set_ylim(0, max(vals) * 1.18)
    sns.despine(ax=ax_fr, left=False)

    fig.subplots_adjust(top=0.88)

    out_path = outdir / "fig_ablation.pdf"
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  ✓ {out_path}")


if __name__ == "__main__":
    main()
