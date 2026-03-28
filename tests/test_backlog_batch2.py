"""Tests for backlog batch 2: R10, R11, A.3, D.14."""

import pytest

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
    SemType,
    SortDir,
    SortSpec,
    WindowFunc,
)
from optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from optim.rewrite.rules import rule_self_join_collapse, rule_cte_inline
from optim.cegis.result_shape import classify_result_shape, ResultShape
from optim.cegis.compositional import (
    check_region_independence,
    DecompositionPlan,
    LocalRegion,
    MoveGroup,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def catalog():
    return Catalog(
        tables={
            "orders": TableInfo(
                name="orders",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="customer_id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="amount", sem_type=SemType.INT, nullable=False),
                ],
                primary_keys=["id"],
            ),
            "customers": TableInfo(
                name="customers",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="name", sem_type=SemType.STRING, nullable=False),
                ],
                primary_keys=["id"],
            ),
        },
        foreign_keys=[],
    )


def _dummy_local_query():
    """A trivial QueryIR used as placeholder in LocalRegion."""
    return QueryIR(
        select=[ColumnRef(table="t", column="id")],
        from_table=RelRef(table="t"),
    )


# ---------------------------------------------------------------------------
# R10: Redundant self-join collapse
# ---------------------------------------------------------------------------

class TestSelfJoinCollapse:
    def test_self_join_collapse_basic(self, catalog):
        """Two INNER JOINs on same table, second alias not in SELECT → collapsed."""
        # SELECT o1.amount FROM orders base
        #   JOIN orders o1 ON base.id = o1.id
        #   JOIN orders o2 ON base.id = o2.id
        # WHERE o2.amount > 100
        ir = QueryIR(
            select=[ColumnRef(table="o1", column="amount")],
            from_table=RelRef(table="orders", alias="base"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="orders", alias="o1"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="base", column="id"),
                        right=ColumnRef(table="o1", column="id"),
                    ),
                ),
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="orders", alias="o2"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="base", column="id"),
                        right=ColumnRef(table="o2", column="id"),
                    ),
                ),
            ],
            where=BinOp(
                op=BinOpKind.GT,
                left=ColumnRef(table="o2", column="amount"),
                right=Literal(value=100),
            ),
        )
        results = rule_self_join_collapse(ir, catalog)
        assert len(results) >= 1
        # The collapsed result should have only 1 join
        collapsed = results[0]
        assert len(collapsed.joins) == 1

    def test_self_join_different_tables_skipped(self, catalog):
        """Two joins on different tables → []."""
        ir = QueryIR(
            select=[ColumnRef(table="o", column="amount")],
            from_table=RelRef(table="customers", alias="c"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="orders", alias="o"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="c", column="id"),
                        right=ColumnRef(table="o", column="customer_id"),
                    ),
                ),
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="customers", alias="c2"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="o", column="customer_id"),
                        right=ColumnRef(table="c2", column="id"),
                    ),
                ),
            ],
        )
        results = rule_self_join_collapse(ir, catalog)
        assert results == []

    def test_self_join_both_referenced_skipped(self, catalog):
        """Both aliases in SELECT → []."""
        ir = QueryIR(
            select=[
                ColumnRef(table="o1", column="amount"),
                ColumnRef(table="o2", column="amount"),
            ],
            from_table=RelRef(table="customers", alias="c"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="orders", alias="o1"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="c", column="id"),
                        right=ColumnRef(table="o1", column="customer_id"),
                    ),
                ),
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="orders", alias="o2"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="c", column="id"),
                        right=ColumnRef(table="o2", column="customer_id"),
                    ),
                ),
            ],
        )
        results = rule_self_join_collapse(ir, catalog)
        assert results == []


# ---------------------------------------------------------------------------
# R11: CTE inlining stub
# ---------------------------------------------------------------------------

class TestCTEInline:
    def test_cte_inline_stub(self, catalog):
        ir = QueryIR(
            select=[ColumnRef(table="orders", column="id")],
            from_table=RelRef(table="orders"),
        )
        assert rule_cte_inline(ir, catalog) == []


# ---------------------------------------------------------------------------
# A.3: Result-shape classifier
# ---------------------------------------------------------------------------

class TestResultShape:
    def test_classify_scalar_agg(self):
        """COUNT(*) no GROUP BY → SCALAR_AGG."""
        ir = QueryIR(
            select=[AggCall(func=AggFunc.COUNT, arg=None)],
            from_table=RelRef(table="orders"),
        )
        assert classify_result_shape(ir) == ResultShape.SCALAR_AGG

    def test_classify_grouped_agg(self):
        """With GROUP BY → GROUPED_AGG."""
        ir = QueryIR(
            select=[
                ColumnRef(table="orders", column="customer_id"),
                AggCall(func=AggFunc.SUM, arg=ColumnRef(table="orders", column="amount")),
            ],
            from_table=RelRef(table="orders"),
            group_by=[ColumnRef(table="orders", column="customer_id")],
        )
        assert classify_result_shape(ir) == ResultShape.GROUPED_AGG

    def test_classify_bag_rows(self):
        """Simple SELECT → BAG_ROWS."""
        ir = QueryIR(
            select=[ColumnRef(table="orders", column="id")],
            from_table=RelRef(table="orders"),
        )
        assert classify_result_shape(ir) == ResultShape.BAG_ROWS

    def test_classify_topk(self):
        """ORDER BY + LIMIT → TOPK."""
        ir = QueryIR(
            select=[ColumnRef(table="orders", column="id")],
            from_table=RelRef(table="orders"),
            order_by=[SortSpec(expr=ColumnRef(table="orders", column="amount"), direction=SortDir.DESC)],
            limit=10,
        )
        assert classify_result_shape(ir) == ResultShape.TOPK

    def test_classify_windowed(self):
        """Window function in SELECT → WINDOWED."""
        ir = QueryIR(
            select=[
                ColumnRef(table="orders", column="id"),
                WindowFunc(
                    func_name="ROW_NUMBER",
                    args=[],
                    order_by=[SortSpec(expr=ColumnRef(table="orders", column="amount"))],
                ),
            ],
            from_table=RelRef(table="orders"),
        )
        assert classify_result_shape(ir) == ResultShape.WINDOWED


# ---------------------------------------------------------------------------
# D.14: Region independence check
# ---------------------------------------------------------------------------

class TestRegionIndependence:
    def test_independent_regions(self):
        """Two regions with disjoint internal aliases → True."""
        r1 = LocalRegion(
            join_idx=0,
            local_aliases={"a", "b", "shared"},
            boundary_aliases={"shared"},
            interface_columns=[],
            original_local=_dummy_local_query(),
            rewrite_local=_dummy_local_query(),
            move_group=MoveGroup(
                join_idx=0,
                moved_to_on=[
                    BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="a", column="id"),
                        right=ColumnRef(table="shared", column="id"),
                    ),
                ],
                moved_to_where=[],
                structural_on=[],
            ),
        )
        r2 = LocalRegion(
            join_idx=1,
            local_aliases={"c", "d", "shared"},
            boundary_aliases={"shared"},
            interface_columns=[],
            original_local=_dummy_local_query(),
            rewrite_local=_dummy_local_query(),
            move_group=MoveGroup(
                join_idx=1,
                moved_to_on=[
                    BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="c", column="id"),
                        right=ColumnRef(table="shared", column="id"),
                    ),
                ],
                moved_to_where=[],
                structural_on=[],
            ),
        )
        plan = DecompositionPlan(regions=[r1, r2])
        independent, reason = check_region_independence(plan)
        assert independent is True
        assert reason is None

    def test_overlapping_regions(self):
        """Two regions sharing internal alias → False."""
        r1 = LocalRegion(
            join_idx=0,
            local_aliases={"a", "overlap"},
            boundary_aliases={"a"},
            interface_columns=[],
            original_local=_dummy_local_query(),
            rewrite_local=_dummy_local_query(),
        )
        r2 = LocalRegion(
            join_idx=1,
            local_aliases={"b", "overlap"},
            boundary_aliases={"b"},
            interface_columns=[],
            original_local=_dummy_local_query(),
            rewrite_local=_dummy_local_query(),
        )
        plan = DecompositionPlan(regions=[r1, r2])
        independent, reason = check_region_independence(plan)
        assert independent is False
        assert "overlapping internal aliases" in reason
