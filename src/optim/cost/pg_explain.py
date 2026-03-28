"""PostgreSQL EXPLAIN ANALYZE cost model.

Parses PostgreSQL EXPLAIN (ANALYZE, FORMAT JSON) output to produce
plan-sensitive cost estimates. Falls back gracefully when no PostgreSQL
connection is available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..ir.render_sql import render
from ..ir.types import QueryIR
from ..schema.catalog import Catalog
from .estimator import CostEstimate, SyntacticCostEstimator

logger = logging.getLogger(__name__)

# Node-type cost weights used when scoring a plan tree.
_NODE_COSTS: dict[str, float] = {
    "Seq Scan": 100.0,
    "Index Scan": 10.0,
    "Index Only Scan": 10.0,
    "Hash Join": 15.0,
    "Nested Loop": 80.0,
    "Sort": 20.0,
    "Merge Join": 12.0,
    "Bitmap Heap Scan": 15.0,
    "Bitmap Index Scan": 15.0,
}

_DEFAULT_NODE_COST = 5.0


def _parse_plan_tree(plan_json: dict) -> float:
    """Recursively walk a PostgreSQL JSON plan tree and sum node costs."""
    node_type = plan_json.get("Node Type", "")
    cost = _NODE_COSTS.get(node_type, _DEFAULT_NODE_COST)

    for child in plan_json.get("Plans", []):
        cost += _parse_plan_tree(child)

    return cost


class PostgresExplainCostEstimator:
    """Cost model using PostgreSQL EXPLAIN (FORMAT JSON).

    Runs ``EXPLAIN (FORMAT JSON)`` against a live PostgreSQL instance and
    scores the resulting plan tree.  Falls back to the syntactic cost model
    when *psycopg2* is not installed or the connection fails.
    """

    def __init__(self, conn_string: str) -> None:
        self.conn_string = conn_string

    def estimate(self, ir: QueryIR, catalog: Catalog) -> CostEstimate:
        sql = render(ir, dialect="postgres")

        try:
            import psycopg2  # lazy import
        except ImportError:
            logger.warning("psycopg2 not installed; falling back to syntactic cost model")
            return SyntacticCostEstimator().estimate(ir, catalog)

        try:
            conn = psycopg2.connect(self.conn_string)
            try:
                cur = conn.cursor()
                cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
                result = cur.fetchone()
                cur.close()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("PostgreSQL EXPLAIN failed: %s; falling back to syntactic cost model", e)
            return SyntacticCostEstimator().estimate(ir, catalog)

        if not result or not result[0]:
            logger.warning("Empty EXPLAIN result; falling back to syntactic cost model")
            return SyntacticCostEstimator().estimate(ir, catalog)

        plan_json = result[0][0]["Plan"]
        total = _parse_plan_tree(plan_json)

        return CostEstimate(
            total_cost=total,
            breakdown={"plan_tree": total},
            source="pg_explain",
        )
