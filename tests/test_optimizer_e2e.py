"""End-to-end optimizer tests with verifier-validated equivalence.

For each rewrite rule, apply it to a hand-built IR, then run
`synthesize_witness(original_ir, rewrite_ir, catalog, scope)` to confirm
semantic equivalence (UNSAT) or non-equivalence (SAT).

Test categories:
  A. Verifier-validated rewrite equivalence (rule by rule)
  B. Verifier catches unsound rewrites
  C. Full optimize() E2E
"""

import pytest

from optim.cegis.witness_export import validate_witness
from optim.cegis.witness_synthesis import synthesize_witness
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
)
from optim.optimizer.loop import optimize
from optim.rewrite.generator import RewriteConfig
from optim.rewrite.rules import (
    rule_agg_swap,
    rule_distinct_toggle,
    rule_join_reorder,
    rule_predicate_pullup,
    rule_predicate_pushdown,
    rule_redundant_distinct_removal,
)
from optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from optim.verify.encode_z3 import BoundedScope


# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture
def catalog():
    return Catalog(
        tables={
            "customers": TableInfo(name="customers", columns=[
                ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                ColumnInfo(name="name", sem_type=SemType.STRING, nullable=False),
                ColumnInfo(name="country", sem_type=SemType.STRING, nullable=True),
            ], primary_keys=["id"]),
            "orders": TableInfo(name="orders", columns=[
                ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                ColumnInfo(name="customer_id", sem_type=SemType.INT, nullable=False),
                ColumnInfo(name="amount", sem_type=SemType.INT, nullable=False),
                ColumnInfo(name="status", sem_type=SemType.STRING, nullable=True),
            ], primary_keys=["id"]),
        },
        foreign_keys=[
            ForeignKey(src_table="orders", src_column="customer_id",
                       dst_table="customers", dst_column="id"),
        ],
    )


@pytest.fixture
def scope():
    return BoundedScope(k_rows=2, int_bounds=(-2, 5))


# ===================================================================
# A. Verifier-validated rewrite equivalence (rule by rule)
# ===================================================================

class TestRewriteEquivalence:
    """Apply each sound rule and confirm synthesize_witness returns UNSAT."""

    def test_r1_predicate_pushdown_unsat(self, catalog, scope):
        """R1: pushing a WHERE conjunct into an INNER JOIN ON is semantics-preserving."""
        # SELECT customers.name, orders.amount
        # FROM customers
        # JOIN orders ON customers.id = orders.customer_id
        # WHERE orders.status = 'shipped' AND customers.country = 'US'
        ir = QueryIR(
            select=[
                ColumnRef(table="customers", column="name", sem_type=SemType.STRING),
                ColumnRef(table="orders", column="amount", sem_type=SemType.INT),
            ],
            from_table=RelRef(table="customers"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="orders"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="customers", column="id", sem_type=SemType.INT),
                        right=ColumnRef(table="orders", column="customer_id", sem_type=SemType.INT),
                    ),
                ),
            ],
            where=BinOp(
                op=BinOpKind.AND,
                left=BinOp(
                    op=BinOpKind.EQ,
                    left=ColumnRef(table="orders", column="status", sem_type=SemType.STRING),
                    right=Literal(value="shipped"),
                ),
                right=BinOp(
                    op=BinOpKind.EQ,
                    left=ColumnRef(table="customers", column="country", sem_type=SemType.STRING),
                    right=Literal(value="US"),
                ),
            ),
        )

        rewrites = rule_predicate_pushdown(ir, catalog)
        assert len(rewrites) >= 1, "R1 should produce at least one rewrite"

        result = synthesize_witness(ir, rewrites[0], catalog, scope)
        assert result.status == "unsat", (
            f"Predicate pushdown should be semantics-preserving, got {result.status}"
        )

    def test_r2_predicate_pullup_unsat(self, catalog, scope):
        """R2: pulling a non-join-key ON conjunct to WHERE on INNER JOIN is preserving."""
        # SELECT customers.name, orders.amount
        # FROM customers
        # JOIN orders ON customers.id = orders.customer_id AND orders.status = 'shipped'
        ir = QueryIR(
            select=[
                ColumnRef(table="customers", column="name", sem_type=SemType.STRING),
                ColumnRef(table="orders", column="amount", sem_type=SemType.INT),
            ],
            from_table=RelRef(table="customers"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="orders"),
                    on=BinOp(
                        op=BinOpKind.AND,
                        left=BinOp(
                            op=BinOpKind.EQ,
                            left=ColumnRef(table="customers", column="id", sem_type=SemType.INT),
                            right=ColumnRef(table="orders", column="customer_id", sem_type=SemType.INT),
                        ),
                        right=BinOp(
                            op=BinOpKind.EQ,
                            left=ColumnRef(table="orders", column="status", sem_type=SemType.STRING),
                            right=Literal(value="shipped"),
                        ),
                    ),
                ),
            ],
        )

        rewrites = rule_predicate_pullup(ir, catalog)
        assert len(rewrites) >= 1, "R2 should produce at least one rewrite"

        result = synthesize_witness(ir, rewrites[0], catalog, scope)
        assert result.status == "unsat", (
            f"Predicate pullup on INNER JOIN should be preserving, got {result.status}"
        )

    def test_r4_distinct_removal_on_pk_unsat(self, catalog, scope):
        """R4: removing DISTINCT when PK is in SELECT is semantics-preserving."""
        # SELECT DISTINCT id, name FROM customers
        ir = QueryIR(
            select=[
                ColumnRef(table="customers", column="id", sem_type=SemType.INT),
                ColumnRef(table="customers", column="name", sem_type=SemType.STRING),
            ],
            from_table=RelRef(table="customers"),
            distinct=True,
        )

        rewrites = rule_redundant_distinct_removal(ir, catalog)
        assert len(rewrites) >= 1, "R4 should fire when PK is in SELECT"

        result = synthesize_witness(ir, rewrites[0], catalog, scope)
        assert result.status == "unsat", (
            f"DISTINCT removal with PK should be preserving, got {result.status}"
        )

    def test_r5_join_reorder_unsat(self, catalog, scope):
        """R5: reordering INNER JOINs is semantics-preserving."""
        # SELECT customers.name, orders.amount
        # FROM customers
        # JOIN orders ON customers.id = orders.customer_id
        ir = QueryIR(
            select=[
                ColumnRef(table="customers", column="name", sem_type=SemType.STRING),
                ColumnRef(table="orders", column="amount", sem_type=SemType.INT),
            ],
            from_table=RelRef(table="customers"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="orders"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="customers", column="id", sem_type=SemType.INT),
                        right=ColumnRef(table="orders", column="customer_id", sem_type=SemType.INT),
                    ),
                ),
            ],
        )

        rewrites = rule_join_reorder(ir, catalog)
        assert len(rewrites) >= 1, "R5 should produce at least one rewrite"

        result = synthesize_witness(ir, rewrites[0], catalog, scope)
        assert result.status == "unsat", (
            f"Join reorder on INNER JOINs should be preserving, got {result.status}"
        )


# ===================================================================
# B. Verifier catches unsound rewrites
# ===================================================================

class TestUnsoundRewriteDetection:
    """Apply known-unsound rewrites and confirm the verifier catches them (SAT)."""

    def test_distinct_toggle_on_join_sat(self, catalog, scope):
        """Toggling DISTINCT off on a join that fans out → SAT (not equivalent)."""
        # SELECT DISTINCT customers.name
        # FROM customers
        # JOIN orders ON customers.id = orders.customer_id
        ir = QueryIR(
            select=[
                ColumnRef(table="customers", column="name", sem_type=SemType.STRING),
            ],
            from_table=RelRef(table="customers"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="orders"),
                    on=BinOp(
                        op=BinOpKind.EQ,
                        left=ColumnRef(table="customers", column="id", sem_type=SemType.INT),
                        right=ColumnRef(table="orders", column="customer_id", sem_type=SemType.INT),
                    ),
                ),
            ],
            distinct=True,
        )

        rewrites = rule_distinct_toggle(ir, catalog)
        assert len(rewrites) >= 1
        rewrite_ir = rewrites[0]
        assert rewrite_ir.distinct is False

        result = synthesize_witness(ir, rewrite_ir, catalog, scope)
        assert result.status == "sat", (
            f"Removing DISTINCT on fan-out join should be non-equivalent, got {result.status}"
        )
        assert result.witness_db is not None

        val = validate_witness(ir, rewrite_ir, result.witness_db, catalog)
        assert val.results_differ is True, "Witness DB should demonstrate query disagreement"

    def test_agg_swap_count_star_vs_count_distinct_sat(self, catalog, scope):
        """COUNT(*) vs COUNT(DISTINCT col) → SAT when table has duplicates possible."""
        # SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id
        # (needs a ColumnRef so agg_swap can build COUNT(DISTINCT col))
        ir = QueryIR(
            select=[
                ColumnRef(table="orders", column="customer_id", sem_type=SemType.INT),
                AggCall(func=AggFunc.COUNT, arg=None, distinct=False),
            ],
            from_table=RelRef(table="orders"),
            group_by=[
                ColumnRef(table="orders", column="customer_id", sem_type=SemType.INT),
            ],
        )

        rewrites = rule_agg_swap(ir, catalog)
        # Find the COUNT(DISTINCT col) variant
        count_distinct = [
            r for r in rewrites
            if any(
                isinstance(s, AggCall) and s.distinct
                for s in r.select
            )
        ]
        assert len(count_distinct) >= 1, "agg_swap should produce COUNT(DISTINCT) variant"
        rewrite_ir = count_distinct[0]

        result = synthesize_witness(ir, rewrite_ir, catalog, scope)
        assert result.status == "sat", (
            f"COUNT(*) vs COUNT(DISTINCT) should be non-equivalent, got {result.status}"
        )
        assert result.witness_db is not None

        val = validate_witness(ir, rewrite_ir, result.witness_db, catalog)
        assert val.results_differ is True, "Witness DB should demonstrate query disagreement"


# ===================================================================
# C. Full optimize() E2E
# ===================================================================

class TestOptimizeE2E:
    """End-to-end tests through the optimize() entry point."""

    def test_optimize_join_query_with_r1_r2_r3(self, catalog, scope):
        """optimize() with R1+R2+R3 on a join query: certificate when improved."""
        result = optimize(
            "SELECT customers.name, orders.amount "
            "FROM customers "
            "JOIN orders ON customers.id = orders.customer_id "
            "WHERE orders.status = 'shipped' AND customers.country = 'US'",
            catalog,
            scope=scope,
            rewrite_config=RewriteConfig(enabled_rules=["R1", "R2", "R3"]),
        )
        assert result.total_candidates > 0
        if result.improved:
            assert result.certificate is not None, (
                "An improved result should carry a certificate"
            )

    def test_optimize_rejected_non_equivalent_have_witness(self, catalog, scope):
        """Rejected rewrites with reason='non_equivalent' should have witness_db."""
        result = optimize(
            "SELECT DISTINCT customers.name "
            "FROM customers "
            "JOIN orders ON customers.id = orders.customer_id",
            catalog,
            scope=scope,
            rewrite_config=RewriteConfig(enabled_rules=["distinct_toggle"]),
        )
        rejected_non_eq = [
            r for r in result.rejected if r.reason == "non_equivalent"
        ]
        for rej in rejected_non_eq:
            assert rej.witness_db is not None, (
                "Non-equivalent rejection should include a witness DB"
            )

    def test_optimize_total_candidates_accounting(self, catalog, scope):
        """total_candidates >= n_verified + n_rejected (accounting for pruning)."""
        result = optimize(
            "SELECT customers.name, orders.amount "
            "FROM customers "
            "JOIN orders ON customers.id = orders.customer_id "
            "WHERE orders.status = 'shipped' AND customers.country = 'US'",
            catalog,
            scope=scope,
            rewrite_config=RewriteConfig(
                enabled_rules=["R1", "R2", "R5", "distinct_toggle"],
            ),
        )
        assert result.total_candidates >= result.n_verified + result.n_rejected, (
            f"total_candidates ({result.total_candidates}) should be >= "
            f"n_verified ({result.n_verified}) + n_rejected ({result.n_rejected})"
        )
