"""Tests for incremental combo construction (A.2)."""

import pytest
from optim.cegis.witness_synthesis import synthesize_witness
from optim.ir.types import SemType
from optim.parser.sql_to_ir import sql_to_ir
from optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from optim.verify.encode_z3 import BoundedScope


@pytest.fixture
def catalog():
    return Catalog(
        tables={
            "t1": TableInfo(name="t1", columns=[
                ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                ColumnInfo(name="val", sem_type=SemType.INT, nullable=True),
            ], primary_keys=["id"]),
            "t2": TableInfo(name="t2", columns=[
                ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                ColumnInfo(name="t1_id", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="amount", sem_type=SemType.INT, nullable=False),
            ], primary_keys=["id"]),
            "t3": TableInfo(name="t3", columns=[
                ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                ColumnInfo(name="t2_id", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="label", sem_type=SemType.STRING, nullable=True),
            ], primary_keys=["id"]),
        },
        foreign_keys=[
            ForeignKey(src_table="t2", src_column="t1_id", dst_table="t1", dst_column="id"),
            ForeignKey(src_table="t3", src_column="t2_id", dst_table="t2", dst_column="id"),
        ],
    )


@pytest.fixture
def scope():
    return BoundedScope(k_rows=2, int_bounds=(-5, 5))


def _parse_pair(q1: str, q2: str):
    ir1, e1 = sql_to_ir(q1)
    ir2, e2 = sql_to_ir(q2)
    assert ir1 is not None, f"Parse error: {e1}"
    assert ir2 is not None, f"Parse error: {e2}"
    return ir1, ir2


class TestLazyCombos:
    """Tests that lazy combo construction produces identical results."""

    def test_single_table_no_join(self, catalog, scope):
        """Single table: same result as before."""
        ir1, _ = sql_to_ir("SELECT val FROM t1")
        ir2, _ = sql_to_ir("SELECT val FROM t1 WHERE val > 0")
        result = synthesize_witness(ir1, ir2, catalog, scope)
        assert result.status == "sat"

    def test_two_table_inner_join_equivalent(self, catalog, scope):
        """2-table INNER JOIN: equivalent queries produce UNSAT."""
        q1 = "SELECT t1.val FROM t1 INNER JOIN t2 ON t1.id = t2.t1_id"
        q2 = "SELECT t1.val FROM t1 INNER JOIN t2 ON t2.t1_id = t1.id"
        result = synthesize_witness(*_parse_pair(q1, q2), catalog, scope)
        assert result.status == "unsat"

    def test_two_table_inner_join_nonequivalent(self, catalog, scope):
        """2-table INNER JOIN: non-equivalent queries produce SAT."""
        q1 = "SELECT t1.val FROM t1 INNER JOIN t2 ON t1.id = t2.t1_id"
        q2 = "SELECT t2.amount FROM t1 INNER JOIN t2 ON t1.id = t2.t1_id"
        result = synthesize_witness(*_parse_pair(q1, q2), catalog, scope)
        assert result.status == "sat"

    def test_three_table_chain_join(self, catalog, scope):
        """3-table chain JOIN: equivalent self-join produces UNSAT."""
        q = "SELECT t1.val FROM t1 INNER JOIN t2 ON t1.id = t2.t1_id INNER JOIN t3 ON t2.id = t3.t2_id"
        ir1, _ = sql_to_ir(q)
        ir2, _ = sql_to_ir(q)
        result = synthesize_witness(ir1, ir2, catalog, scope)
        assert result.status == "unsat"

    def test_left_join_preserves_unmatched(self, catalog, scope):
        """LEFT JOIN: correctly handles unmatched left rows."""
        q1 = "SELECT t1.val FROM t1 LEFT JOIN t2 ON t1.id = t2.t1_id"
        q2 = "SELECT t1.val FROM t1 INNER JOIN t2 ON t1.id = t2.t1_id"
        result = synthesize_witness(*_parse_pair(q1, q2), catalog, scope)
        assert result.status == "sat"  # LEFT vs INNER can differ

    def test_cross_join(self, catalog, scope):
        """CROSS JOIN: no ON predicate, all combos survive."""
        q = "SELECT t1.val FROM t1 CROSS JOIN t2"
        ir1, _ = sql_to_ir(q)
        ir2, _ = sql_to_ir(q)
        result = synthesize_witness(ir1, ir2, catalog, scope)
        assert result.status == "unsat"

    def test_self_join_equivalent(self, catalog, scope):
        """Self-join equivalence."""
        q1 = "SELECT a.val FROM t1 a INNER JOIN t1 b ON a.id = b.id"
        q2 = "SELECT a.val FROM t1 a INNER JOIN t1 b ON b.id = a.id"
        result = synthesize_witness(*_parse_pair(q1, q2), catalog, scope)
        assert result.status == "unsat"

    def test_aggregate_with_join(self, catalog, scope):
        """Aggregate over joined tables."""
        q1 = "SELECT COUNT(*) FROM t1 INNER JOIN t2 ON t1.id = t2.t1_id"
        q2 = "SELECT COUNT(*) FROM t1 INNER JOIN t2 ON t1.id = t2.t1_id"
        result = synthesize_witness(*_parse_pair(q1, q2), catalog, scope)
        assert result.status == "unsat"

    def test_where_after_join(self, catalog, scope):
        """WHERE applied after JOIN."""
        q1 = "SELECT t1.val FROM t1 INNER JOIN t2 ON t1.id = t2.t1_id WHERE t2.amount > 0"
        q2 = "SELECT t1.val FROM t1 INNER JOIN t2 ON t1.id = t2.t1_id WHERE t2.amount > 0"
        result = synthesize_witness(*_parse_pair(q1, q2), catalog, scope)
        assert result.status == "unsat"

    def test_nonequivalent_where_after_join(self, catalog, scope):
        """Different WHERE after same JOIN → SAT."""
        q1 = "SELECT t1.val FROM t1 INNER JOIN t2 ON t1.id = t2.t1_id WHERE t2.amount > 0"
        q2 = "SELECT t1.val FROM t1 INNER JOIN t2 ON t1.id = t2.t1_id WHERE t2.amount > 1"
        result = synthesize_witness(*_parse_pair(q1, q2), catalog, scope)
        assert result.status == "sat"
