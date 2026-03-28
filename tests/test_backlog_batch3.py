"""Tests for backlog batch 3: E.5, D.5, D.7, D.8, P8.6."""

import datetime
from unittest.mock import patch

import pytest

from optim.ir.types import (
    BinOp,
    BinOpKind,
    ColumnRef,
    ExistsSubquery,
    JoinClause,
    JoinType,
    Literal,
    QueryIR,
    RelRef,
    SemType,
)
from optim.parser.sql_to_ir import _date_to_epoch_day
from optim.cegis.compositional import (
    InterfaceColumn,
    LocalRegion,
    _build_interface_columns,
    _extract_join_elimination_region,
    _extract_subquery_decorrelation_region,
    _narrow_interface_columns,
)
from optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from optim.cost.duckdb_explain import DuckDBExplainCostEstimator
from optim.cost.estimator import CostEstimate


# ===================================================================
# E.5 — Date normalization
# ===================================================================


class TestDateToEpochDay:
    def test_date_to_epoch_day(self):
        """Verify '2025-03-10' → correct epoch-day integer."""
        expected = (datetime.date(2025, 3, 10) - datetime.date(1970, 1, 1)).days
        assert _date_to_epoch_day("2025-03-10") == expected

    def test_epoch(self):
        """1970-01-01 should be day 0."""
        assert _date_to_epoch_day("1970-01-01") == 0

    def test_invalid_date(self):
        """Invalid date strings return None."""
        assert _date_to_epoch_day("not-a-date") is None
        assert _date_to_epoch_day("2025-13-40") is None
        assert _date_to_epoch_day("") is None


# ===================================================================
# D.5 — Advanced interface narrowing
# ===================================================================


def _make_catalog_for_narrowing():
    return Catalog(
        tables={
            "orders": TableInfo(
                name="orders",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="customer_id", sem_type=SemType.INT),
                    ColumnInfo(name="total", sem_type=SemType.FLOAT),
                    ColumnInfo(name="status", sem_type=SemType.STRING),
                ],
                primary_keys=["id"],
                unique_columns=["id"],
            ),
            "items": TableInfo(
                name="items",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="order_id", sem_type=SemType.INT),
                    ColumnInfo(name="product", sem_type=SemType.STRING),
                    ColumnInfo(name="qty", sem_type=SemType.INT),
                ],
                primary_keys=["id"],
            ),
        },
        foreign_keys=[
            ForeignKey(src_table="items", src_column="order_id", dst_table="orders", dst_column="id"),
        ],
    )


class TestNarrowInterface:
    def test_narrow_interface_basic(self):
        """Externally referenced columns narrow the interface."""
        catalog = _make_catalog_for_narrowing()

        # Build a query that only references orders.total in SELECT
        ir = QueryIR(
            select=[ColumnRef(table="orders", column="total", sem_type=SemType.FLOAT)],
            from_table=RelRef(table="orders"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="items"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="orders", column="id"),
                        right=ColumnRef(table="items", column="order_id"),
                    ),
                ),
            ],
        )

        # Build full interface columns for orders (boundary)
        from optim.cegis.compositional import _alias_to_relation
        alias_to_rel = _alias_to_relation(ir)
        full_interface = _build_interface_columns({"orders"}, alias_to_rel, catalog)

        # All 4 orders columns in full interface
        assert len(full_interface) == 4

        narrowed = _narrow_interface_columns(
            full_interface,
            ir,
            local_aliases={"orders", "items"},
            boundary_aliases={"orders"},
            catalog=catalog,
        )

        # Should be narrowed: total (from SELECT) + id (PK/UNIQUE)
        narrowed_names = {ic.column_name.lower() for ic in narrowed}
        assert "total" in narrowed_names
        assert "id" in narrowed_names
        assert len(narrowed) < len(full_interface)

    def test_narrow_interface_preserves_pk(self):
        """PK columns are always kept even if not externally referenced."""
        catalog = _make_catalog_for_narrowing()

        # Query that doesn't reference orders.id in SELECT at all
        ir = QueryIR(
            select=[ColumnRef(table="orders", column="status", sem_type=SemType.STRING)],
            from_table=RelRef(table="orders"),
        )

        from optim.cegis.compositional import _alias_to_relation
        alias_to_rel = _alias_to_relation(ir)
        full_interface = _build_interface_columns({"orders"}, alias_to_rel, catalog)

        narrowed = _narrow_interface_columns(
            full_interface,
            ir,
            local_aliases={"orders"},
            boundary_aliases={"orders"},
            catalog=catalog,
        )

        narrowed_names = {ic.column_name.lower() for ic in narrowed}
        # PK 'id' should always be present
        assert "id" in narrowed_names
        # 'status' from SELECT should be present
        assert "status" in narrowed_names


# ===================================================================
# D.7 — Join elimination detection
# ===================================================================


def _make_catalog_for_join_elim():
    return Catalog(
        tables={
            "orders": TableInfo(
                name="orders",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="customer_id", sem_type=SemType.INT),
                    ColumnInfo(name="total", sem_type=SemType.FLOAT),
                ],
                primary_keys=["id"],
            ),
            "customers": TableInfo(
                name="customers",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="name", sem_type=SemType.STRING),
                ],
                primary_keys=["id"],
            ),
        },
        foreign_keys=[
            ForeignKey(
                src_table="orders", src_column="customer_id",
                dst_table="customers", dst_column="id",
            ),
        ],
    )


class TestJoinEliminationDetection:
    def test_join_elimination_detection(self):
        """Original has extra join that was eliminated → detected."""
        catalog = _make_catalog_for_join_elim()

        # Original: SELECT orders.total FROM orders JOIN customers ON orders.customer_id = customers.id
        original = QueryIR(
            select=[ColumnRef(table="orders", column="total")],
            from_table=RelRef(table="orders"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="customers"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="orders", column="customer_id"),
                        right=ColumnRef(table="customers", column="id"),
                    ),
                ),
            ],
        )

        # Rewrite: SELECT orders.total FROM orders (customers join eliminated)
        rewrite = QueryIR(
            select=[ColumnRef(table="orders", column="total")],
            from_table=RelRef(table="orders"),
            joins=[],
        )

        region = _extract_join_elimination_region(original, rewrite, catalog)
        assert region is not None
        assert region.proof_kind == "join_elimination"
        assert "customers" in region.local_aliases

    def test_no_elimination_same_joins(self):
        """Same number of joins → not a join elimination."""
        catalog = _make_catalog_for_join_elim()
        ir = QueryIR(
            select=[ColumnRef(table="orders", column="total")],
            from_table=RelRef(table="orders"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="customers"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="orders", column="customer_id"),
                        right=ColumnRef(table="customers", column="id"),
                    ),
                ),
            ],
        )
        assert _extract_join_elimination_region(ir, ir, catalog) is None


# ===================================================================
# D.8 — Subquery decorrelation detection
# ===================================================================


class TestSubqueryDecorrelationDetection:
    def test_subquery_decorrelation_detection(self):
        """Original has EXISTS subquery → detected as decorrelation."""
        catalog = _make_catalog_for_join_elim()

        # Original: SELECT orders.total FROM orders
        #           WHERE EXISTS (SELECT 1 FROM customers WHERE customers.id = orders.customer_id)
        original = QueryIR(
            select=[ColumnRef(table="orders", column="total")],
            from_table=RelRef(table="orders"),
            joins=[],
            where=ExistsSubquery(
                query=QueryIR(
                    select=[Literal(value=1)],
                    from_table=RelRef(table="customers"),
                    where=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="customers", column="id"),
                        right=ColumnRef(table="orders", column="customer_id"),
                    ),
                ),
            ),
        )

        # Rewrite: SELECT DISTINCT orders.total FROM orders
        #          JOIN customers ON customers.id = orders.customer_id
        rewrite = QueryIR(
            select=[ColumnRef(table="orders", column="total")],
            from_table=RelRef(table="orders"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="customers"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="customers", column="id"),
                        right=ColumnRef(table="orders", column="customer_id"),
                    ),
                ),
            ],
            distinct=True,
        )

        region = _extract_subquery_decorrelation_region(original, rewrite, catalog)
        assert region is not None
        assert region.proof_kind == "subquery_decorrelation"
        assert "customers" in region.local_aliases

    def test_no_decorrelation_no_subquery(self):
        """No subquery in original → not a decorrelation."""
        catalog = _make_catalog_for_join_elim()
        ir = QueryIR(
            select=[ColumnRef(table="orders", column="total")],
            from_table=RelRef(table="orders"),
        )
        assert _extract_subquery_decorrelation_region(ir, ir, catalog) is None


# ===================================================================
# P8.6 — DuckDB EXPLAIN cost model
# ===================================================================


class TestDuckDBEstimatorFallback:
    def test_duckdb_estimator_fallback(self):
        """Graceful fallback when duckdb is unavailable."""
        estimator = DuckDBExplainCostEstimator()

        # Mock duckdb import to fail
        with patch.object(estimator, "_get_duckdb", side_effect=ImportError("no duckdb")):
            catalog = Catalog(
                tables={
                    "t": TableInfo(
                        name="t",
                        columns=[ColumnInfo(name="id", sem_type=SemType.INT)],
                    ),
                },
            )
            ir = QueryIR(
                select=[ColumnRef(table="t", column="id")],
                from_table=RelRef(table="t"),
            )
            result = estimator.estimate(ir, catalog)
            assert result.total_cost == float("inf")
            assert "unavailable" in result.source
