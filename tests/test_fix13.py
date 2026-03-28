"""Tests for FIX.13 — star expansion, DuckDB validation, $-identifier normalization.

Each test targets a specific failure pattern observed in the Calcite-397 benchmark.
"""

import pytest

from optim.cegis.witness_export import (
    ValidationResult,
    _normalize_for_duckdb,
    _normalize_for_sqlite,
    validate_witness_sql,
)
from optim.cegis.witness_synthesis import (
    BoundedScope,
    _expand_stars,
    synthesize_witness,
)
from optim.ir.types import (
    AggCall,
    AggFunc,
    BinOp,
    BinOpKind,
    ColumnRef,
    DerivedTable,
    Literal,
    QueryIR,
    RelRef,
    SemType,
    SetOpKind,
    Star,
    UnaryOp,
    UnaryOpKind,
)
from optim.schema.catalog import Catalog, ColumnInfo, TableInfo


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _emp_catalog() -> Catalog:
    """Catalog matching VeriEQL Calcite benchmark EMP table."""
    return Catalog(tables={
        "emp": TableInfo(
            name="emp",
            columns=[
                ColumnInfo(name="EMPNO", sem_type=SemType.INT, nullable=False),
                ColumnInfo(name="ENAME", sem_type=SemType.STRING, nullable=True),
                ColumnInfo(name="JOB", sem_type=SemType.STRING, nullable=True),
                ColumnInfo(name="MGR", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="HIREDATE", sem_type=SemType.DATE, nullable=True),
                ColumnInfo(name="SAL", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="COMM", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="DEPTNO", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="SLACKER", sem_type=SemType.BOOL, nullable=True),
            ],
        ),
        "empnullables": TableInfo(
            name="empnullables",
            columns=[
                ColumnInfo(name="EMPNO", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="ENAME", sem_type=SemType.STRING, nullable=True),
                ColumnInfo(name="JOB", sem_type=SemType.STRING, nullable=True),
                ColumnInfo(name="MGR", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="HIREDATE", sem_type=SemType.DATE, nullable=True),
                ColumnInfo(name="SAL", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="COMM", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="DEPTNO", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="SLACKER", sem_type=SemType.BOOL, nullable=True),
            ],
        ),
    })


def _scope() -> BoundedScope:
    return BoundedScope(k_rows=2, int_bounds=(0, 20), solver_timeout_ms=10_000)


# ---------------------------------------------------------------------------
# FIX.13a: $-identifier normalization
# ---------------------------------------------------------------------------

class TestDollarIdentNormalization:
    """Test that $-identifiers are correctly quoted for SQLite and DuckDB."""

    def test_sqlite_bare_dollar_at_start(self):
        """$cor0.EMPNO → "$cor0".EMPNO"""
        result = _normalize_for_sqlite('$cor0.EMPNO')
        assert '"$cor0"' in result
        assert 'EMPNO' in result

    def test_sqlite_dollar_after_dot(self):
        """t.$f0 → t."$f0" """
        result = _normalize_for_sqlite('SELECT t.$f0 FROM t')
        assert '"$f0"' in result

    def test_sqlite_mid_identifier_unchanged(self):
        """EXPR$0 should NOT be quoted ($ is mid-identifier)."""
        result = _normalize_for_sqlite('SELECT EXPR$0 FROM t')
        assert 'EXPR$0' in result
        assert '"$0"' not in result

    def test_sqlite_column_alias_mid_dollar(self):
        """AS t (EXPR$0, EXPR$1) — $ mid-identifier stays unchanged."""
        result = _normalize_for_sqlite('AS t (EXPR$0, EXPR$1)')
        assert 'EXPR$0' in result
        assert 'EXPR$1' in result

    def test_sqlite_dollar_in_parens(self):
        """($f0) → ("$f0")"""
        result = _normalize_for_sqlite('AS t ($f0)')
        assert '"$f0"' in result

    def test_duckdb_bare_dollar_at_start(self):
        result = _normalize_for_duckdb('$cor0.EMPNO')
        assert '"$cor0"' in result

    def test_duckdb_mid_identifier_unchanged(self):
        result = _normalize_for_duckdb('SELECT EXPR$0 FROM t')
        assert 'EXPR$0' in result
        assert '"$0"' not in result

    def test_duckdb_dollar_after_dot(self):
        result = _normalize_for_duckdb('SELECT t.$f0 FROM t')
        assert '"$f0"' in result

    def test_sqlite_backtick_to_double_quote(self):
        result = _normalize_for_sqlite('SELECT `col` FROM `tbl`')
        assert '"col"' in result
        assert '"tbl"' in result
        assert '`' not in result

    def test_sqlite_substring_to_substr(self):
        result = _normalize_for_sqlite('SELECT SUBSTRING(x, 1, 3)')
        assert 'SUBSTR(' in result
        assert 'SUBSTRING' not in result


# ---------------------------------------------------------------------------
# FIX.13a: DuckDB validation
# ---------------------------------------------------------------------------

class TestDuckDBValidation:
    """Test DuckDB validation fallback."""

    def test_duckdb_import_available(self):
        """DuckDB should be importable."""
        import duckdb
        assert duckdb is not None

    def test_duckdb_validate_matching_results(self):
        """Witness where both queries return the same result → spurious."""
        from optim.cegis.witness_export import validate_witness_duckdb

        catalog = Catalog(tables={
            "t": TableInfo(
                name="t",
                columns=[ColumnInfo(name="x", sem_type=SemType.INT, nullable=False)],
            ),
        })
        witness_db = {"t": [{"x": 1}, {"x": 2}]}

        result = validate_witness_duckdb(
            "SELECT x FROM t ORDER BY x",
            "SELECT x FROM t ORDER BY x",
            witness_db, catalog,
        )
        assert not result.results_differ
        assert result.is_spurious

    def test_duckdb_validate_different_results(self):
        """Witness where queries return different results → confirmed."""
        from optim.cegis.witness_export import validate_witness_duckdb

        catalog = Catalog(tables={
            "t": TableInfo(
                name="t",
                columns=[ColumnInfo(name="x", sem_type=SemType.INT, nullable=False)],
            ),
        })
        witness_db = {"t": [{"x": 5}, {"x": 10}]}

        result = validate_witness_duckdb(
            "SELECT x FROM t WHERE x > 3",
            "SELECT x FROM t WHERE x > 7",
            witness_db, catalog,
        )
        assert result.results_differ
        assert not result.is_spurious

    def test_duckdb_validate_values_syntax(self):
        """DuckDB handles VALUES syntax that SQLite may not."""
        from optim.cegis.witness_export import validate_witness_duckdb

        catalog = Catalog(tables={
            "t": TableInfo(
                name="t",
                columns=[ColumnInfo(name="x", sem_type=SemType.INT, nullable=False)],
            ),
        })
        witness_db = {"t": [{"x": 1}]}

        result = validate_witness_duckdb(
            "SELECT 1 AS x",
            "SELECT 1 AS x",
            witness_db, catalog,
        )
        assert not result.error
        assert result.is_spurious

    def test_duckdb_coerces_string_sentinels_in_int_columns(self):
        """Sentinel strings like '__fresh_lo__' in INT columns → NULL, no crash."""
        from optim.cegis.witness_export import validate_witness_duckdb

        catalog = Catalog(tables={
            "t": TableInfo(
                name="t",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="val", sem_type=SemType.INT, nullable=True),
                ],
            ),
        })
        witness_db = {"t": [
            {"id": 1, "val": "__fresh_lo__"},  # sentinel string in INT col
            {"id": 2, "val": 42},
        ]}

        result = validate_witness_duckdb(
            "SELECT id, val FROM t",
            "SELECT id, val FROM t",
            witness_db, catalog,
        )
        # Should not error — coerces string to None
        assert not result.error
        assert result.is_spurious

    def test_duckdb_coerces_sentinels_in_date_columns(self):
        """Sentinel strings in DATE columns → NULL, no crash."""
        from optim.cegis.witness_export import validate_witness_duckdb

        catalog = Catalog(tables={
            "t": TableInfo(
                name="t",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="dt", sem_type=SemType.DATE, nullable=True),
                ],
            ),
        })
        witness_db = {"t": [
            {"id": 1, "dt": "\x01__fresh_lo__"},
            {"id": 2, "dt": "2024-01-15"},
        ]}

        result = validate_witness_duckdb(
            "SELECT id, dt FROM t",
            "SELECT id, dt FROM t",
            witness_db, catalog,
        )
        assert not result.error
        assert result.is_spurious


# ---------------------------------------------------------------------------
# FIX.13b: Star expansion
# ---------------------------------------------------------------------------

class TestStarExpansion:
    """Test _expand_stars() correctly replaces Star() with ColumnRefs."""

    def test_star_from_base_table(self):
        """SELECT * FROM emp → SELECT EMPNO, ENAME, ... FROM emp"""
        catalog = _emp_catalog()
        ir = QueryIR(
            select=[Star()],
            from_table=RelRef(table="emp"),
        )
        expanded = _expand_stars(ir, catalog)
        assert not any(isinstance(e, Star) for e in expanded.select)
        col_names = [e.column for e in expanded.select if isinstance(e, ColumnRef)]
        assert "EMPNO" in col_names
        assert "SAL" in col_names
        assert len(col_names) == 9  # all EMP columns

    def test_star_from_derived_table_with_explicit_select(self):
        """SELECT * FROM (SELECT EMPNO, SAL FROM emp) AS t → t.EMPNO, t.SAL"""
        catalog = _emp_catalog()
        inner = QueryIR(
            select=[
                ColumnRef(table="emp", column="EMPNO", alias="EMPNO"),
                ColumnRef(table="emp", column="SAL", alias="SAL"),
            ],
            from_table=RelRef(table="emp"),
        )
        ir = QueryIR(
            select=[Star()],
            from_table=DerivedTable(query=inner, alias="t"),
        )
        expanded = _expand_stars(ir, catalog)
        assert not any(isinstance(e, Star) for e in expanded.select)
        col_names = [e.column for e in expanded.select if isinstance(e, ColumnRef)]
        assert col_names == ["EMPNO", "SAL"]
        # All should reference alias "t"
        assert all(e.table == "t" for e in expanded.select if isinstance(e, ColumnRef))

    def test_star_from_derived_table_with_column_aliases(self):
        """SELECT * FROM (SELECT EMPNO, SAL FROM emp) AS t(A, B) → t.A, t.B"""
        catalog = _emp_catalog()
        inner = QueryIR(
            select=[
                ColumnRef(table="emp", column="EMPNO"),
                ColumnRef(table="emp", column="SAL"),
            ],
            from_table=RelRef(table="emp"),
        )
        ir = QueryIR(
            select=[Star()],
            from_table=DerivedTable(query=inner, alias="t", column_aliases=["A", "B"]),
        )
        expanded = _expand_stars(ir, catalog)
        col_names = [e.column for e in expanded.select if isinstance(e, ColumnRef)]
        assert col_names == ["A", "B"]

    def test_star_from_values_derived_table(self):
        """SELECT * FROM (SELECT 11 AS c0 UNION ALL SELECT 23 AS c0) AS t"""
        catalog = _emp_catalog()
        inner_left = QueryIR(
            select=[Literal(value=11, alias="c0")],
            from_table=RelRef(table="__values_dual__"),
        )
        inner_right = QueryIR(
            select=[Literal(value=23, alias="c0")],
            from_table=RelRef(table="__values_dual__"),
        )
        inner_left.set_op = SetOpKind.UNION_ALL
        inner_left.set_right = inner_right
        ir = QueryIR(
            select=[Star()],
            from_table=DerivedTable(query=inner_left, alias="_values"),
        )
        expanded = _expand_stars(ir, catalog)
        assert not any(isinstance(e, Star) for e in expanded.select)
        col_names = [e.column for e in expanded.select if isinstance(e, ColumnRef)]
        assert col_names == ["c0"]

    def test_no_star_unchanged(self):
        """Query without Star() is returned unchanged."""
        catalog = _emp_catalog()
        ir = QueryIR(
            select=[ColumnRef(table="emp", column="EMPNO")],
            from_table=RelRef(table="emp"),
        )
        expanded = _expand_stars(ir, catalog)
        assert len(expanded.select) == 1
        assert isinstance(expanded.select[0], ColumnRef)

    def test_star_with_other_exprs(self):
        """SELECT *, total FROM orders → expands only the Star."""
        catalog = Catalog(tables={
            "orders": TableInfo(
                name="orders",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="total", sem_type=SemType.INT, nullable=False),
                ],
            ),
        })
        ir = QueryIR(
            select=[Star(), ColumnRef(table="orders", column="total")],
            from_table=RelRef(table="orders"),
        )
        expanded = _expand_stars(ir, catalog)
        assert len(expanded.select) == 3  # id, total (from star), total (explicit)

    def test_nested_star_through_derived(self):
        """SELECT * FROM (SELECT * FROM emp) AS t — inner star expanded first."""
        catalog = _emp_catalog()
        inner = QueryIR(
            select=[Star()],
            from_table=RelRef(table="emp"),
        )
        ir = QueryIR(
            select=[Star()],
            from_table=DerivedTable(query=inner, alias="t"),
        )
        expanded = _expand_stars(ir, catalog)
        assert not any(isinstance(e, Star) for e in expanded.select)
        col_names = [e.column for e in expanded.select if isinstance(e, ColumnRef)]
        assert "EMPNO" in col_names
        assert len(col_names) == 9

    def test_star_set_right_recurse(self):
        """Star in set_right (UNION ALL) is also expanded."""
        catalog = _emp_catalog()
        left = QueryIR(
            select=[ColumnRef(table="emp", column="EMPNO")],
            from_table=RelRef(table="emp"),
        )
        right = QueryIR(
            select=[Star()],
            from_table=RelRef(table="emp"),
        )
        left.set_op = SetOpKind.UNION_ALL
        left.set_right = right

        expanded = _expand_stars(left, catalog)
        # The right branch should have stars expanded
        assert expanded.set_right is not None
        assert not any(isinstance(e, Star) for e in expanded.set_right.select)


# ---------------------------------------------------------------------------
# FIX.13b: End-to-end star expansion + synthesis
# ---------------------------------------------------------------------------

class TestStarExpansionE2E:
    """End-to-end tests: star expansion fixes specific benchmark failures."""

    def test_star_through_simple_derived_table_unsat(self):
        """Calcite pair 38 pattern: SELECT * FROM (SELECT cols FROM t WHERE ...) AS t0
        vs SELECT cols FROM t WHERE ... — should be EQU (unsat)."""
        catalog = _emp_catalog()
        scope = _scope()

        inner = QueryIR(
            select=[
                ColumnRef(table="empnullables", column="EMPNO"),
                ColumnRef(table="empnullables", column="SAL"),
            ],
            from_table=RelRef(table="empnullables"),
            where=BinOp(
                op=BinOpKind.GT,
                left=ColumnRef(table="empnullables", column="SAL"),
                right=Literal(value=1000),
            ),
        )
        q1 = QueryIR(
            select=[Star()],
            from_table=DerivedTable(query=inner, alias="t0"),
        )
        q2 = QueryIR(
            select=[
                ColumnRef(table="empnullables", column="EMPNO"),
                ColumnRef(table="empnullables", column="SAL"),
            ],
            from_table=RelRef(table="empnullables"),
            where=BinOp(
                op=BinOpKind.GT,
                left=ColumnRef(table="empnullables", column="SAL"),
                right=Literal(value=1000),
            ),
        )

        result = synthesize_witness(q1, q2, catalog, scope)
        assert result.status == "unsat", f"Expected unsat, got {result.status}"

    def test_star_vs_explicit_columns_same_table(self):
        """SELECT * FROM emp vs SELECT EMPNO, ..., SLACKER FROM emp — EQU."""
        catalog = _emp_catalog()
        scope = _scope()

        q1 = QueryIR(
            select=[Star()],
            from_table=RelRef(table="emp"),
        )
        emp_cols = [c.name for c in catalog.get_table("emp").columns]
        q2 = QueryIR(
            select=[ColumnRef(table="emp", column=c) for c in emp_cols],
            from_table=RelRef(table="emp"),
        )

        result = synthesize_witness(q1, q2, catalog, scope)
        assert result.status == "unsat", f"Expected unsat, got {result.status}"


# ---------------------------------------------------------------------------
# FIX.13a+13b: Validation gate integration
# ---------------------------------------------------------------------------

class TestValidationGateIntegration:
    """Test that the validation gate correctly uses DuckDB as primary."""

    def test_sqlite_validation_succeeds(self):
        """Basic SQLite validation: spurious witness is caught."""
        catalog = Catalog(tables={
            "t": TableInfo(
                name="t",
                columns=[ColumnInfo(name="x", sem_type=SemType.INT, nullable=False)],
            ),
        })
        witness = {"t": [{"x": 5}]}
        result = validate_witness_sql(
            "SELECT x FROM t", "SELECT x FROM t",
            witness, catalog,
        )
        assert result.is_spurious
        assert not result.error

    def test_sqlite_validation_confirms_difference(self):
        """SQLite validation: real witness is confirmed."""
        catalog = Catalog(tables={
            "t": TableInfo(
                name="t",
                columns=[ColumnInfo(name="x", sem_type=SemType.INT, nullable=False)],
            ),
        })
        witness = {"t": [{"x": 5}]}
        result = validate_witness_sql(
            "SELECT x FROM t WHERE x > 3",
            "SELECT x FROM t WHERE x > 7",
            witness, catalog,
        )
        assert result.results_differ
        assert not result.is_spurious

    def test_duckdb_primary_catches_spurious_witness(self):
        """DuckDB-primary validation gate downgrades spurious SAT→UNKNOWN."""
        catalog = Catalog(tables={
            "t": TableInfo(
                name="t",
                columns=[ColumnInfo(name="x", sem_type=SemType.INT, nullable=False)],
            ),
        })
        # Construct two identical-result queries but with different IR
        q1 = QueryIR(
            select=[ColumnRef(table="t", column="x")],
            from_table=RelRef(table="t"),
            where=BinOp(op=BinOpKind.GT,
                        left=ColumnRef(table="t", column="x"),
                        right=Literal(value=0)),
        )
        q2 = QueryIR(
            select=[ColumnRef(table="t", column="x")],
            from_table=RelRef(table="t"),
            where=BinOp(op=BinOpKind.GTE,
                        left=ColumnRef(table="t", column="x"),
                        right=Literal(value=1)),
        )
        scope = BoundedScope(k_rows=2, int_bounds=(0, 10), solver_timeout_ms=5000)
        result = synthesize_witness(
            q1, q2, catalog, scope,
            validate_witnesses=True,
            original_sql=("SELECT x FROM t WHERE x > 0",
                          "SELECT x FROM t WHERE x >= 1"),
        )
        # With int domain, x > 0 ≡ x >= 1 for integers, so should be unsat
        # or if SAT, validation catches it
        assert result.status in ("unsat", "unknown")


# ---------------------------------------------------------------------------
# FIX.13c: Faithful aggregation through derived tables
# ---------------------------------------------------------------------------

class TestAggregationThroughDerivedTable:
    """Test HAVING with aggregate functions is evaluated correctly."""

    def test_having_count_star_filters(self):
        """SELECT DEPTNO, COUNT(*) FROM emp GROUP BY DEPTNO HAVING COUNT(*) >= 2
        vs SELECT DEPTNO, COUNT(*) FROM emp GROUP BY DEPTNO — different results."""
        catalog = _emp_catalog()
        scope = _scope()

        q1 = QueryIR(
            select=[
                ColumnRef(table="emp", column="DEPTNO"),
                AggCall(func=AggFunc.COUNT, arg=None),
            ],
            from_table=RelRef(table="emp"),
            group_by=[ColumnRef(table="emp", column="DEPTNO")],
            having=BinOp(
                op=BinOpKind.GTE,
                left=AggCall(func=AggFunc.COUNT, arg=None),
                right=Literal(value=2),
            ),
        )
        q2 = QueryIR(
            select=[
                ColumnRef(table="emp", column="DEPTNO"),
                AggCall(func=AggFunc.COUNT, arg=None),
            ],
            from_table=RelRef(table="emp"),
            group_by=[ColumnRef(table="emp", column="DEPTNO")],
        )

        result = synthesize_witness(q1, q2, catalog, scope)
        # Should find a witness: q2 returns all groups, q1 filters some out
        assert result.status == "sat"

    def test_having_count_eq_self_equivalence(self):
        """Same query with HAVING COUNT(*) = 1 should be self-equivalent."""
        catalog = _emp_catalog()
        scope = _scope()

        having = BinOp(
            op=BinOpKind.EQ,
            left=AggCall(func=AggFunc.COUNT, arg=None),
            right=Literal(value=1),
        )
        q1 = QueryIR(
            select=[
                ColumnRef(table="emp", column="DEPTNO"),
                AggCall(func=AggFunc.COUNT, arg=None),
            ],
            from_table=RelRef(table="emp"),
            group_by=[ColumnRef(table="emp", column="DEPTNO")],
            having=having,
        )
        q2 = QueryIR(
            select=[
                ColumnRef(table="emp", column="DEPTNO"),
                AggCall(func=AggFunc.COUNT, arg=None),
            ],
            from_table=RelRef(table="emp"),
            group_by=[ColumnRef(table="emp", column="DEPTNO")],
            having=BinOp(
                op=BinOpKind.EQ,
                left=AggCall(func=AggFunc.COUNT, arg=None),
                right=Literal(value=1),
            ),
        )

        result = synthesize_witness(q1, q2, catalog, scope)
        assert result.status == "unsat"


# ---------------------------------------------------------------------------
# FIX.13d: Improved CAST encoding
# ---------------------------------------------------------------------------

class TestCastEncoding:
    """Test improved CAST encoding."""

    def test_cast_null_as_integer_is_null(self):
        """CAST(NULL AS INTEGER) → NULL, same as plain NULL."""
        from optim.ir.types import FuncCall
        catalog = _emp_catalog()
        scope = _scope()

        q1 = QueryIR(
            select=[FuncCall(func_name="CAST", args=[Literal(value=None), Literal(value="INTEGER")])],
            from_table=RelRef(table="emp"),
        )
        q2 = QueryIR(
            select=[Literal(value=None)],
            from_table=RelRef(table="emp"),
        )

        result = synthesize_witness(q1, q2, catalog, scope)
        assert result.status == "unsat"

    def test_cast_integer_identity(self):
        """CAST(x AS INTEGER) ≡ x for integer columns."""
        from optim.ir.types import FuncCall
        catalog = _emp_catalog()
        scope = _scope()

        q1 = QueryIR(
            select=[FuncCall(func_name="CAST", args=[
                ColumnRef(table="emp", column="SAL"),
                Literal(value="INTEGER"),
            ])],
            from_table=RelRef(table="emp"),
        )
        q2 = QueryIR(
            select=[ColumnRef(table="emp", column="SAL")],
            from_table=RelRef(table="emp"),
        )

        result = synthesize_witness(q1, q2, catalog, scope)
        assert result.status == "unsat"


# ---------------------------------------------------------------------------
# FIX.13a+: DuckDB DATE column type mapping
# ---------------------------------------------------------------------------

class TestDuckDBDateTypes:
    """Test that DuckDB uses native DATE/TIMESTAMP types."""

    def test_duckdb_date_column_extract(self):
        """DuckDB should create DATE columns so EXTRACT works."""
        from optim.cegis.witness_export import validate_witness_duckdb

        catalog = Catalog(tables={
            "t": TableInfo(
                name="t",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="dt", sem_type=SemType.DATE, nullable=True),
                ],
            ),
        })
        witness_db = {"t": [
            {"id": 1, "dt": "2024-03-15"},
            {"id": 2, "dt": "2024-06-20"},
        ]}

        result = validate_witness_duckdb(
            "SELECT id, EXTRACT(YEAR FROM dt) FROM t",
            "SELECT id, EXTRACT(YEAR FROM dt) FROM t",
            witness_db, catalog,
        )
        assert not result.error
        assert result.is_spurious

    def test_duckdb_invalid_date_string_becomes_null(self):
        """Non-ISO date strings in DATE columns → NULL, no crash."""
        from optim.cegis.witness_export import validate_witness_duckdb

        catalog = Catalog(tables={
            "t": TableInfo(
                name="t",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="dt", sem_type=SemType.DATE, nullable=True),
                ],
            ),
        })
        witness_db = {"t": [
            {"id": 1, "dt": "not_a_date"},
            {"id": 2, "dt": "2024-03-15"},
        ]}

        result = validate_witness_duckdb(
            "SELECT id, dt FROM t",
            "SELECT id, dt FROM t",
            witness_db, catalog,
        )
        assert not result.error
        assert result.is_spurious


# ---------------------------------------------------------------------------
# FIX.13e: Aggregate expressions in SELECT (not just top-level AggCall)
# ---------------------------------------------------------------------------

class TestAggregateExpressions:
    """Test that aggregate expressions nested in arithmetic/functions are
    properly resolved in the Z3 encoding."""

    def test_deptno_plus_sum_sal_alias_only(self):
        """Calcite pair 107 pattern: DEPTNO + SUM(SAL) vs same with alias.
        These are identical and should be UNSAT."""
        catalog = _emp_catalog()
        scope = _scope()

        q1 = QueryIR(
            select=[
                BinOp(op=BinOpKind.ADD,
                      left=ColumnRef(table="emp", column="DEPTNO"),
                      right=AggCall(func=AggFunc.SUM,
                                    arg=ColumnRef(table="emp", column="SAL"))),
            ],
            from_table=RelRef(table="emp"),
            group_by=[
                ColumnRef(table="emp", column="JOB"),
                ColumnRef(table="emp", column="DEPTNO"),
            ],
        )
        q2 = QueryIR(
            select=[
                BinOp(op=BinOpKind.ADD,
                      left=ColumnRef(table="emp", column="DEPTNO"),
                      right=AggCall(func=AggFunc.SUM,
                                    arg=ColumnRef(table="emp", column="SAL")),
                      alias="$f0"),
            ],
            from_table=RelRef(table="emp"),
            group_by=[
                ColumnRef(table="emp", column="JOB"),
                ColumnRef(table="emp", column="DEPTNO"),
            ],
        )
        result = synthesize_witness(q1, q2, catalog, scope)
        assert result.status == "unsat"

    def test_coalesce_sum_zero(self):
        """COALESCE(SUM(SAL), 0) vs SUM(SAL) — different when SUM is NULL."""
        from optim.ir.types import FuncCall
        catalog = _emp_catalog()
        scope = _scope()

        q1 = QueryIR(
            select=[
                FuncCall(func_name="COALESCE", args=[
                    AggCall(func=AggFunc.SUM,
                            arg=ColumnRef(table="emp", column="SAL")),
                    Literal(value=0),
                ]),
            ],
            from_table=RelRef(table="emp"),
            group_by=[ColumnRef(table="emp", column="DEPTNO")],
        )
        q2 = QueryIR(
            select=[
                AggCall(func=AggFunc.SUM,
                        arg=ColumnRef(table="emp", column="SAL")),
            ],
            from_table=RelRef(table="emp"),
            group_by=[ColumnRef(table="emp", column="DEPTNO")],
        )
        result = synthesize_witness(q1, q2, catalog, scope)
        # COALESCE(SUM(SAL), 0) ≠ SUM(SAL) when group has all NULLs
        assert result.status == "sat"

    def test_cast_sum_as_integer_identity(self):
        """CAST(SUM(SAL) AS INTEGER) ≡ SUM(SAL) under integer encoding."""
        from optim.ir.types import FuncCall
        catalog = _emp_catalog()
        scope = _scope()

        q1 = QueryIR(
            select=[
                FuncCall(func_name="CAST", args=[
                    AggCall(func=AggFunc.SUM,
                            arg=ColumnRef(table="emp", column="SAL")),
                    Literal(value="INTEGER"),
                ]),
            ],
            from_table=RelRef(table="emp"),
            group_by=[ColumnRef(table="emp", column="DEPTNO")],
        )
        q2 = QueryIR(
            select=[
                AggCall(func=AggFunc.SUM,
                        arg=ColumnRef(table="emp", column="SAL")),
            ],
            from_table=RelRef(table="emp"),
            group_by=[ColumnRef(table="emp", column="DEPTNO")],
        )
        result = synthesize_witness(q1, q2, catalog, scope)
        assert result.status == "unsat"

    def test_count_star_empty_group_coalesce(self):
        """COALESCE(COUNT(*), 0) ≡ COUNT(*) (COUNT never returns NULL)."""
        from optim.ir.types import FuncCall
        catalog = _emp_catalog()
        scope = _scope()

        q1 = QueryIR(
            select=[
                FuncCall(func_name="COALESCE", args=[
                    AggCall(func=AggFunc.COUNT, arg=None),
                    Literal(value=0),
                ]),
            ],
            from_table=RelRef(table="emp"),
        )
        q2 = QueryIR(
            select=[AggCall(func=AggFunc.COUNT, arg=None)],
            from_table=RelRef(table="emp"),
        )
        result = synthesize_witness(q1, q2, catalog, scope)
        assert result.status == "unsat"

    def test_sum_sal_plus_one(self):
        """SUM(SAL) + 1 ≡ SUM(SAL) + 1 (same expression)."""
        catalog = _emp_catalog()
        scope = _scope()

        expr = BinOp(op=BinOpKind.ADD,
                     left=AggCall(func=AggFunc.SUM,
                                  arg=ColumnRef(table="emp", column="SAL")),
                     right=Literal(value=1))
        q1 = QueryIR(
            select=[expr],
            from_table=RelRef(table="emp"),
            group_by=[ColumnRef(table="emp", column="DEPTNO")],
        )
        q2 = QueryIR(
            select=[
                BinOp(op=BinOpKind.ADD,
                      left=AggCall(func=AggFunc.SUM,
                                   arg=ColumnRef(table="emp", column="SAL")),
                      right=Literal(value=1)),
            ],
            from_table=RelRef(table="emp"),
            group_by=[ColumnRef(table="emp", column="DEPTNO")],
        )
        result = synthesize_witness(q1, q2, catalog, scope)
        assert result.status == "unsat"


# ---------------------------------------------------------------------------
# FIX.13e: DuckDB integer-to-date coercion
# ---------------------------------------------------------------------------

class TestDuckDBIntegerDateCoercion:
    """Test that integer values in DATE columns are coerced to date strings."""

    def test_duckdb_integer_in_date_column(self):
        """Integer value in DATE column should be coerced to date string."""
        from optim.cegis.witness_export import validate_witness_duckdb

        catalog = Catalog(tables={
            "t": TableInfo(
                name="t",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="dt", sem_type=SemType.DATE, nullable=True),
                ],
            ),
        })
        witness_db = {"t": [
            {"id": 1, "dt": 0},    # 0 → 2024-01-01
            {"id": 2, "dt": 100},  # 100 → 2024-04-10
        ]}

        result = validate_witness_duckdb(
            "SELECT id, dt FROM t",
            "SELECT id, dt FROM t",
            witness_db, catalog,
        )
        assert not result.error
        assert result.is_spurious
