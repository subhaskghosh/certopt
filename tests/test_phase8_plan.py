"""Tests for Phase 8: PostgreSQL EXPLAIN cost model and plan diagnostics."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from optim.cost.pg_explain import PostgresExplainCostEstimator, _parse_plan_tree
from optim.cost.plan_diagnostics import (
    PainPoint,
    PlanDiagnostics,
    extract_diagnostics,
)


# ---------------------------------------------------------------------------
# P8.1 – _parse_plan_tree
# ---------------------------------------------------------------------------


class TestParsePlanTree:
    def test_parse_plan_tree_seq_scan(self):
        plan = {"Node Type": "Seq Scan", "Relation Name": "orders"}
        assert _parse_plan_tree(plan) == 100.0

    def test_parse_plan_tree_nested_loop(self):
        plan = {
            "Node Type": "Nested Loop",
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "orders"},
                {"Node Type": "Index Scan", "Relation Name": "customers"},
            ],
        }
        # Nested Loop (80) + Seq Scan (100) + Index Scan (10) = 190
        assert _parse_plan_tree(plan) == 190.0

    def test_parse_plan_tree_unknown_node(self):
        plan = {"Node Type": "UnknownNode"}
        assert _parse_plan_tree(plan) == 5.0

    def test_parse_plan_tree_hash_join(self):
        plan = {
            "Node Type": "Hash Join",
            "Plans": [
                {"Node Type": "Seq Scan"},
                {"Node Type": "Hash", "Plans": [{"Node Type": "Seq Scan"}]},
            ],
        }
        # Hash Join (15) + Seq Scan (100) + Hash (5) + Seq Scan (100) = 220
        assert _parse_plan_tree(plan) == 220.0


class TestPostgresExplainFallback:
    def test_fallback_on_no_connection(self, simple_select_ir, sample_catalog):
        """When psycopg2 import fails, fall back to syntactic cost."""
        estimator = PostgresExplainCostEstimator(conn_string="host=nohost dbname=nodb")

        with patch.dict("sys.modules", {"psycopg2": None}):
            result = estimator.estimate(simple_select_ir, sample_catalog)

        assert result.source == "syntactic"
        assert isinstance(result.total_cost, float)

    def test_fallback_on_connect_error(self, simple_select_ir, sample_catalog):
        """When connection raises, fall back to syntactic cost."""
        mock_psycopg2 = MagicMock()
        mock_psycopg2.connect.side_effect = Exception("connection refused")

        estimator = PostgresExplainCostEstimator(conn_string="host=nohost")

        with patch.dict("sys.modules", {"psycopg2": mock_psycopg2}):
            result = estimator.estimate(simple_select_ir, sample_catalog)

        assert result.source == "syntactic"


# ---------------------------------------------------------------------------
# P8.2 – plan_diagnostics
# ---------------------------------------------------------------------------


class TestExtractDiagnostics:
    def test_extract_seq_scan_pain_point(self):
        plan = {
            "Node Type": "Seq Scan",
            "Relation Name": "big_table",
            "Plan Rows": 500_000,
            "Total Cost": 12345.0,
        }
        diag = extract_diagnostics(plan)
        assert len(diag.pain_points) == 1
        pp = diag.pain_points[0]
        assert pp.operator == "Seq Scan"
        assert "big_table" in pp.tables
        assert pp.estimated_rows == 500_000
        assert "index" in pp.suggestion.lower()

    def test_extract_nested_loop_pain_point(self):
        plan = {
            "Node Type": "Nested Loop",
            "Plan Rows": 50_000,
            "Total Cost": 9999.0,
            "Plans": [
                {"Node Type": "Index Scan", "Relation Name": "t1", "Plan Rows": 100, "Total Cost": 10.0},
                {"Node Type": "Index Scan", "Relation Name": "t2", "Plan Rows": 500, "Total Cost": 50.0},
            ],
        }
        diag = extract_diagnostics(plan)
        nested_pps = [pp for pp in diag.pain_points if pp.operator == "Nested Loop"]
        assert len(nested_pps) == 1
        assert nested_pps[0].estimated_rows == 50_000

    def test_no_pain_points_small_plan(self):
        plan = {
            "Node Type": "Index Scan",
            "Relation Name": "users",
            "Plan Rows": 50,
            "Total Cost": 8.5,
        }
        diag = extract_diagnostics(plan)
        assert len(diag.pain_points) == 0

    def test_bottleneck_fraction(self):
        plan = {
            "Node Type": "Hash Join",
            "Plan Rows": 1000,
            "Total Cost": 5000.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "large",
                    "Plan Rows": 200_000,
                    "Total Cost": 4000.0,
                },
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "small",
                    "Plan Rows": 100,
                    "Total Cost": 50.0,
                },
            ],
        }
        diag = extract_diagnostics(plan)
        assert diag.total_cost == 5000.0
        assert len(diag.pain_points) == 1  # large Seq Scan
        assert diag.bottleneck_fraction > 0

    def test_cardinality_misestimation(self):
        plan = {
            "Node Type": "Seq Scan",
            "Relation Name": "data",
            "Plan Rows": 200_000,
            "Actual Rows": 10,
            "Total Cost": 500.0,
        }
        diag = extract_diagnostics(plan)
        # Should get both large seq scan + cardinality misestimation
        operators = [pp.operator for pp in diag.pain_points]
        assert "Seq Scan" in operators
        misest = [pp for pp in diag.pain_points if pp.est_actual_ratio is not None]
        assert len(misest) == 1
        assert misest[0].est_actual_ratio == 20_000.0

    def test_hash_spill(self):
        plan = {
            "Node Type": "Hash Join",
            "Plan Rows": 1000,
            "Hash Batches": 4,
            "Total Cost": 300.0,
        }
        diag = extract_diagnostics(plan)
        spills = [pp for pp in diag.pain_points if "spill" in pp.suggestion.lower()]
        assert len(spills) == 1
