"""Centralized configuration for the CEGIS query optimizer.

Feature flags enable ablation studies: disable individual components
to measure their contribution to optimization quality.

Usage:
    config = OptimizerConfig()                    # all features enabled
    config = OptimizerConfig.ablation("no_llm")   # disable LLM rewrites
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .verify.encode_z3 import BoundedScope
from .rewrite.generator import RewriteConfig


@dataclass
class OptimizerConfig:
    """Configuration for the CEGIS optimization pipeline.

    Feature flags control which components are active. All default to True
    (full pipeline). Set to False for ablation experiments.
    """

    # --- Scope ---
    scope: BoundedScope = field(default_factory=lambda: BoundedScope(k_rows=2))
    dialect: str = "postgres"

    # --- Rewrite generation ---
    rewrite_config: RewriteConfig = field(default_factory=RewriteConfig)
    enable_rule_rewrites: bool = True       # algebraic rewrite rules (R1–R6)
    enable_llm_rewrites: bool = False       # LLM-based rewrite candidates
    llm_provider: str = "openai"            # "openai", "anthropic", or "amp"
    llm_model: str = "gpt-4o"               # model name (provider-specific)
    llm_n_candidates: int = 5
    llm_mode: str = "smart"                 # Amp-specific: "smart" or "deep"

    # --- Verification ---
    enable_structural_verify: bool = True    # structural verification before synthesis
    enable_witness_synthesis: bool = True     # Z3 bounded equivalence checking
    validate_witnesses: bool = True          # SQLite validation of SAT witnesses
    enable_family_pruning: bool = True       # prune rewrite families on first witness

    # --- Preprocessing (Phase 7) ---
    enable_preprocessing: bool = True        # predicate promotion + table elimination
    enable_predicate_promotion: bool = True   # WHERE → JOIN ON for implicit joins
    enable_table_elimination: bool = True     # remove redundant FK→PK tables
    enable_adaptive_limits: bool = True       # shape-aware combo limits (64/256/512)

    # --- Compositional verification (Direction D) ---
    enable_compositional: bool = False       # fallback when monolithic exceeds combo limits

    # --- Cost model ---
    cost_model: str = "syntactic"            # "syntactic" or "explain"
    db_path: Optional[str] = None            # required for "explain" cost model

    # --- Output ---
    output_dir: Optional[str] = None         # directory for results/logs
    verbose: bool = False
    trace_smt: bool = False                  # log full SMT formulas

    # --- Evaluation ---
    max_queries: Optional[int] = None        # limit queries for quick runs
    seed: int = 42

    def to_dict(self) -> dict:
        """Serialize config for reproducibility."""
        return {
            "k_rows": self.scope.k_rows,
            "solver_timeout_ms": self.scope.solver_timeout_ms,
            "dialect": self.dialect,
            "enable_rule_rewrites": self.enable_rule_rewrites,
            "enable_llm_rewrites": self.enable_llm_rewrites,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_n_candidates": self.llm_n_candidates,
            "llm_mode": self.llm_mode,
            "enable_structural_verify": self.enable_structural_verify,
            "enable_witness_synthesis": self.enable_witness_synthesis,
            "validate_witnesses": self.validate_witnesses,
            "enable_family_pruning": self.enable_family_pruning,
            "enable_preprocessing": self.enable_preprocessing,
            "enable_predicate_promotion": self.enable_predicate_promotion,
            "enable_table_elimination": self.enable_table_elimination,
            "enable_adaptive_limits": self.enable_adaptive_limits,
            "enable_compositional": self.enable_compositional,
            "cost_model": self.cost_model,
            "verbose": self.verbose,
            "seed": self.seed,
        }

    @classmethod
    def ablation(cls, name: str) -> "OptimizerConfig":
        """Create a config for a named ablation experiment.

        Supported ablations:
            "no_llm"            — disable LLM rewrites
            "no_rules"          — disable rule-based rewrites
            "no_preprocessing"  — disable Phase 7 preprocessing
            "no_family_pruning" — disable family-based witness pruning
            "no_verification"   — disable witness synthesis (accept all structural-ok)
            "no_table_elim"     — disable redundant table elimination only
            "rules_only"        — rules only, no LLM, no preprocessing
            "llm_only"          — LLM only, no rules
            "full"              — all features enabled (default)
        """
        config = cls()
        if name == "no_llm":
            config.enable_llm_rewrites = False
        elif name == "no_rules":
            config.enable_rule_rewrites = False
        elif name == "no_preprocessing":
            config.enable_preprocessing = False
            config.enable_predicate_promotion = False
            config.enable_table_elimination = False
            config.enable_adaptive_limits = False
        elif name == "no_family_pruning":
            config.enable_family_pruning = False
        elif name == "no_verification":
            config.enable_witness_synthesis = False
        elif name == "no_table_elim":
            config.enable_table_elimination = False
        elif name == "rules_only":
            config.enable_llm_rewrites = False
        elif name == "llm_only":
            config.enable_rule_rewrites = False
            config.enable_llm_rewrites = True
        elif name == "full":
            config.enable_llm_rewrites = True
        elif name == "compositional_only":
            config.enable_compositional = True
        elif name == "compositional":
            config.enable_compositional = True
        else:
            raise ValueError(f"Unknown ablation: {name!r}")
        return config

    # Named ablation configs for CLI
    ABLATION_NAMES = [
        "full", "no_llm", "no_rules", "no_preprocessing",
        "no_family_pruning", "no_verification", "no_table_elim",
        "rules_only", "llm_only", "compositional_only", "compositional",
    ]
