"""Tests for extended SQL support: EXISTS, IN subquery, scalar subquery,
set operations, and window functions.

Covers:
  - Parser: parse → IR for each construct
  - Renderer: IR → SQL roundtrip
  - Witness synthesis: exact encoding (SAT/UNSAT correctness)
  - Window functions: parse/render/conservative synthesis
"""

import pytest

from optim.cegis.witness_export import validate_witness
from optim.cegis.witness_synthesis import synthesize_witness
from optim.ir.render_sql import render
from optim.ir.types import (
    AggCall,
    AggFunc,
    BinOp,
    BinOpKind,
    ColumnRef,
    ExistsSubquery,
    InSubquery,
    JoinClause,
    JoinType,
    Literal,
    QueryIR,
    RelRef,
    ScalarSubquery,
    SemType,
    WindowFunc,
)
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
                ColumnInfo(name="cat", sem_type=SemType.STRING, nullable=True),
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
    return BoundedScope(k_rows=2, int_bounds=(-2, 5))


def _synth(sql1, sql2, catalog, scope):
    """Parse two SQL strings and run witness synthesis."""
    ir1, e1 = sql_to_ir(sql1)
    ir2, e2 = sql_to_ir(sql2)
    assert ir1 is not None, f"Parse error for Q1: {e1}"
    assert ir2 is not None, f"Parse error for Q2: {e2}"
    return synthesize_witness(ir1, ir2, catalog, scope)


# ===================================================================
# EXISTS / NOT EXISTS
# ===================================================================

class TestExists:

    def test_parse_exists(self):
        ir, err = sql_to_ir("SELECT id FROM t1 WHERE EXISTS (SELECT 1 FROM t2 WHERE t2.ref_id = t1.id)")
        assert ir is not None, err
        assert ir.where is not None

    def test_parse_not_exists(self):
        ir, err = sql_to_ir("SELECT id FROM t1 WHERE NOT EXISTS (SELECT 1 FROM t2)")
        assert ir is not None, err

    def test_exists_renders(self):
        ir, _ = sql_to_ir("SELECT id FROM t1 WHERE EXISTS (SELECT 1 FROM t2 WHERE t2.ref_id = t1.id)")
        sql = render(ir, dialect="sqlite")
        assert "EXISTS" in sql

    def test_uncorrelated_exists_equivalent(self, catalog, scope):
        """EXISTS (SELECT 1 FROM t2) is TRUE iff t2 has rows — same for both queries."""
        r = _synth(
            "SELECT id FROM t1 WHERE EXISTS (SELECT 1 FROM t2)",
            "SELECT id FROM t1 WHERE EXISTS (SELECT 1 FROM t2)",
            catalog, scope,
        )
        assert r.status == "unsat"

    def test_exists_vs_no_exists_sat(self, catalog, scope):
        """With vs without EXISTS filter should differ."""
        r = _synth(
            "SELECT id FROM t1 WHERE EXISTS (SELECT 1 FROM t2 WHERE t2.ref_id = t1.id)",
            "SELECT id FROM t1",
            catalog, scope,
        )
        assert r.status == "sat"

    def test_not_exists_vs_exists_sat(self, catalog, scope):
        """NOT EXISTS vs EXISTS on same subquery should differ."""
        r = _synth(
            "SELECT id FROM t1 WHERE EXISTS (SELECT 1 FROM t2 WHERE t2.ref_id = t1.id)",
            "SELECT id FROM t1 WHERE NOT EXISTS (SELECT 1 FROM t2 WHERE t2.ref_id = t1.id)",
            catalog, scope,
        )
        assert r.status == "sat"


# ===================================================================
# IN (subquery)
# ===================================================================

class TestInSubquery:

    def test_parse_in_subquery(self):
        ir, err = sql_to_ir("SELECT id FROM t1 WHERE id IN (SELECT ref_id FROM t2)")
        assert ir is not None, err

    def test_in_subquery_renders(self):
        ir, _ = sql_to_ir("SELECT id FROM t1 WHERE id IN (SELECT ref_id FROM t2)")
        sql = render(ir, dialect="sqlite")
        assert "IN" in sql

    def test_in_subquery_vs_no_filter_sat(self, catalog, scope):
        """IN subquery vs no filter should differ."""
        r = _synth(
            "SELECT id FROM t1 WHERE id IN (SELECT ref_id FROM t2)",
            "SELECT id FROM t1",
            catalog, scope,
        )
        assert r.status == "sat"

    def test_in_subquery_identical_unsat(self, catalog, scope):
        """Same IN subquery should be equivalent."""
        r = _synth(
            "SELECT id FROM t1 WHERE id IN (SELECT ref_id FROM t2)",
            "SELECT id FROM t1 WHERE id IN (SELECT ref_id FROM t2)",
            catalog, scope,
        )
        assert r.status == "unsat"

    def test_in_subquery_witness_validates(self, catalog, scope):
        """IN subquery vs no filter: witness should validate in SQLite."""
        ir1, _ = sql_to_ir("SELECT id FROM t1 WHERE id IN (SELECT ref_id FROM t2)")
        ir2, _ = sql_to_ir("SELECT id FROM t1")
        r = synthesize_witness(ir1, ir2, catalog, scope)
        assert r.status == "sat"
        if r.witness_db:
            val = validate_witness(ir1, ir2, r.witness_db, catalog)
            assert val.results_differ

    def test_in_subquery_same_table_groupby_unsat(self):
        """FIX.26a: IN subquery referencing the same table with GROUP BY HAVING.

        The inner subquery has GROUP BY + HAVING (aggregation in HAVING,
        not in SELECT), so _encode_inner_query must use the aggregation
        path.  Previously, has_aggregation() returned False because no
        aggregate appeared in SELECT, causing GROUP BY/HAVING to be ignored.
        """
        person_catalog = Catalog(tables={
            "person": TableInfo(
                name="person",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="email", sem_type=SemType.STRING, nullable=True),
                ],
                primary_keys=["id"],
            ),
        })
        scope = BoundedScope(k_rows=2, int_bounds=(0, 10), solver_timeout_ms=10000)
        ir1, e1 = sql_to_ir(
            "SELECT EMAIL FROM PERSON GROUP BY EMAIL HAVING COUNT(EMAIL) > 1"
        )
        ir2, e2 = sql_to_ir(
            "SELECT DISTINCT EMAIL FROM PERSON WHERE EMAIL IN "
            "(SELECT EMAIL FROM PERSON GROUP BY EMAIL HAVING COUNT(*) > 1)"
        )
        assert ir1 is not None, e1
        assert ir2 is not None, e2
        r = synthesize_witness(ir1, ir2, person_catalog, scope)
        assert r.status == "unsat", (
            f"Expected UNSAT (equivalent) but got {r.status}; "
            f"witness={r.witness_db}"
        )

    def test_correlated_scalar_subquery_same_table_unsat(self):
        """FIX.26b: Correlated scalar subquery with same-table self-reference.

        Inner subquery uses alias P, outer uses PERSON (no alias).
        The correlated ref `P.EMAIL = PERSON.EMAIL` must correctly resolve
        P.EMAIL to inner row and PERSON.EMAIL to outer row.
        """
        person_catalog = Catalog(tables={
            "person": TableInfo(
                name="person",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="email", sem_type=SemType.STRING, nullable=True),
                ],
                primary_keys=["id"],
            ),
        })
        scope = BoundedScope(k_rows=2, int_bounds=(0, 10), solver_timeout_ms=10000)
        ir1, e1 = sql_to_ir(
            "SELECT EMAIL FROM PERSON GROUP BY EMAIL HAVING COUNT(EMAIL) > 1"
        )
        ir2, e2 = sql_to_ir(
            "SELECT DISTINCT EMAIL FROM PERSON "
            "WHERE (SELECT COUNT(EMAIL) FROM PERSON P "
            "WHERE P.EMAIL = PERSON.EMAIL) > 1"
        )
        assert ir1 is not None, e1
        assert ir2 is not None, e2
        r = synthesize_witness(ir1, ir2, person_catalog, scope)
        assert r.status == "unsat", (
            f"Expected UNSAT (equivalent) but got {r.status}; "
            f"witness={r.witness_db}"
        )


# ===================================================================
# Scalar subquery
# ===================================================================

class TestScalarSubquery:

    def test_parse_scalar_subquery(self):
        ir, err = sql_to_ir("SELECT id, (SELECT MAX(amount) FROM t2 WHERE t2.ref_id = t1.id) FROM t1")
        assert ir is not None, err

    def test_scalar_subquery_renders(self):
        ir, _ = sql_to_ir("SELECT id, (SELECT MAX(amount) FROM t2) FROM t1")
        if ir:
            sql = render(ir, dialect="sqlite")
            assert "SELECT" in sql


# ===================================================================
# Set operations
# ===================================================================

class TestSetOperations:

    def test_parse_union_all(self):
        ir, err = sql_to_ir("SELECT id FROM t1 UNION ALL SELECT id FROM t2")
        assert ir is not None, err
        assert ir.set_op is not None

    def test_parse_union(self):
        ir, err = sql_to_ir("SELECT id FROM t1 UNION SELECT id FROM t2")
        assert ir is not None, err

    def test_parse_intersect(self):
        ir, err = sql_to_ir("SELECT id FROM t1 INTERSECT SELECT id FROM t2")
        assert ir is not None, err

    def test_parse_except(self):
        ir, err = sql_to_ir("SELECT id FROM t1 EXCEPT SELECT id FROM t2")
        assert ir is not None, err

    def test_union_all_vs_union_sat(self, catalog, scope):
        """UNION ALL preserves duplicates, UNION doesn't — should differ."""
        r = _synth(
            "SELECT id FROM t1 UNION ALL SELECT id FROM t2",
            "SELECT id FROM t1 UNION SELECT id FROM t2",
            catalog, scope,
        )
        assert r.status == "sat"

    def test_union_all_identical_unsat(self, catalog, scope):
        """Same UNION ALL should be equivalent."""
        r = _synth(
            "SELECT id FROM t1 UNION ALL SELECT id FROM t2",
            "SELECT id FROM t1 UNION ALL SELECT id FROM t2",
            catalog, scope,
        )
        assert r.status == "unsat"

    def test_union_identical_unsat(self, catalog, scope):
        """Same UNION should be equivalent."""
        r = _synth(
            "SELECT id FROM t1 UNION SELECT id FROM t2",
            "SELECT id FROM t1 UNION SELECT id FROM t2",
            catalog, scope,
        )
        assert r.status == "unsat"

    def test_intersect_vs_union_sat(self, catalog, scope):
        """INTERSECT vs UNION should differ."""
        r = _synth(
            "SELECT id FROM t1 INTERSECT SELECT id FROM t2",
            "SELECT id FROM t1 UNION SELECT id FROM t2",
            catalog, scope,
        )
        assert r.status == "sat"

    def test_except_vs_original_sat(self, catalog, scope):
        """EXCEPT removes matching rows — should differ from original."""
        r = _synth(
            "SELECT id FROM t1 EXCEPT SELECT id FROM t2",
            "SELECT id FROM t1",
            catalog, scope,
        )
        assert r.status == "sat"

    def test_union_all_witness_validates(self, catalog, scope):
        """UNION ALL vs UNION witness should validate in SQLite."""
        ir1, _ = sql_to_ir("SELECT id FROM t1 UNION ALL SELECT id FROM t2")
        ir2, _ = sql_to_ir("SELECT id FROM t1 UNION SELECT id FROM t2")
        r = synthesize_witness(ir1, ir2, catalog, scope)
        assert r.status == "sat"
        if r.witness_db:
            val = validate_witness(ir1, ir2, r.witness_db, catalog)
            assert val.results_differ

    def test_intersect_identical_unsat(self, catalog, scope):
        """Same INTERSECT should be equivalent."""
        r = _synth(
            "SELECT id FROM t1 INTERSECT SELECT id FROM t2",
            "SELECT id FROM t1 INTERSECT SELECT id FROM t2",
            catalog, scope,
        )
        assert r.status == "unsat"

    def test_except_identical_unsat(self, catalog, scope):
        """Same EXCEPT should be equivalent."""
        r = _synth(
            "SELECT id FROM t1 EXCEPT SELECT id FROM t2",
            "SELECT id FROM t1 EXCEPT SELECT id FROM t2",
            catalog, scope,
        )
        assert r.status == "unsat"


# ===================================================================
# Window functions
# ===================================================================

class TestWindowFunctions:

    def test_parse_row_number(self):
        ir, err = sql_to_ir("SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM t1")
        assert ir is not None, err
        # Should have a WindowFunc in select
        has_window = any(isinstance(s, WindowFunc) for s in ir.select)
        assert has_window

    def test_parse_rank_with_partition(self):
        ir, err = sql_to_ir("SELECT id, RANK() OVER (PARTITION BY cat ORDER BY val DESC) FROM t1")
        assert ir is not None, err

    def test_parse_sum_window(self):
        ir, err = sql_to_ir("SELECT id, SUM(val) OVER (PARTITION BY cat) FROM t1")
        assert ir is not None, err

    def test_window_renders(self):
        ir, _ = sql_to_ir("SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM t1")
        if ir:
            sql = render(ir, dialect="sqlite")
            assert "OVER" in sql

    def test_window_roundtrip(self):
        original = "SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM t1"
        ir, _ = sql_to_ir(original)
        if ir:
            sql = render(ir, dialect="sqlite")
            ir2, _ = sql_to_ir(sql)
            assert ir2 is not None

    def test_window_synthesis_conservative(self, catalog, scope):
        """Window function queries should not crash witness synthesis.
        Result should be unsat (same query) or unknown/sat (conservative)."""
        ir1, _ = sql_to_ir("SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM t1")
        ir2, _ = sql_to_ir("SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM t1")
        if ir1 and ir2:
            r = synthesize_witness(ir1, ir2, catalog, scope)
            # Should not crash; result is acceptable as unsat or unknown
            assert r.status in ("unsat", "sat", "unknown")


# ===================================================================
# Window functions — exact encoding tests
# ===================================================================

class TestWindowFunctionExactEncoding:
    """Tests for exact window function encoding in witness synthesis.

    These verify the Z3 encoding produces correct SAT/UNSAT results
    by comparing semantically equivalent and different window queries.
    """

    # --- ROW_NUMBER ---

    def test_row_number_self_equiv(self, catalog, scope):
        """ROW_NUMBER() OVER same spec → UNSAT (self-equivalence)."""
        r = _synth(
            "SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM t1",
            "SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM t1",
            catalog, scope,
        )
        assert r.status == "unsat"

    def test_row_number_different_order(self, catalog, scope):
        """ROW_NUMBER() with ASC vs DESC ordering → SAT."""
        r = _synth(
            "SELECT id, ROW_NUMBER() OVER (ORDER BY val ASC) FROM t1",
            "SELECT id, ROW_NUMBER() OVER (ORDER BY val DESC) FROM t1",
            catalog, scope,
        )
        assert r.status == "sat"

    def test_row_number_with_partition_self_equiv(self, catalog, scope):
        """ROW_NUMBER() OVER (PARTITION BY cat ORDER BY id) → UNSAT."""
        r = _synth(
            "SELECT id, ROW_NUMBER() OVER (PARTITION BY cat ORDER BY id) FROM t1",
            "SELECT id, ROW_NUMBER() OVER (PARTITION BY cat ORDER BY id) FROM t1",
            catalog, scope,
        )
        assert r.status == "unsat"

    # --- RANK ---

    def test_rank_self_equiv(self, catalog, scope):
        """RANK() OVER same spec → UNSAT."""
        r = _synth(
            "SELECT id, RANK() OVER (ORDER BY val) FROM t1",
            "SELECT id, RANK() OVER (ORDER BY val) FROM t1",
            catalog, scope,
        )
        assert r.status == "unsat"

    def test_rank_vs_row_number(self, catalog, scope):
        """RANK vs ROW_NUMBER can differ when there are ties → SAT."""
        r = _synth(
            "SELECT id, RANK() OVER (ORDER BY val) FROM t1",
            "SELECT id, ROW_NUMBER() OVER (ORDER BY val) FROM t1",
            catalog, scope,
        )
        # RANK and ROW_NUMBER can differ when values tie:
        # RANK gives same rank to ties, ROW_NUMBER always increments
        assert r.status == "sat"

    def test_rank_different_order(self, catalog, scope):
        """RANK with ASC vs DESC → SAT."""
        r = _synth(
            "SELECT id, RANK() OVER (ORDER BY val ASC) FROM t1",
            "SELECT id, RANK() OVER (ORDER BY val DESC) FROM t1",
            catalog, scope,
        )
        assert r.status == "sat"

    # --- DENSE_RANK ---

    def test_dense_rank_self_equiv(self, catalog, scope):
        """DENSE_RANK() OVER same spec → UNSAT."""
        r = _synth(
            "SELECT id, DENSE_RANK() OVER (ORDER BY val) FROM t1",
            "SELECT id, DENSE_RANK() OVER (ORDER BY val) FROM t1",
            catalog, scope,
        )
        assert r.status == "unsat"

    def test_dense_rank_vs_rank(self, catalog, scope):
        """DENSE_RANK vs RANK can differ with gaps → SAT.

        With k=2 rows, RANK and DENSE_RANK are actually equivalent
        (no gap possible with 2 rows), so expect UNSAT.
        """
        r = _synth(
            "SELECT id, DENSE_RANK() OVER (ORDER BY val) FROM t1",
            "SELECT id, RANK() OVER (ORDER BY val) FROM t1",
            catalog, scope,
        )
        # With 2 rows, RANK and DENSE_RANK produce identical results
        assert r.status == "unsat"

    # --- COUNT OVER ---

    def test_count_window_self_equiv(self, catalog, scope):
        """COUNT(*) OVER (PARTITION BY cat) → UNSAT."""
        r = _synth(
            "SELECT id, COUNT(*) OVER (PARTITION BY cat) FROM t1",
            "SELECT id, COUNT(*) OVER (PARTITION BY cat) FROM t1",
            catalog, scope,
        )
        assert r.status == "unsat"

    def test_count_window_vs_no_partition(self, catalog, scope):
        """COUNT(*) OVER (PARTITION BY cat) vs COUNT(*) OVER () → SAT."""
        r = _synth(
            "SELECT id, COUNT(*) OVER (PARTITION BY cat) FROM t1",
            "SELECT id, COUNT(*) OVER () FROM t1",
            catalog, scope,
        )
        # With different cats, partition count differs from total count
        assert r.status == "sat"

    # --- SUM OVER ---

    def test_sum_window_self_equiv(self, catalog, scope):
        """SUM(val) OVER (PARTITION BY cat) → UNSAT."""
        r = _synth(
            "SELECT id, SUM(val) OVER (PARTITION BY cat) FROM t1",
            "SELECT id, SUM(val) OVER (PARTITION BY cat) FROM t1",
            catalog, scope,
        )
        assert r.status == "unsat"

    def test_sum_window_different_partition(self, catalog, scope):
        """SUM over different partition keys → SAT."""
        r = _synth(
            "SELECT id, SUM(val) OVER (PARTITION BY cat) FROM t1",
            "SELECT id, SUM(val) OVER () FROM t1",
            catalog, scope,
        )
        assert r.status == "sat"

    # --- MAX / MIN OVER ---

    def test_max_window_self_equiv(self, catalog, scope):
        """MAX(val) OVER (PARTITION BY cat) → UNSAT."""
        r = _synth(
            "SELECT id, MAX(val) OVER (PARTITION BY cat) FROM t1",
            "SELECT id, MAX(val) OVER (PARTITION BY cat) FROM t1",
            catalog, scope,
        )
        assert r.status == "unsat"

    def test_min_vs_max_window(self, catalog, scope):
        """MIN vs MAX over same partition → SAT (different aggregates)."""
        r = _synth(
            "SELECT id, MIN(val) OVER (PARTITION BY cat) FROM t1",
            "SELECT id, MAX(val) OVER (PARTITION BY cat) FROM t1",
            catalog, scope,
        )
        assert r.status == "sat"

    # --- Mixed: window + non-window columns ---

    def test_rank_with_partition_self_equiv(self, catalog, scope):
        """Complex: id, val, RANK() OVER (PARTITION BY cat ORDER BY val DESC)."""
        r = _synth(
            "SELECT id, val, RANK() OVER (PARTITION BY cat ORDER BY val DESC) FROM t1",
            "SELECT id, val, RANK() OVER (PARTITION BY cat ORDER BY val DESC) FROM t1",
            catalog, scope,
        )
        assert r.status == "unsat"
