"""Tests for rewrite rules and the RewriteGenerator.

Each rule has ≥3 tests covering:
  - Rule produces valid rewrites
  - Rule returns empty list when not applicable
  - Edge cases

Also tests: dedup, generator, family pruning.
"""

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
    Star,
    UnaryOp,
    UnaryOpKind,
)
from optim.ir.render_sql import render
from optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from optim.ir.types import SemType
from optim.cegis.equivalence import Candidate
from optim.rewrite.rules import (
    RULE_REGISTRY,
    dedup_candidates,
    rule_agg_swap,
    rule_distinct_toggle,
    rule_join_elimination,
    rule_join_reorder,
    rule_predicate_pullup,
    rule_predicate_pushdown,
    rule_projection_minimization,
    rule_redundant_distinct_removal,
    rule_unwrap_abs,
)
from optim.rewrite.generator import RewriteConfig, RewriteGenerator
from optim.rewrite.families import classify_rewrites, prune_families


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def catalog():
    """Simple customers/orders/products catalog."""
    return Catalog(
        tables={
            "customers": TableInfo(
                name="customers",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="name", sem_type=SemType.STRING, nullable=False),
                    ColumnInfo(name="country", sem_type=SemType.STRING, nullable=True),
                ],
                primary_keys=["id"],
            ),
            "orders": TableInfo(
                name="orders",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="customer_id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="amount", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="status", sem_type=SemType.STRING, nullable=True),
                ],
                primary_keys=["id"],
            ),
            "products": TableInfo(
                name="products",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="name", sem_type=SemType.STRING, nullable=False),
                    ColumnInfo(name="price", sem_type=SemType.INT, nullable=False),
                ],
                primary_keys=["id"],
            ),
        },
        foreign_keys=[
            ForeignKey(src_table="orders", src_column="customer_id",
                       dst_table="customers", dst_column="id"),
        ],
    )


def _simple_query() -> QueryIR:
    """SELECT name FROM customers WHERE country = 'US'"""
    return QueryIR(
        select=[ColumnRef(table="customers", column="name")],
        from_table=RelRef(table="customers"),
        where=BinOp(
            op=BinOpKind.EQ,
            left=ColumnRef(table="customers", column="country"),
            right=Literal(value="US"),
        ),
    )


def _join_query() -> QueryIR:
    """SELECT customers.name, orders.amount FROM customers
       JOIN orders ON customers.id = orders.customer_id
       WHERE orders.status = 'shipped' AND customers.country = 'US'
    """
    return QueryIR(
        select=[
            ColumnRef(table="customers", column="name"),
            ColumnRef(table="orders", column="amount"),
        ],
        from_table=RelRef(table="customers"),
        joins=[
            JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="orders"),
                on=BinOp(
                    op=BinOpKind.EQ,
                    left=ColumnRef(table="customers", column="id"),
                    right=ColumnRef(table="orders", column="customer_id"),
                ),
            ),
        ],
        where=BinOp(
            op=BinOpKind.AND,
            left=BinOp(
                op=BinOpKind.EQ,
                left=ColumnRef(table="orders", column="status"),
                right=Literal(value="shipped"),
            ),
            right=BinOp(
                op=BinOpKind.EQ,
                left=ColumnRef(table="customers", column="country"),
                right=Literal(value="US"),
            ),
        ),
    )


def _agg_query() -> QueryIR:
    """SELECT customers.name, COUNT(*), SUM(orders.amount)
       FROM customers JOIN orders ON customers.id = orders.customer_id
       GROUP BY customers.name
    """
    return QueryIR(
        select=[
            ColumnRef(table="customers", column="name"),
            AggCall(func=AggFunc.COUNT, arg=None),
            AggCall(func=AggFunc.SUM, arg=ColumnRef(table="orders", column="amount")),
        ],
        from_table=RelRef(table="customers"),
        joins=[
            JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="orders"),
                on=BinOp(
                    op=BinOpKind.EQ,
                    left=ColumnRef(table="customers", column="id"),
                    right=ColumnRef(table="orders", column="customer_id"),
                ),
            ),
        ],
        group_by=[ColumnRef(table="customers", column="name")],
    )


# ===================================================================
# R1: Predicate pushdown
# ===================================================================

class TestPredicatePushdown:

    def test_pushes_single_table_predicate(self, catalog):
        ir = _join_query()
        results = rule_predicate_pushdown(ir, catalog)
        assert len(results) == 1
        r = results[0]
        # The orders.status predicate should be pushed into the JOIN ON
        # WHERE should only have customers.country predicate
        assert r.where is not None
        sql = render(r, dialect="sqlite")
        assert "shipped" not in (render(QueryIR(select=[Literal(value=1)], from_table=RelRef(table="x"), where=r.where), dialect="sqlite") if r.where else "")

    def test_no_pushdown_without_joins(self, catalog):
        ir = _simple_query()
        results = rule_predicate_pushdown(ir, catalog)
        assert results == []

    def test_no_pushdown_single_conjunct(self, catalog):
        ir = QueryIR(
            select=[ColumnRef(table="customers", column="name")],
            from_table=RelRef(table="customers"),
            joins=[JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="orders"),
                on=BinOp(op=BinOpKind.EQ,
                         left=ColumnRef(table="customers", column="id"),
                         right=ColumnRef(table="orders", column="customer_id")),
            )],
            where=BinOp(op=BinOpKind.EQ,
                         left=ColumnRef(table="customers", column="country"),
                         right=Literal(value="US")),
        )
        results = rule_predicate_pushdown(ir, catalog)
        assert results == []

    def test_pushdown_renders_valid_sql(self, catalog):
        ir = _join_query()
        results = rule_predicate_pushdown(ir, catalog)
        for r in results:
            sql = render(r, dialect="sqlite")
            assert "SELECT" in sql


# ===================================================================
# R2: Predicate pullup
# ===================================================================

class TestPredicatePullup:

    def test_pulls_up_single_table_predicate(self, catalog):
        """ON clause with a predicate referencing only one side → pull to WHERE."""
        ir = QueryIR(
            select=[
                ColumnRef(table="customers", column="name"),
                ColumnRef(table="orders", column="amount"),
            ],
            from_table=RelRef(table="customers"),
            joins=[JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="orders"),
                on=BinOp(
                    op=BinOpKind.AND,
                    left=BinOp(op=BinOpKind.EQ,
                               left=ColumnRef(table="customers", column="id"),
                               right=ColumnRef(table="orders", column="customer_id")),
                    right=BinOp(op=BinOpKind.EQ,
                               left=ColumnRef(table="orders", column="status"),
                               right=Literal(value="shipped")),
                ),
            )],
        )
        results = rule_predicate_pullup(ir, catalog)
        assert len(results) == 1
        assert results[0].where is not None

    def test_no_pullup_all_two_sided(self, catalog):
        ir = QueryIR(
            select=[ColumnRef(table="customers", column="name")],
            from_table=RelRef(table="customers"),
            joins=[JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="orders"),
                on=BinOp(op=BinOpKind.EQ,
                         left=ColumnRef(table="customers", column="id"),
                         right=ColumnRef(table="orders", column="customer_id")),
            )],
        )
        results = rule_predicate_pullup(ir, catalog)
        assert results == []

    def test_no_pullup_for_left_join(self, catalog):
        ir = QueryIR(
            select=[ColumnRef(table="customers", column="name")],
            from_table=RelRef(table="customers"),
            joins=[JoinClause(
                join_type=JoinType.LEFT,
                right=RelRef(table="orders"),
                on=BinOp(
                    op=BinOpKind.AND,
                    left=BinOp(op=BinOpKind.EQ,
                               left=ColumnRef(table="customers", column="id"),
                               right=ColumnRef(table="orders", column="customer_id")),
                    right=BinOp(op=BinOpKind.EQ,
                               left=ColumnRef(table="orders", column="status"),
                               right=Literal(value="shipped")),
                ),
            )],
        )
        results = rule_predicate_pullup(ir, catalog)
        assert results == []


# ===================================================================
# R3: Join elimination
# ===================================================================

class TestJoinElimination:

    def test_eliminates_unused_fk_pk_join(self, catalog):
        """Join to customers where no customer columns are used → eliminate."""
        ir = QueryIR(
            select=[
                ColumnRef(table="orders", column="id"),
                ColumnRef(table="orders", column="amount"),
            ],
            from_table=RelRef(table="orders"),
            joins=[JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="customers"),
                on=BinOp(op=BinOpKind.EQ,
                         left=ColumnRef(table="orders", column="customer_id"),
                         right=ColumnRef(table="customers", column="id")),
            )],
        )
        results = rule_join_elimination(ir, catalog)
        assert len(results) == 1
        assert results[0].joins == []

    def test_no_elimination_when_columns_used(self, catalog):
        ir = _join_query()  # uses customers.name in SELECT
        results = rule_join_elimination(ir, catalog)
        assert results == []

    def test_no_elimination_for_left_join(self, catalog):
        ir = QueryIR(
            select=[ColumnRef(table="orders", column="amount")],
            from_table=RelRef(table="orders"),
            joins=[JoinClause(
                join_type=JoinType.LEFT,
                right=RelRef(table="customers"),
                on=BinOp(op=BinOpKind.EQ,
                         left=ColumnRef(table="orders", column="customer_id"),
                         right=ColumnRef(table="customers", column="id")),
            )],
        )
        results = rule_join_elimination(ir, catalog)
        assert results == []

    def test_no_elimination_without_fk(self, catalog):
        """Join to products (no FK from orders) → don't eliminate."""
        ir = QueryIR(
            select=[ColumnRef(table="orders", column="amount")],
            from_table=RelRef(table="orders"),
            joins=[JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="products"),
                on=BinOp(op=BinOpKind.EQ,
                         left=ColumnRef(table="orders", column="id"),
                         right=ColumnRef(table="products", column="id")),
            )],
        )
        results = rule_join_elimination(ir, catalog)
        assert results == []


# ===================================================================
# R4: Redundant DISTINCT removal
# ===================================================================

class TestRedundantDistinctRemoval:

    def test_removes_distinct_when_pk_in_select(self, catalog):
        ir = QueryIR(
            select=[
                ColumnRef(table="customers", column="id"),
                ColumnRef(table="customers", column="name"),
            ],
            from_table=RelRef(table="customers"),
            distinct=True,
        )
        results = rule_redundant_distinct_removal(ir, catalog)
        assert len(results) == 1
        assert results[0].distinct is False

    def test_no_removal_without_pk(self, catalog):
        ir = QueryIR(
            select=[ColumnRef(table="customers", column="name")],
            from_table=RelRef(table="customers"),
            distinct=True,
        )
        results = rule_redundant_distinct_removal(ir, catalog)
        assert results == []

    def test_no_removal_with_joins(self, catalog):
        ir = QueryIR(
            select=[
                ColumnRef(table="customers", column="id"),
                ColumnRef(table="customers", column="name"),
            ],
            from_table=RelRef(table="customers"),
            joins=[JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="orders"),
                on=BinOp(op=BinOpKind.EQ,
                         left=ColumnRef(table="customers", column="id"),
                         right=ColumnRef(table="orders", column="customer_id")),
            )],
            distinct=True,
        )
        results = rule_redundant_distinct_removal(ir, catalog)
        assert results == []

    def test_no_removal_when_not_distinct(self, catalog):
        ir = QueryIR(
            select=[ColumnRef(table="customers", column="id")],
            from_table=RelRef(table="customers"),
            distinct=False,
        )
        results = rule_redundant_distinct_removal(ir, catalog)
        assert results == []


# ===================================================================
# R5: Join reordering
# ===================================================================

class TestJoinReorder:

    def test_reorders_two_inner_joins(self, catalog):
        ir = QueryIR(
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
        results = rule_join_reorder(ir, catalog)
        # Should produce at least 1 reordering (swap from_table and join)
        assert len(results) >= 1
        # Each result should render to valid SQL
        for r in results:
            sql = render(r, dialect="sqlite")
            assert "SELECT" in sql

    def test_no_reorder_for_left_join(self, catalog):
        ir = QueryIR(
            select=[ColumnRef(table="customers", column="name")],
            from_table=RelRef(table="customers"),
            joins=[JoinClause(
                join_type=JoinType.LEFT,
                right=RelRef(table="orders"),
                on=BinOp(op=BinOpKind.EQ,
                         left=ColumnRef(table="customers", column="id"),
                         right=ColumnRef(table="orders", column="customer_id")),
            )],
        )
        results = rule_join_reorder(ir, catalog)
        assert results == []

    def test_no_reorder_without_joins(self, catalog):
        ir = _simple_query()
        results = rule_join_reorder(ir, catalog)
        assert results == []


# ===================================================================
# R6: Projection minimization
# ===================================================================

class TestProjectionMinimization:

    def test_strips_extra_columns(self, catalog):
        ir = _agg_query()  # name, COUNT(*), SUM(amount) — name is in group_by
        # Add an extra non-agg column not in group_by
        ir.select.append(ColumnRef(table="customers", column="country"))
        results = rule_projection_minimization(ir, catalog)
        assert len(results) == 1
        # Should keep COUNT(*), SUM(amount), and name (group_by)
        r = results[0]
        agg_count = sum(1 for s in r.select if isinstance(s, AggCall))
        assert agg_count == 2

    def test_no_minimization_without_aggregation(self, catalog):
        ir = _simple_query()
        results = rule_projection_minimization(ir, catalog)
        assert results == []

    def test_no_minimization_when_already_minimal(self, catalog):
        ir = QueryIR(
            select=[AggCall(func=AggFunc.COUNT, arg=None)],
            from_table=RelRef(table="customers"),
        )
        results = rule_projection_minimization(ir, catalog)
        assert results == []


# ===================================================================
# Seeded rules: DISTINCT toggle, agg swap, unwrap ABS
# ===================================================================

class TestDistinctToggle:

    def test_toggles_on(self, catalog):
        ir = _simple_query()
        assert not ir.distinct
        results = rule_distinct_toggle(ir, catalog)
        assert len(results) == 1
        assert results[0].distinct is True

    def test_toggles_off(self, catalog):
        ir = _simple_query()
        ir.distinct = True
        results = rule_distinct_toggle(ir, catalog)
        assert len(results) == 1
        assert results[0].distinct is False

    def test_does_not_mutate_original(self, catalog):
        ir = _simple_query()
        rule_distinct_toggle(ir, catalog)
        assert ir.distinct is False


class TestAggSwap:

    def test_count_star_to_count_distinct(self, catalog):
        ir = _agg_query()
        results = rule_agg_swap(ir, catalog)
        assert any(
            isinstance(r.select[1], AggCall) and r.select[1].distinct
            for r in results
        )

    def test_sum_to_avg(self, catalog):
        ir = _agg_query()
        results = rule_agg_swap(ir, catalog)
        assert any(
            isinstance(r.select[2], AggCall) and r.select[2].func == AggFunc.AVG
            for r in results
        )

    def test_no_swap_without_aggregates(self, catalog):
        ir = _simple_query()
        results = rule_agg_swap(ir, catalog)
        assert results == []


# ===================================================================
# Dedup
# ===================================================================

class TestDedup:

    def test_removes_shape_duplicates(self, catalog):
        ir = _simple_query()
        candidates = [
            Candidate(id="a", ir=ir, confidence=0.9, source="test"),
            Candidate(id="b", ir=ir.model_copy(deep=True), confidence=0.8, source="test"),
        ]
        result = dedup_candidates(candidates)
        assert len(result) == 1
        assert result[0].id == "a"

    def test_keeps_distinct_shapes(self, catalog):
        ir1 = _simple_query()
        ir2 = _simple_query()
        ir2.distinct = True
        candidates = [
            Candidate(id="a", ir=ir1, confidence=0.9, source="test"),
            Candidate(id="b", ir=ir2, confidence=0.8, source="test"),
        ]
        result = dedup_candidates(candidates)
        assert len(result) == 2


# ===================================================================
# RewriteGenerator
# ===================================================================

class TestRewriteGenerator:

    def test_generates_candidates(self, catalog):
        gen = RewriteGenerator(catalog)
        ir = _join_query()
        candidates = gen.generate(ir)
        assert len(candidates) > 0
        # All candidates should have source set
        for c in candidates:
            assert c.source is not None

    def test_respects_max_total(self, catalog):
        config = RewriteConfig(max_total_candidates=3)
        gen = RewriteGenerator(catalog, config)
        ir = _join_query()
        candidates = gen.generate(ir)
        assert len(candidates) <= 3

    def test_all_candidates_render_to_sql(self, catalog):
        gen = RewriteGenerator(catalog)
        ir = _join_query()
        candidates = gen.generate(ir)
        for c in candidates:
            sql = render(c.ir, dialect="sqlite")
            assert "SELECT" in sql

    def test_simple_query_gets_some_rewrites(self, catalog):
        gen = RewriteGenerator(catalog, RewriteConfig(
            enabled_rules=["distinct_toggle", "R4"],
        ))
        ir = _simple_query()
        candidates = gen.generate(ir)
        assert len(candidates) >= 1


# ===================================================================
# Family classification + pruning
# ===================================================================

class TestFamilies:

    def test_classify_groups_by_structural_signature(self, catalog):
        """Candidates from the same rule share a family."""
        candidates = [
            Candidate(id="R1_0", ir=_simple_query(), confidence=0.8, source="R1"),
            Candidate(id="R1_1", ir=_simple_query(), confidence=0.8, source="R1"),
            Candidate(id="R5_0", ir=_simple_query(), confidence=0.8, source="R5"),
        ]
        families = classify_rewrites(candidates)
        # R1_0 and R1_1 share a family; R5_0 is separate
        assert len(families) == 2

    def test_prune_family_prunes_siblings(self, catalog):
        """Rejecting one candidate prunes siblings sharing the structural defect."""
        candidates = [
            Candidate(id="R1_0", ir=_simple_query(), confidence=0.8, source="R1"),
            Candidate(id="R1_1", ir=_simple_query(), confidence=0.8, source="R1"),
            Candidate(id="R5_0", ir=_simple_query(), confidence=0.8, source="R5"),
        ]
        families = classify_rewrites(candidates)
        pruned = prune_families(families, rejected_ids={"R1_0"})
        assert "R1_0" in pruned
        assert "R1_1" in pruned  # sibling pruned
        assert "R5_0" not in pruned  # different family

    def test_prune_empty_when_no_rejections(self, catalog):
        candidates = [
            Candidate(id="R1_0", ir=_simple_query(), confidence=0.8, source="R1"),
        ]
        families = classify_rewrites(candidates)
        pruned = prune_families(families, rejected_ids=set())
        assert len(pruned) == 0
