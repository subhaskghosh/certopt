"""Evaluation harness for running the CEGIS optimizer on benchmark queries.

Iterates over a BenchmarkSuite, runs the optimization loop on each query,
and collects parse/optimization/improvement metrics for analysis.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..config import OptimizerConfig
from ..cost.estimator import SyntacticCostEstimator
from ..ir.render_sql import render
from ..optimizer.loop import optimize, optimize_with_config
from ..optimizer.result import OptimizationResult
from ..parser.sql_to_ir import sql_to_ir
from ..rewrite.generator import RewriteConfig
from ..schema.catalog import Catalog
from ..verify.encode_z3 import BoundedScope
from .benchmark import BenchmarkQuery, BenchmarkSuite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------


@dataclass
class QueryResult:
    """Result of evaluating a single benchmark query."""

    query_id: str
    original_sql: str
    parse_success: bool
    parse_error: str | None = None
    optimization_result: OptimizationResult | None = None
    num_candidates: int = 0
    num_verified: int = 0
    num_rejected: int = 0
    improved: bool = False
    speedup: float = 1.0
    optimizer_time_ms: float = 0.0
    error: str | None = None
    prep_tables_before: int = 0
    prep_tables_after: int = 0
    prep_promoted: int = 0
    prep_eliminated: int = 0


@dataclass
class EvalResult:
    """Aggregated result of evaluating a full benchmark suite."""

    suite_name: str
    queries: list[QueryResult] = field(default_factory=list)
    total_time_ms: float = 0.0
    timestamp: str = ""
    config: Optional[dict] = None  # serialized OptimizerConfig for reproducibility

    @property
    def parse_rate(self) -> float:
        """Fraction of queries parsed successfully."""
        if not self.queries:
            return 0.0
        return sum(1 for q in self.queries if q.parse_success) / len(self.queries)

    @property
    def optimize_rate(self) -> float:
        """Fraction of queries where the optimizer ran without error."""
        if not self.queries:
            return 0.0
        return sum(1 for q in self.queries if q.optimization_result is not None) / len(self.queries)

    @property
    def improvement_rate(self) -> float:
        """Fraction of queries that were improved."""
        if not self.queries:
            return 0.0
        return sum(1 for q in self.queries if q.improved) / len(self.queries)

    @property
    def avg_speedup(self) -> float:
        """Average speedup across improved queries."""
        improved = [q.speedup for q in self.queries if q.improved]
        if not improved:
            return 1.0
        return sum(improved) / len(improved)

    def summary_dict(self) -> dict:
        """Return a dict summarizing all metrics for JSON export."""
        return {
            "suite_name": self.suite_name,
            "timestamp": self.timestamp,
            "total_time_ms": self.total_time_ms,
            "num_queries": len(self.queries),
            "parse_rate": self.parse_rate,
            "optimize_rate": self.optimize_rate,
            "improvement_rate": self.improvement_rate,
            "avg_speedup": self.avg_speedup,
            "config": self.config,
            "queries": [
                {
                    "id": q.query_id,
                    "parse_success": q.parse_success,
                    "improved": q.improved,
                    "speedup": q.speedup,
                    "num_candidates": q.num_candidates,
                    "time_ms": q.optimizer_time_ms,
                    "prep_tables_before": q.prep_tables_before,
                    "prep_tables_after": q.prep_tables_after,
                    "prep_promoted": q.prep_promoted,
                    "prep_eliminated": q.prep_eliminated,
                }
                for q in self.queries
            ],
        }


# ---------------------------------------------------------------------------
# Evaluation entry point
# ---------------------------------------------------------------------------


def run_evaluation(
    suite: BenchmarkSuite,
    catalog: Catalog,
    *,
    scope: Optional[BoundedScope] = None,
    rewrite_config: Optional[RewriteConfig] = None,
    dialect: str = "postgres",
) -> EvalResult:
    """Run the CEGIS optimizer on every query in a benchmark suite.

    Args:
        suite: Benchmark suite to evaluate.
        catalog: Schema metadata for the workload.
        scope: Bounded scope for witness synthesis.
        rewrite_config: Rewrite rule configuration.
        dialect: SQL dialect (default ``"postgres"`` for JOB-Complex).

    Returns:
        An :class:`EvalResult` with per-query results and aggregate metrics.
    """
    n = len(suite.queries)
    results: list[QueryResult] = []
    t_start = time.monotonic()

    for i, bq in enumerate(suite.queries):
        qr = _evaluate_query(bq, catalog, scope=scope, rewrite_config=rewrite_config, dialect=dialect)
        results.append(qr)

        status = "improved" if qr.improved else ("ok" if qr.parse_success else "parse_fail")
        if qr.error:
            status = "error"
        logger.info("[%d/%d] %s: %s", i + 1, n, qr.query_id, status)

    total_ms = (time.monotonic() - t_start) * 1000

    return EvalResult(
        suite_name=suite.name,
        queries=results,
        total_time_ms=total_ms,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _evaluate_query(
    bq: BenchmarkQuery,
    catalog: Catalog,
    *,
    scope: Optional[BoundedScope] = None,
    rewrite_config: Optional[RewriteConfig] = None,
    dialect: str = "postgres",
) -> QueryResult:
    """Evaluate a single benchmark query."""
    # Check parseability first
    ir, parse_err = sql_to_ir(bq.sql, dialect=dialect)
    if ir is None:
        return QueryResult(
            query_id=bq.id,
            original_sql=bq.sql,
            parse_success=False,
            parse_error=parse_err,
        )

    # Run the optimizer
    try:
        t0 = time.monotonic()
        opt_result = optimize(
            bq.sql,
            catalog,
            scope=scope,
            rewrite_config=rewrite_config,
            dialect=dialect,
            validate_witnesses=False,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000

        return QueryResult(
            query_id=bq.id,
            original_sql=bq.sql,
            parse_success=True,
            optimization_result=opt_result,
            num_candidates=opt_result.total_candidates,
            num_verified=opt_result.n_verified,
            num_rejected=opt_result.n_rejected,
            improved=opt_result.improved,
            speedup=opt_result.speedup,
            optimizer_time_ms=elapsed_ms,
        )
    except Exception as exc:
        return QueryResult(
            query_id=bq.id,
            original_sql=bq.sql,
            parse_success=True,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Config-driven evaluation
# ---------------------------------------------------------------------------


def run_evaluation_with_config(
    suite: BenchmarkSuite,
    catalog: Catalog,
    config: OptimizerConfig,
) -> EvalResult:
    """Run evaluation using OptimizerConfig (supports feature flags + LLM rewrites)."""
    n = len(suite.queries)
    results: list[QueryResult] = []
    t_start = time.monotonic()

    for i, bq in enumerate(suite.queries):
        qr = _evaluate_query_with_config(bq, catalog, config)
        results.append(qr)

        status = "improved" if qr.improved else ("ok" if qr.parse_success else "parse_fail")
        if qr.error:
            status = "error"
        logger.info("[%d/%d] %s: %s", i + 1, n, qr.query_id, status)

    total_ms = (time.monotonic() - t_start) * 1000

    return EvalResult(
        suite_name=suite.name,
        queries=results,
        total_time_ms=total_ms,
        timestamp=datetime.now(timezone.utc).isoformat(),
        config=config.to_dict(),
    )


def _evaluate_query_with_config(
    bq: BenchmarkQuery,
    catalog: Catalog,
    config: OptimizerConfig,
) -> QueryResult:
    """Evaluate a single benchmark query using OptimizerConfig."""
    ir, parse_err = sql_to_ir(bq.sql, dialect=config.dialect)
    if ir is None:
        return QueryResult(
            query_id=bq.id,
            original_sql=bq.sql,
            parse_success=False,
            parse_error=parse_err,
        )

    try:
        t0 = time.monotonic()
        opt_result = optimize_with_config(bq.sql, catalog, config)
        elapsed_ms = (time.monotonic() - t0) * 1000

        return QueryResult(
            query_id=bq.id,
            original_sql=bq.sql,
            parse_success=True,
            optimization_result=opt_result,
            num_candidates=opt_result.total_candidates,
            num_verified=opt_result.n_verified,
            num_rejected=opt_result.n_rejected,
            improved=opt_result.improved,
            speedup=opt_result.speedup,
            optimizer_time_ms=elapsed_ms,
        )
    except Exception as exc:
        return QueryResult(
            query_id=bq.id,
            original_sql=bq.sql,
            parse_success=True,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------


def save_results(result: EvalResult, output_path: str) -> None:
    """Save evaluation results to JSON + markdown summary."""
    with open(output_path, "w") as f:
        json.dump(result.summary_dict(), f, indent=2)

    # Save markdown summary alongside JSON
    md_path = output_path.rsplit(".", 1)[0] + ".md"
    _write_markdown_summary(result, md_path)

    # Save config JSON for reproducibility
    if result.config:
        config_path = output_path.rsplit(".", 1)[0] + "_config.json"
        with open(config_path, "w") as f:
            json.dump(result.config, f, indent=2)

    logger.info("Saved results to %s and %s", output_path, md_path)


def _write_markdown_summary(result: EvalResult, path: str) -> None:
    """Write a human-readable markdown summary."""
    lines = [
        f"# Evaluation: {result.suite_name}",
        f"**Timestamp:** {result.timestamp}",
        f"**Total time:** {result.total_time_ms:.0f}ms",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Queries | {len(result.queries)} |",
        f"| Parse rate | {result.parse_rate:.1%} |",
        f"| Optimization rate | {result.optimize_rate:.1%} |",
        f"| Improvement rate | {result.improvement_rate:.1%} |",
        f"| Avg speedup | {result.avg_speedup:.2f}× |",
        "",
    ]
    # Per-query details
    lines.append("## Per-query Results")
    lines.append("")
    lines.append("| Query | Status | Candidates | Verified | Speedup | Time |")
    lines.append("|---|---|---|---|---|---|")
    for q in result.queries:
        status = "✅" if q.improved else ("❌" if q.error else "—")
        lines.append(
            f"| {q.query_id} | {status} | {q.num_candidates} | "
            f"{q.num_verified} | {q.speedup:.2f}× | {q.optimizer_time_ms:.0f}ms |"
        )

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
