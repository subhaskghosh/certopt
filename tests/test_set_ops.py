"""Tests for chained set operations and set-op arity verification."""

from optim.parser.sql_to_ir import sql_to_ir
from optim.ir.render_sql import render


class TestChainedSetOps:
    """Regression tests for chained set operations (3+ branches)."""

    def test_triple_union_all_roundtrip(self):
        """A UNION ALL B UNION ALL C must preserve all three branches."""
        sql = "SELECT 1 AS x FROM t UNION ALL SELECT 2 AS x FROM t UNION ALL SELECT 3 AS x FROM t"
        ir, err = sql_to_ir(sql)
        assert ir is not None, f"Parse failed: {err}"
        rendered = render(ir, dialect="sqlite")
        # Must contain all three literals
        assert "1" in rendered
        assert "2" in rendered
        assert "3" in rendered
        # Must have two UNION ALL
        assert rendered.upper().count("UNION ALL") == 2

    def test_triple_union_chain_depth(self):
        """Chain of 3 should produce depth-2 set_right chain."""
        sql = "SELECT 1 AS x FROM t UNION ALL SELECT 2 AS x FROM t UNION ALL SELECT 3 AS x FROM t"
        ir, err = sql_to_ir(sql)
        assert ir is not None
        assert ir.set_op is not None
        assert ir.set_right is not None
        assert ir.set_right.set_op is not None
        assert ir.set_right.set_right is not None
        assert ir.set_right.set_right.set_op is None  # leaf

    def test_mixed_set_ops_chain(self):
        """A UNION ALL B INTERSECT C preserves both ops."""
        sql = "SELECT x FROM t UNION ALL SELECT x FROM t INTERSECT SELECT x FROM t"
        ir, err = sql_to_ir(sql)
        assert ir is not None, f"Parse failed: {err}"
        rendered = render(ir, dialect="sqlite")
        assert "UNION ALL" in rendered.upper()
        assert "INTERSECT" in rendered.upper()

    def test_four_way_union_all(self):
        """4-way chain preserves all branches."""
        sql = "SELECT 1 AS x FROM t UNION ALL SELECT 2 AS x FROM t UNION ALL SELECT 3 AS x FROM t UNION ALL SELECT 4 AS x FROM t"
        ir, err = sql_to_ir(sql)
        assert ir is not None, f"Parse failed: {err}"
        rendered = render(ir, dialect="sqlite")
        for v in ["1", "2", "3", "4"]:
            assert v in rendered
        assert rendered.upper().count("UNION ALL") == 3


class TestSetOpVerifierArity:
    """Regression tests for set-op arity checking in verifier."""

    def test_mismatched_arity_rejected(self):
        """SELECT x UNION ALL SELECT x, y must fail verification."""
        from optim.verify.constraints import structural_verify
        from optim.schema.catalog import Catalog, TableInfo, ColumnInfo
        from optim.ir.types import SemType

        cat = Catalog(tables={
            't': TableInfo(name='t', columns=[
                ColumnInfo(name='x', sem_type=SemType.INT),
                ColumnInfo(name='y', sem_type=SemType.INT),
            ], primary_keys=['x']),
        }, foreign_keys=[])

        sql = "SELECT x FROM t UNION ALL SELECT x, y FROM t"
        ir, err = sql_to_ir(sql)
        assert ir is not None
        result = structural_verify(ir, cat)
        assert not result.certified, "Mismatched arity should fail verification"

    def test_matching_arity_accepted(self):
        """SELECT x UNION ALL SELECT y must pass verification."""
        from optim.verify.constraints import structural_verify
        from optim.schema.catalog import Catalog, TableInfo, ColumnInfo
        from optim.ir.types import SemType

        cat = Catalog(tables={
            't': TableInfo(name='t', columns=[
                ColumnInfo(name='x', sem_type=SemType.INT),
                ColumnInfo(name='y', sem_type=SemType.INT),
            ], primary_keys=['x']),
        }, foreign_keys=[])

        sql = "SELECT x FROM t UNION ALL SELECT y FROM t"
        ir, err = sql_to_ir(sql)
        assert ir is not None
        result = structural_verify(ir, cat)
        assert result.certified, "Matching arity should pass verification"

    def test_chained_arity_check(self):
        """Arity check must work at all depths of chained set ops."""
        from optim.verify.constraints import structural_verify
        from optim.schema.catalog import Catalog, TableInfo, ColumnInfo
        from optim.ir.types import SemType

        cat = Catalog(tables={
            't': TableInfo(name='t', columns=[
                ColumnInfo(name='x', sem_type=SemType.INT),
                ColumnInfo(name='y', sem_type=SemType.INT),
            ], primary_keys=['x']),
        }, foreign_keys=[])

        # First two branches have arity 1, third has arity 2 — should fail
        sql = "SELECT x FROM t UNION ALL SELECT x FROM t UNION ALL SELECT x, y FROM t"
        ir, err = sql_to_ir(sql)
        assert ir is not None
        result = structural_verify(ir, cat)
        assert not result.certified, "Arity mismatch in chained set-op should fail"
