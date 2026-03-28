"""Tests for Phase 7 scalability: preprocessing + adaptive combo limits.

Tests cover:
  - Predicate-to-ON promotion from implicit join syntax
  - Redundant table elimination via FK→PK joins
  - Adaptive combo limits based on query shape
  - Integration with witness synthesis on multi-table queries
"""

from __future__ import annotations

import pytest

from optim.cegis.preprocessing import (
    eliminate_redundant_tables,
    preprocess_for_synthesis,
    promote_predicates_to_on,
)
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
from optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from optim.verify.encode_z3 import BoundedScope


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def multi_table_catalog() -> Catalog:
    """A 5-table schema with FK chains: A → B → C, A → D, A → E.

    All FK columns are NOT NULL to enable safe elimination.
    """
    return Catalog(
        tables={
            "a": TableInfo(
                name="a",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="b_id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="d_id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="e_id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="val", sem_type=SemType.INT, nullable=True),
                ],
                primary_keys=["id"],
            ),
            "b": TableInfo(
                name="b",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="c_id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="name", sem_type=SemType.STRING, nullable=True),
                ],
                primary_keys=["id"],
            ),
            "c": TableInfo(
                name="c",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="label", sem_type=SemType.STRING, nullable=True),
                ],
                primary_keys=["id"],
            ),
            "d": TableInfo(
                name="d",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="code", sem_type=SemType.STRING, nullable=True),
                ],
                primary_keys=["id"],
            ),
            "e": TableInfo(
                name="e",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="tag", sem_type=SemType.STRING, nullable=True),
                ],
                primary_keys=["id"],
            ),
        },
        foreign_keys=[
            ForeignKey(src_table="a", src_column="b_id", dst_table="b", dst_column="id"),
            ForeignKey(src_table="b", src_column="c_id", dst_table="c", dst_column="id"),
            ForeignKey(src_table="a", src_column="d_id", dst_table="d", dst_column="id"),
            ForeignKey(src_table="a", src_column="e_id", dst_table="e", dst_column="id"),
        ],
    )


def _cross_join(table: str, alias: str | None = None) -> JoinClause:
    """Helper: create a CROSS JOIN with ON TRUE."""
    return JoinClause(
        join_type=JoinType.CROSS,
        right=RelRef(table=table, alias=alias),
        on=Literal(value=True),
    )


def _inner_join(table: str, on: BinOp, alias: str | None = None) -> JoinClause:
    """Helper: create an INNER JOIN."""
    return JoinClause(
        join_type=JoinType.INNER,
        right=RelRef(table=table, alias=alias),
        on=on,
    )


def _eq(t1: str, c1: str, t2: str, c2: str) -> BinOp:
    """Helper: create t1.c1 = t2.c2."""
    return BinOp(
        op=BinOpKind.EQ,
        left=ColumnRef(table=t1, column=c1, sem_type=SemType.INT),
        right=ColumnRef(table=t2, column=c2, sem_type=SemType.INT),
    )


def _and(*exprs) -> BinOp:
    """Helper: chain expressions with AND."""
    result = exprs[0]
    for e in exprs[1:]:
        result = BinOp(op=BinOpKind.AND, left=result, right=e)
    return result


# ---------------------------------------------------------------------------
# Test: Predicate-to-ON promotion
# ---------------------------------------------------------------------------

class TestPredicatePromotion:
    """Tests for promote_predicates_to_on()."""

    def test_promote_single_cross_join(self):
        """Single CROSS JOIN with equi-join in WHERE → INNER JOIN with ON."""
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_cross_join("b")],
            where=_eq("a", "b_id", "b", "id"),
        )
        result, n = promote_predicates_to_on(ir)
        assert n == 1
        assert result.joins[0].join_type == JoinType.INNER
        assert result.where is None

    def test_promote_multiple_cross_joins(self):
        """Multiple CROSS JOINs with equi-join predicates in WHERE."""
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_cross_join("b"), _cross_join("d")],
            where=_and(
                _eq("a", "b_id", "b", "id"),
                _eq("a", "d_id", "d", "id"),
            ),
        )
        result, n = promote_predicates_to_on(ir)
        assert n == 2
        assert result.joins[0].join_type == JoinType.INNER
        assert result.joins[1].join_type == JoinType.INNER
        assert result.where is None

    def test_preserve_non_equi_where(self):
        """Non-equi-join WHERE predicates are preserved."""
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_cross_join("b")],
            where=_and(
                _eq("a", "b_id", "b", "id"),
                BinOp(
                    op=BinOpKind.GT,
                    left=ColumnRef(table="a", column="val", sem_type=SemType.INT),
                    right=Literal(value=10),
                ),
            ),
        )
        result, n = promote_predicates_to_on(ir)
        assert n == 1
        assert result.where is not None  # val > 10 remains

    def test_no_promotion_without_joins(self):
        """No joins → no promotion."""
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            where=BinOp(
                op=BinOpKind.GT,
                left=ColumnRef(table="a", column="val", sem_type=SemType.INT),
                right=Literal(value=10),
            ),
        )
        result, n = promote_predicates_to_on(ir)
        assert n == 0

    def test_no_promotion_for_left_join(self):
        """LEFT JOINs are not eligible for promotion."""
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[
                JoinClause(
                    join_type=JoinType.LEFT,
                    right=RelRef(table="b"),
                    on=Literal(value=True),
                ),
            ],
            where=_eq("a", "b_id", "b", "id"),
        )
        result, n = promote_predicates_to_on(ir)
        assert n == 0

    def test_promote_symmetric_reference(self):
        """Predicate with tables in reversed order still promotes."""
        # WHERE b.id = a.b_id (right table listed first)
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_cross_join("b")],
            where=_eq("b", "id", "a", "b_id"),
        )
        result, n = promote_predicates_to_on(ir)
        assert n == 1
        assert result.joins[0].join_type == JoinType.INNER

    def test_promote_chain_joins(self):
        """Three-table chain: A × B × C with WHERE conditions."""
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_cross_join("b"), _cross_join("c")],
            where=_and(
                _eq("a", "b_id", "b", "id"),
                _eq("b", "c_id", "c", "id"),
            ),
        )
        result, n = promote_predicates_to_on(ir)
        assert n == 2
        assert result.joins[0].join_type == JoinType.INNER
        assert result.joins[1].join_type == JoinType.INNER


# ---------------------------------------------------------------------------
# Test: Redundant table elimination
# ---------------------------------------------------------------------------

class TestTableElimination:
    """Tests for eliminate_redundant_tables()."""

    def test_eliminate_unused_fk_pk_join(self, multi_table_catalog):
        """Table joined via FK→PK but not referenced in output → eliminated."""
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[
                _inner_join("b", _eq("a", "b_id", "b", "id")),
            ],
        )
        result, eliminated = eliminate_redundant_tables(ir, multi_table_catalog)
        assert "b" in eliminated
        assert len(result.joins) == 0

    def test_no_eliminate_referenced_table(self, multi_table_catalog):
        """Table referenced in SELECT → not eliminated."""
        ir = QueryIR(
            select=[
                ColumnRef(table="a", column="val", sem_type=SemType.INT),
                ColumnRef(table="b", column="name", sem_type=SemType.STRING),
            ],
            from_table=RelRef(table="a"),
            joins=[
                _inner_join("b", _eq("a", "b_id", "b", "id")),
            ],
        )
        result, eliminated = eliminate_redundant_tables(ir, multi_table_catalog)
        assert len(eliminated) == 0
        assert len(result.joins) == 1

    def test_eliminate_multiple_tables(self, multi_table_catalog):
        """Multiple unused FK→PK joins can be eliminated iteratively."""
        # SELECT a.val FROM a JOIN b ON a.b_id=b.id JOIN d ON a.d_id=d.id
        # Both b and d are unused in output
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[
                _inner_join("b", _eq("a", "b_id", "b", "id")),
                _inner_join("d", _eq("a", "d_id", "d", "id")),
            ],
        )
        result, eliminated = eliminate_redundant_tables(ir, multi_table_catalog)
        assert len(eliminated) == 2
        assert set(eliminated) == {"b", "d"}
        assert len(result.joins) == 0

    def test_no_eliminate_nullable_fk(self):
        """FK column is nullable → not safe to eliminate (join filters NULLs)."""
        catalog = Catalog(
            tables={
                "a": TableInfo(
                    name="a",
                    columns=[
                        ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                        ColumnInfo(name="b_id", sem_type=SemType.INT, nullable=True),  # nullable!
                        ColumnInfo(name="val", sem_type=SemType.INT, nullable=True),
                    ],
                    primary_keys=["id"],
                ),
                "b": TableInfo(
                    name="b",
                    columns=[
                        ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ],
                    primary_keys=["id"],
                ),
            },
            foreign_keys=[
                ForeignKey(src_table="a", src_column="b_id", dst_table="b", dst_column="id"),
            ],
        )
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_inner_join("b", _eq("a", "b_id", "b", "id"))],
        )
        result, eliminated = eliminate_redundant_tables(ir, catalog)
        assert len(eliminated) == 0

    def test_no_eliminate_left_join(self, multi_table_catalog):
        """LEFT JOINs are not eligible for elimination."""
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[
                JoinClause(
                    join_type=JoinType.LEFT,
                    right=RelRef(table="b"),
                    on=_eq("a", "b_id", "b", "id"),
                ),
            ],
        )
        result, eliminated = eliminate_redundant_tables(ir, multi_table_catalog)
        assert len(eliminated) == 0

    def test_no_eliminate_if_referenced_in_where(self, multi_table_catalog):
        """Table referenced in another join's ON survives if chain prevents full elimination."""
        # SELECT a.val, c.label FROM a JOIN b ON a.b_id=b.id JOIN c ON b.c_id=c.id
        # c is referenced in SELECT → can't eliminate c → b is referenced in c's ON → can't eliminate b
        ir = QueryIR(
            select=[
                ColumnRef(table="a", column="val", sem_type=SemType.INT),
                ColumnRef(table="c", column="label", sem_type=SemType.STRING),
            ],
            from_table=RelRef(table="a"),
            joins=[
                _inner_join("b", _eq("a", "b_id", "b", "id")),
                _inner_join("c", _eq("b", "c_id", "c", "id")),
            ],
        )
        result, eliminated = eliminate_redundant_tables(ir, multi_table_catalog)
        # c is in SELECT → can't eliminate; b is in c's ON → can't eliminate
        assert "b" not in eliminated
        assert "c" not in eliminated
        assert len(result.joins) == 2

    def test_no_eliminate_without_fk(self):
        """Join without FK relationship → not eliminated."""
        catalog = Catalog(
            tables={
                "a": TableInfo(
                    name="a",
                    columns=[
                        ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                        ColumnInfo(name="x", sem_type=SemType.INT, nullable=False),
                    ],
                    primary_keys=["id"],
                ),
                "b": TableInfo(
                    name="b",
                    columns=[
                        ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                        ColumnInfo(name="x", sem_type=SemType.INT, nullable=False),
                    ],
                    primary_keys=["id"],
                ),
            },
            foreign_keys=[],  # No FK!
        )
        ir = QueryIR(
            select=[ColumnRef(table="a", column="id", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_inner_join("b", _eq("a", "x", "b", "x"))],
        )
        result, eliminated = eliminate_redundant_tables(ir, catalog)
        assert len(eliminated) == 0

    def test_iterative_elimination(self, multi_table_catalog):
        """Eliminating one table enables elimination of another (chain)."""
        # SELECT a.val FROM a JOIN b ON a.b_id=b.id JOIN c ON b.c_id=c.id
        # c is unused → eliminate c → now b is also unused → eliminate b
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[
                _inner_join("b", _eq("a", "b_id", "b", "id")),
                _inner_join("c", _eq("b", "c_id", "c", "id")),
            ],
        )
        result, eliminated = eliminate_redundant_tables(ir, multi_table_catalog)
        # c eliminated first (no outgoing deps), then b becomes eliminable
        assert set(eliminated) == {"c", "b"}
        assert len(result.joins) == 0

    def test_table_in_where_not_eliminated(self, multi_table_catalog):
        """Table referenced in WHERE predicate → not eliminated."""
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_inner_join("b", _eq("a", "b_id", "b", "id"))],
            where=BinOp(
                op=BinOpKind.EQ,
                left=ColumnRef(table="b", column="name", sem_type=SemType.STRING),
                right=Literal(value="test"),
            ),
        )
        result, eliminated = eliminate_redundant_tables(ir, multi_table_catalog)
        assert len(eliminated) == 0


# ---------------------------------------------------------------------------
# Test: Combined preprocessing
# ---------------------------------------------------------------------------

class TestPreprocessForSynthesis:
    """Tests for the combined preprocess_for_synthesis() entry point."""

    def test_combined_promotion_and_elimination(self, multi_table_catalog):
        """Promotion + elimination reduces table count."""
        # Implicit join: SELECT a.val FROM a, b, d WHERE a.b_id=b.id AND a.d_id=d.id
        # Step 1: promote to INNER JOINs
        # Step 2: b and d are unused → eliminate
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_cross_join("b"), _cross_join("d")],
            where=_and(
                _eq("a", "b_id", "b", "id"),
                _eq("a", "d_id", "d", "id"),
            ),
        )
        result, stats = preprocess_for_synthesis(ir, multi_table_catalog)
        assert stats["tables_before"] == 3
        assert stats["tables_after"] == 1
        assert stats["promoted"] == 2
        assert len(stats["eliminated"]) == 2

    def test_stats_no_change(self, multi_table_catalog):
        """Query with no implicit joins and all tables referenced → no change."""
        ir = QueryIR(
            select=[
                ColumnRef(table="a", column="val", sem_type=SemType.INT),
                ColumnRef(table="b", column="name", sem_type=SemType.STRING),
            ],
            from_table=RelRef(table="a"),
            joins=[_inner_join("b", _eq("a", "b_id", "b", "id"))],
        )
        result, stats = preprocess_for_synthesis(ir, multi_table_catalog)
        assert stats["tables_before"] == 2
        assert stats["tables_after"] == 2
        assert stats["promoted"] == 0
        assert len(stats["eliminated"]) == 0


# ---------------------------------------------------------------------------
# Test: Adaptive combo limits
# ---------------------------------------------------------------------------

class TestAdaptiveComboLimits:
    """Tests verifying that adaptive combo limits allow synthesis on
    preprocessed multi-table queries."""

    def test_5_table_with_elimination_completes(self, multi_table_catalog):
        """5-table query reduces to 1 table after preprocessing → synthesis runs."""
        # SELECT a.val FROM a, b, c, d, e
        # WHERE a.b_id=b.id AND b.c_id=c.id AND a.d_id=d.id AND a.e_id=e.id
        # All joined tables are unused → all eliminated → 1 table remains
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[
                _cross_join("b"),
                _cross_join("c"),
                _cross_join("d"),
                _cross_join("e"),
            ],
            where=_and(
                _eq("a", "b_id", "b", "id"),
                _eq("b", "c_id", "c", "id"),
                _eq("a", "d_id", "d", "id"),
                _eq("a", "e_id", "e", "id"),
            ),
        )
        scope = BoundedScope(k_rows=2)
        # Should not return "unknown" since preprocessing eliminates tables
        result = synthesize_witness(ir, ir, multi_table_catalog, scope=scope)
        assert result.status in ("unsat", "sat")  # not "unknown"

    def test_self_equivalence_after_preprocessing(self, multi_table_catalog):
        """Self-equivalence check succeeds after preprocessing."""
        ir = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_cross_join("b"), _cross_join("d")],
            where=_and(
                _eq("a", "b_id", "b", "id"),
                _eq("a", "d_id", "d", "id"),
            ),
        )
        scope = BoundedScope(k_rows=2)
        result = synthesize_witness(ir, ir, multi_table_catalog, scope=scope)
        assert result.status == "unsat"  # identical queries → equivalent

    def test_non_eliminable_tables_use_extended_limit(self, multi_table_catalog):
        """Preprocessed query with non-eliminable tables uses extended limit."""
        # SELECT a.val, b.name FROM a, b WHERE a.b_id=b.id
        # b is referenced in SELECT → not eliminated, but predicate is promoted
        # 2 tables × k=2 → 4 combos → well within any limit
        ir = QueryIR(
            select=[
                ColumnRef(table="a", column="val", sem_type=SemType.INT),
                ColumnRef(table="b", column="name", sem_type=SemType.STRING),
            ],
            from_table=RelRef(table="a"),
            joins=[_cross_join("b")],
            where=_eq("a", "b_id", "b", "id"),
        )
        scope = BoundedScope(k_rows=2)
        result = synthesize_witness(ir, ir, multi_table_catalog, scope=scope)
        assert result.status == "unsat"


# ---------------------------------------------------------------------------
# Test: Witness synthesis integration
# ---------------------------------------------------------------------------

class TestSynthesisIntegration:
    """Integration tests: preprocessing enables synthesis on queries
    that would previously be skipped."""

    def test_detect_difference_after_elimination(self, multi_table_catalog):
        """Preprocessing enables detecting semantic difference."""
        # Q1: SELECT a.val FROM a, b WHERE a.b_id=b.id
        # Q2: SELECT a.val FROM a, b WHERE a.b_id=b.id AND a.val > 0
        # After preprocessing: both eliminate b, but Q2 has extra WHERE
        q1 = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_cross_join("b")],
            where=_eq("a", "b_id", "b", "id"),
        )
        q2 = QueryIR(
            select=[ColumnRef(table="a", column="val", sem_type=SemType.INT)],
            from_table=RelRef(table="a"),
            joins=[_cross_join("b")],
            where=_and(
                _eq("a", "b_id", "b", "id"),
                BinOp(
                    op=BinOpKind.GT,
                    left=ColumnRef(table="a", column="val", sem_type=SemType.INT),
                    right=Literal(value=0),
                ),
            ),
        )
        scope = BoundedScope(k_rows=2)
        result = synthesize_witness(q1, q2, multi_table_catalog, scope=scope)
        assert result.status == "sat"
        assert result.witness_db is not None
