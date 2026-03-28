"""Tests for cost estimators."""

import sqlite3
import tempfile

import pytest

from optim.cost.estimator import (
    CostEstimate,
    ExplainCostEstimator,
    SyntacticCostEstimator,
)
from optim.ir.types import (
    AggCall,
    AggFunc,
    BinOp,
    BinOpKind,
    ColumnRef,
    JoinClause,
    JoinType,
    Literal,
    QueryIR,
    RelRef,
    SortDir,
    SortSpec,
)
from optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from optim.ir.types import SemType


@pytest.fixture
def catalog():
    return Catalog(
        tables={
            "customers": TableInfo(
                name="customers",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="name", sem_type=SemType.STRING, nullable=False),
                ],
                primary_keys=["id"],
            ),
            "orders": TableInfo(
                name="orders",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="customer_id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="amount", sem_type=SemType.INT, nullable=False),
                ],
                primary_keys=["id"],
            ),
        },
        foreign_keys=[
            ForeignKey(src_table="orders", src_column="customer_id",
                       dst_table="customers", dst_column="id"),
        ],
    )


def _simple_query():
    return QueryIR(
        select=[ColumnRef(table="customers", column="name")],
        from_table=RelRef(table="customers"),
    )


def _join_query():
    return QueryIR(
        select=[
            ColumnRef(table="customers", column="name"),
            ColumnRef(table="orders", column="amount"),
        ],
        from_table=RelRef(table="customers"),
        joins=[JoinClause(
            join_type=JoinType.INNER,
            right=RelRef(table="orders"),
            on=BinOp(op=BinOpKind.EQ,
                     left=ColumnRef(table="customers", column="id"),
                     right=ColumnRef(table="orders", column="customer_id")),
        )],
    )


def _complex_query():
    return QueryIR(
        select=[
            ColumnRef(table="customers", column="name"),
            AggCall(func=AggFunc.COUNT, arg=None),
        ],
        from_table=RelRef(table="customers"),
        joins=[JoinClause(
            join_type=JoinType.INNER,
            right=RelRef(table="orders"),
            on=BinOp(op=BinOpKind.EQ,
                     left=ColumnRef(table="customers", column="id"),
                     right=ColumnRef(table="orders", column="customer_id")),
        )],
        group_by=[ColumnRef(table="customers", column="name")],
        distinct=True,
        order_by=[SortSpec(expr=ColumnRef(table="customers", column="name"), direction=SortDir.ASC)],
        limit=10,
    )


# ===================================================================
# SyntacticCostEstimator
# ===================================================================

class TestSyntacticCost:

    def test_simple_query_cheap(self, catalog):
        est = SyntacticCostEstimator()
        cost = est.estimate(_simple_query(), catalog)
        assert cost.total_cost > 0
        assert cost.source == "syntactic"
        assert "tables" in cost.breakdown

    def test_join_more_expensive_than_simple(self, catalog):
        est = SyntacticCostEstimator()
        c_simple = est.estimate(_simple_query(), catalog)
        c_join = est.estimate(_join_query(), catalog)
        assert c_join.total_cost > c_simple.total_cost

    def test_complex_query_most_expensive(self, catalog):
        est = SyntacticCostEstimator()
        c_simple = est.estimate(_simple_query(), catalog)
        c_complex = est.estimate(_complex_query(), catalog)
        assert c_complex.total_cost > c_simple.total_cost

    def test_distinct_adds_cost(self, catalog):
        est = SyntacticCostEstimator()
        ir = _simple_query()
        c_no_dist = est.estimate(ir, catalog)
        ir_dist = ir.model_copy(deep=True)
        ir_dist.distinct = True
        c_dist = est.estimate(ir_dist, catalog)
        assert c_dist.total_cost > c_no_dist.total_cost

    def test_limit_reduces_cost(self, catalog):
        est = SyntacticCostEstimator()
        ir = _simple_query()
        c_no_limit = est.estimate(ir, catalog)
        ir_limit = ir.model_copy(deep=True)
        ir_limit.limit = 10
        c_limit = est.estimate(ir_limit, catalog)
        assert c_limit.total_cost < c_no_limit.total_cost

    def test_breakdown_keys(self, catalog):
        est = SyntacticCostEstimator()
        cost = est.estimate(_complex_query(), catalog)
        expected_keys = {"tables", "joins", "cross_joins", "subqueries",
                         "distinct", "group_by", "order_by", "limit"}
        assert expected_keys <= set(cost.breakdown.keys())


# ===================================================================
# ExplainCostEstimator
# ===================================================================

class TestExplainCost:

    @pytest.fixture
    def db_path(self):
        """Create a temp SQLite DB with customers/orders tables."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            conn = sqlite3.connect(f.name)
            conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount INTEGER)")
            conn.execute("INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob')")
            conn.execute("INSERT INTO orders VALUES (1, 1, 100), (2, 1, 200), (3, 2, 50)")
            conn.commit()
            conn.close()
            return f.name

    def test_explain_produces_cost(self, catalog, db_path):
        est = ExplainCostEstimator(db_path)
        cost = est.estimate(_simple_query(), catalog)
        assert cost.total_cost > 0
        assert cost.source == "explain"

    def test_explain_join_vs_simple(self, catalog, db_path):
        est = ExplainCostEstimator(db_path)
        c_simple = est.estimate(_simple_query(), catalog)
        c_join = est.estimate(_join_query(), catalog)
        # Join should have more operations in the plan
        assert c_join.total_cost >= c_simple.total_cost

    def test_explain_bad_db_returns_inf(self, catalog):
        est = ExplainCostEstimator("/nonexistent/path.db")
        cost = est.estimate(_simple_query(), catalog)
        assert cost.total_cost == float("inf")

    def test_explain_distinct_costs(self, catalog, db_path):
        est = ExplainCostEstimator(db_path)
        cost = est.estimate(_simple_query(), catalog)
        assert cost.source == "explain"
        assert "scan" in cost.breakdown
