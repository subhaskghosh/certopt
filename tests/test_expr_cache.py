"""Tests for expression cache (hash-consed Z3 memoization)."""

import pytest
from optim.cegis.expr_cache import ExprCache
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
                ColumnInfo(name="ref_id", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="amount", sem_type=SemType.INT, nullable=False),
            ], primary_keys=["id"]),
        },
        foreign_keys=[
            ForeignKey(src_table="t2", src_column="ref_id", dst_table="t1", dst_column="id"),
        ],
    )


@pytest.fixture
def scope():
    return BoundedScope(k_rows=2, int_bounds=(-5, 5))


class TestExprCache:
    """Unit tests for the ExprCache class."""

    def test_cache_hit(self):
        cache = ExprCache()
        class FakeRow: pass
        row = FakeRow()
        bkey = (("t1", id(row)),)
        cache.put_value(42, bkey, "result1")
        assert cache.get_value(42, bkey) == "result1"
        assert cache.hits == 1
        assert cache.misses == 1  # from put_value

    def test_cache_miss_different_binding(self):
        cache = ExprCache()
        class FakeRow: pass
        row1 = FakeRow()
        row2 = FakeRow()
        bkey1 = (("t1", id(row1)),)
        bkey2 = (("t1", id(row2)),)
        cache.put_value(42, bkey1, "result1")
        assert cache.get_value(42, bkey2) is None
        assert cache.hits == 0

    def test_cache_miss_different_expr(self):
        cache = ExprCache()
        class FakeRow: pass
        row = FakeRow()
        bkey = (("t1", id(row)),)
        cache.put_value(42, bkey, "result1")
        assert cache.get_value(99, bkey) is None

    def test_pred_cache(self):
        cache = ExprCache()
        bkey = (("t1", 123),)
        cache.put_pred(10, bkey, "tribool_result")
        assert cache.get_pred(10, bkey) == "tribool_result"
        assert cache.pred_hits == 1

    def test_stats(self):
        cache = ExprCache()
        bkey = (("t1", 123),)
        cache.put_value(1, bkey, "v1")
        cache.get_value(1, bkey)  # hit
        cache.get_value(2, bkey)  # miss (returns None)
        stats = cache.stats()
        assert stats["value_hits"] == 1
        assert stats["value_misses"] == 1
        assert stats["total_entries"] == 1

    def test_clear(self):
        cache = ExprCache()
        bkey = (("t1", 123),)
        cache.put_value(1, bkey, "v1")
        cache.clear()
        assert cache.get_value(1, bkey) is None
        assert cache.hits == 0
        assert cache.misses == 0

    def test_binding_key(self):
        """binding_key produces same key for same row objects."""
        cache = ExprCache()
        class FakeRow: pass
        row = FakeRow()
        binding = {"t1": row}
        key1 = cache.binding_key(binding)
        key2 = cache.binding_key(binding)
        assert key1 == key2


class TestExprCacheIntegration:
    """Integration tests: cache produces identical synthesis results."""

    def test_equivalent_pair_with_cache(self, catalog, scope):
        """Cache doesn't change UNSAT result for equivalent queries."""
        ir1, _ = sql_to_ir("SELECT val FROM t1 WHERE val > 0")
        ir2, _ = sql_to_ir("SELECT val FROM t1 WHERE val > 0")
        result = synthesize_witness(ir1, ir2, catalog, scope)
        assert result.status == "unsat"

    def test_nonequivalent_pair_with_cache(self, catalog, scope):
        """Cache doesn't change SAT result for non-equivalent queries."""
        ir1, _ = sql_to_ir("SELECT val FROM t1")
        ir2, _ = sql_to_ir("SELECT val FROM t1 WHERE val > 0")
        result = synthesize_witness(ir1, ir2, catalog, scope)
        assert result.status == "sat"
