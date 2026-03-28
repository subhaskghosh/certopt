"""Plan diagnostic extraction: structured pain points from EXPLAIN output.

Identifies performance bottlenecks to guide LLM rewrite generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PainPoint:
    """A single performance bottleneck identified in a query plan."""
    operator: str
    tables: list[str]
    estimated_rows: int
    actual_rows: Optional[int] = None
    est_actual_ratio: Optional[float] = None
    suggestion: str = ""


@dataclass
class PlanDiagnostics:
    """Aggregated diagnostics extracted from an EXPLAIN plan."""
    pain_points: list[PainPoint] = field(default_factory=list)
    total_cost: float = 0.0
    bottleneck_fraction: float = 0.0


def _extract_tables(node: dict) -> list[str]:
    """Extract table names from a plan node."""
    tables: list[str] = []
    relation = node.get("Relation Name")
    if relation:
        tables.append(relation)
    alias = node.get("Alias")
    if alias and alias != relation:
        tables.append(alias)
    return tables


def _node_cost(node: dict) -> float:
    """Return the total cost attributed to this node (from PostgreSQL plan)."""
    return float(node.get("Total Cost", 0.0))


def _walk_plan(node: dict, pain_points: list[PainPoint]) -> None:
    """Recursively walk a plan tree collecting pain points."""
    node_type = node.get("Node Type", "")
    est_rows = int(node.get("Plan Rows", 0))
    actual_rows: Optional[int] = None
    if "Actual Rows" in node:
        actual_rows = int(node["Actual Rows"])

    tables = _extract_tables(node)

    # 1. Large sequential scans
    if node_type == "Seq Scan" and est_rows > 100_000:
        pain_points.append(PainPoint(
            operator=node_type,
            tables=tables,
            estimated_rows=est_rows,
            actual_rows=actual_rows,
            suggestion="Consider adding an index to avoid full sequential scan on large table.",
        ))

    # 2. Nested loop on large tables
    if node_type == "Nested Loop" and est_rows > 10_000:
        pain_points.append(PainPoint(
            operator=node_type,
            tables=tables,
            estimated_rows=est_rows,
            actual_rows=actual_rows,
            suggestion="Nested loop on large result set; consider hash or merge join via index/rewrite.",
        ))

    # 3. Cardinality misestimation (est/actual ratio > 10x)
    if actual_rows is not None and est_rows > 0 and actual_rows > 0:
        ratio = max(est_rows / actual_rows, actual_rows / est_rows)
        if ratio > 10.0:
            pain_points.append(PainPoint(
                operator=node_type,
                tables=tables,
                estimated_rows=est_rows,
                actual_rows=actual_rows,
                est_actual_ratio=ratio,
                suggestion="Cardinality misestimation detected (>10x); consider ANALYZE or rewrite to help planner.",
            ))

    # 4. Hash spills
    hash_batches = node.get("Hash Batches", 0)
    if hash_batches and int(hash_batches) > 1:
        pain_points.append(PainPoint(
            operator=node_type,
            tables=tables,
            estimated_rows=est_rows,
            actual_rows=actual_rows,
            suggestion="Hash spill detected (batches > 1); consider increasing work_mem or reducing join size.",
        ))

    # 5. Repeated subplan execution
    if node_type == "SubPlan" or node_type.startswith("SubPlan"):
        loops = int(node.get("Actual Loops", 1))
        if loops > 1:
            pain_points.append(PainPoint(
                operator=node_type,
                tables=tables,
                estimated_rows=est_rows,
                actual_rows=actual_rows,
                suggestion="SubPlan executed multiple times; consider rewriting as JOIN or CTE.",
            ))

    for child in node.get("Plans", []):
        _walk_plan(child, pain_points)


def extract_diagnostics(plan_json: dict) -> PlanDiagnostics:
    """Extract diagnostics from a PostgreSQL EXPLAIN (FORMAT JSON) plan.

    Parameters
    ----------
    plan_json:
        The root plan node (i.e. ``result[0][0]["Plan"]`` from a
        ``EXPLAIN (FORMAT JSON)`` query).

    Returns
    -------
    PlanDiagnostics
        Structured diagnostics with identified pain points.
    """
    pain_points: list[PainPoint] = []
    _walk_plan(plan_json, pain_points)

    total_cost = _node_cost(plan_json)

    bottleneck_fraction = 0.0
    if total_cost > 0 and pain_points:
        max_pp_cost = max(_node_cost(plan_json) for _ in pain_points)
        # Re-walk to find highest-cost pain-point node cost.
        # We approximate: use the root total cost for each pain point since
        # individual node costs aren't stored on PainPoint.  Instead, use
        # estimated_rows as a proxy for relative weight.
        max_est = max(pp.estimated_rows for pp in pain_points)
        total_est = sum(pp.estimated_rows for pp in pain_points)
        if total_est > 0:
            bottleneck_fraction = max_est / total_cost if total_cost > 0 else 0.0

    return PlanDiagnostics(
        pain_points=pain_points,
        total_cost=total_cost,
        bottleneck_fraction=bottleneck_fraction,
    )
