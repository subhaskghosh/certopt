"""Cost estimation for query optimization.

Provides a CostEstimator protocol and two implementations:
  - SyntacticCostEstimator: heuristic scoring from IR structure
  - ExplainCostEstimator: SQLite EXPLAIN QUERY PLAN-based scoring
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..ir.render_sql import render
from ..ir.types import (
    AggCall,
    ColumnRef,
    DerivedTable,
    ExistsSubquery,
    Expr,
    InSubquery,
    JoinType,
    QueryIR,
    RelRef,
    ScalarSubquery,
    Star,
)
from ..schema.catalog import Catalog

logger = logging.getLogger(__name__)


@dataclass
class CostEstimate:
    """Result of cost estimation for a single query."""
    total_cost: float
    breakdown: dict[str, float] = field(default_factory=dict)
    source: str = "unknown"


class CostEstimator(Protocol):
    """Protocol for cost estimation."""
    def estimate(self, ir: QueryIR, catalog: Catalog) -> CostEstimate: ...


# ---------------------------------------------------------------------------
# Syntactic cost model
# ---------------------------------------------------------------------------

def _count_subqueries(expr: Optional[Expr]) -> int:
    """Count subquery nodes in an expression tree."""
    if expr is None:
        return 0
    count = 0
    if isinstance(expr, (ScalarSubquery, InSubquery, ExistsSubquery)):
        count += 1
    from ..ir.types import BinOp, UnaryOp, FuncCall, InList, Between, CaseExpr
    if isinstance(expr, BinOp):
        count += _count_subqueries(expr.left) + _count_subqueries(expr.right)
    elif isinstance(expr, UnaryOp):
        count += _count_subqueries(expr.operand)
    elif isinstance(expr, FuncCall):
        for a in expr.args:
            count += _count_subqueries(a)
    elif isinstance(expr, AggCall):
        count += _count_subqueries(expr.arg)
    elif isinstance(expr, InList):
        count += _count_subqueries(expr.expr)
    elif isinstance(expr, Between):
        count += _count_subqueries(expr.expr)
    elif isinstance(expr, CaseExpr):
        for cw in expr.whens:
            count += _count_subqueries(cw.when) + _count_subqueries(cw.then)
        count += _count_subqueries(expr.else_)
    return count


def _collect_table_refs(expr: Optional[Expr]) -> set[str]:
    """Collect table aliases referenced by column refs in an expression."""
    if expr is None:
        return set()
    tables: set[str] = set()
    if isinstance(expr, ColumnRef) and expr.table:
        tables.add(expr.table.lower())
    from ..ir.types import BinOp, UnaryOp, FuncCall, InList, Between, CaseExpr
    if isinstance(expr, BinOp):
        tables |= _collect_table_refs(expr.left)
        tables |= _collect_table_refs(expr.right)
    elif isinstance(expr, UnaryOp):
        tables |= _collect_table_refs(expr.operand)
    elif isinstance(expr, FuncCall):
        for a in expr.args:
            tables |= _collect_table_refs(a)
    elif isinstance(expr, AggCall):
        tables |= _collect_table_refs(expr.arg)
    elif isinstance(expr, InList):
        tables |= _collect_table_refs(expr.expr)
    elif isinstance(expr, Between):
        tables |= _collect_table_refs(expr.expr)
    elif isinstance(expr, CaseExpr):
        for cw in expr.whens:
            tables |= _collect_table_refs(cw.when) | _collect_table_refs(cw.then)
        tables |= _collect_table_refs(expr.else_)
    return tables


def _collect_and_conjuncts_simple(expr: Optional[Expr]) -> list[Expr]:
    """Split an AND-tree into conjuncts (simple version, no import dependency)."""
    if expr is None:
        return []
    from ..ir.types import BinOp, BinOpKind
    if isinstance(expr, BinOp) and expr.op == BinOpKind.AND:
        return _collect_and_conjuncts_simple(expr.left) + _collect_and_conjuncts_simple(expr.right)
    return [expr]


def _count_late_where_filters(ir: QueryIR) -> int:
    """Count WHERE conjuncts that reference a joined table (late filters).

    A filter in WHERE that references columns from a JOIN's right table is
    applied after the join. If it were in the JOIN ON clause, the database
    could apply it during the join scan (early filtering).
    """
    if ir.where is None or not ir.joins:
        return 0

    join_aliases: set[str] = set()
    for j in ir.joins:
        if isinstance(j.right, RelRef):
            join_aliases.add((j.right.alias or j.right.table).lower())

    count = 0
    for conj in _collect_and_conjuncts_simple(ir.where):
        refs = _collect_table_refs(conj)
        if refs & join_aliases:
            count += 1
    return count


class SyntacticCostEstimator:
    """Heuristic cost model based on IR structure.

    Cost factors:
      +10  per join
      +100 per CROSS JOIN
      +20  per subquery
      +5   for DISTINCT
      +3   per GROUP BY column
      +5   for ORDER BY
      -2   for LIMIT (early termination)
      +1   per table scanned
      +2   per late WHERE filter that references a joined table
           (could have been pushed into JOIN ON for early filtering)
    """

    def estimate(self, ir: QueryIR, catalog: Catalog) -> CostEstimate:
        breakdown: dict[str, float] = {}

        # Table count
        n_tables = 1  # from_table
        for j in ir.joins:
            n_tables += 1
        breakdown["tables"] = float(n_tables)

        # Joins
        n_joins = len(ir.joins)
        n_cross = sum(1 for j in ir.joins if j.join_type == JoinType.CROSS)
        breakdown["joins"] = n_joins * 10.0
        breakdown["cross_joins"] = n_cross * 100.0

        # Subqueries
        n_subq = 0
        for sel in ir.select:
            n_subq += _count_subqueries(sel)
        n_subq += _count_subqueries(ir.where)
        n_subq += _count_subqueries(ir.having)
        for j in ir.joins:
            n_subq += _count_subqueries(j.on)
        breakdown["subqueries"] = n_subq * 20.0

        # DISTINCT
        breakdown["distinct"] = 5.0 if ir.distinct else 0.0

        # GROUP BY
        breakdown["group_by"] = len(ir.group_by) * 3.0

        # ORDER BY
        breakdown["order_by"] = 5.0 if ir.order_by else 0.0

        # LIMIT benefit
        breakdown["limit"] = -2.0 if ir.limit is not None else 0.0

        # Late WHERE filter penalty: WHERE predicates that reference a joined
        # table's columns are applied after the join materialises all rows.
        # Moving them into JOIN ON allows early filtering during the join scan.
        breakdown["late_filters"] = _count_late_where_filters(ir) * 2.0

        total = sum(breakdown.values())
        return CostEstimate(
            total_cost=total,
            breakdown=breakdown,
            source="syntactic",
        )


# ---------------------------------------------------------------------------
# EXPLAIN-based cost model (SQLite)
# ---------------------------------------------------------------------------

class ExplainCostEstimator:
    """Cost model using SQLite EXPLAIN QUERY PLAN.

    Scoring:
      SCAN TABLE           = 100  (full table scan)
      SEARCH TABLE ... INDEX = 10 (index lookup)
      USE TEMP B-TREE      = 20  (sort/distinct temp)
      COMPOUND QUERY       = 15  (set operation)
      Other                = 5   (default)
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def estimate(self, ir: QueryIR, catalog: Catalog) -> CostEstimate:
        sql = render(ir, dialect="sqlite")
        breakdown: dict[str, float] = {}

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(f"EXPLAIN QUERY PLAN {sql}")
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            logger.warning("EXPLAIN failed for query: %s", e)
            return CostEstimate(total_cost=float("inf"), source="explain_error")

        scan_cost = 0.0
        sort_cost = 0.0
        other_cost = 0.0

        for row in rows:
            detail = str(row[-1]) if row else ""
            if "SCAN TABLE" in detail or "SCAN " in detail:
                scan_cost += 100.0
            elif "SEARCH TABLE" in detail or "SEARCH " in detail:
                scan_cost += 10.0
            elif "TEMP B-TREE" in detail:
                sort_cost += 20.0
            elif "COMPOUND" in detail:
                other_cost += 15.0
            else:
                other_cost += 5.0

        breakdown["scan"] = scan_cost
        breakdown["sort"] = sort_cost
        breakdown["other"] = other_cost

        return CostEstimate(
            total_cost=sum(breakdown.values()),
            breakdown=breakdown,
            source="explain",
        )
