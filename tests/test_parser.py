"""Tests for SQL → IR parsing (optim.parser.sql_to_ir)."""

from optim.ir.render_sql import render
from optim.ir.types import (
    AggCall,
    AggFunc,
    BinOp,
    BinOpKind,
    ColumnRef,
    FuncCall,
    InSubquery,
    JoinType,
    Literal,
    QueryIR,
    RelRef,
    SemType,
    SetOpKind,
    SortDir,
    Star,
)
from optim.parser.sql_to_ir import sql_to_ir


# ---------------------------------------------------------------------------
# Simple SELECT
# ---------------------------------------------------------------------------


def test_simple_select():
    """Simple SELECT parses from_table and select columns."""
    ir, err = sql_to_ir("SELECT id, name FROM t")
    assert err is None and ir is not None
    assert isinstance(ir.from_table, RelRef)
    assert ir.from_table.table == "t"
    assert len(ir.select) == 2
    assert isinstance(ir.select[0], ColumnRef)
    assert ir.select[0].column == "id"
    assert isinstance(ir.select[1], ColumnRef)
    assert ir.select[1].column == "name"


# ---------------------------------------------------------------------------
# JOIN
# ---------------------------------------------------------------------------


def test_join_query():
    """JOIN query populates joins list with correct type and ON clause."""
    sql = "SELECT a.id, b.val FROM a JOIN b ON a.id = b.a_id"
    ir, err = sql_to_ir(sql)
    assert err is None and ir is not None
    assert len(ir.joins) == 1
    jc = ir.joins[0]
    assert jc.join_type == JoinType.INNER
    assert isinstance(jc.right, RelRef)
    assert jc.right.table == "b"
    assert isinstance(jc.on, BinOp)
    assert jc.on.op == BinOpKind.EQ


# ---------------------------------------------------------------------------
# Aggregate + GROUP BY + HAVING
# ---------------------------------------------------------------------------


def test_aggregate_group_by_having():
    """Aggregate query with GROUP BY and HAVING is parsed correctly."""
    sql = "SELECT category, COUNT(*) AS cnt FROM products GROUP BY category HAVING COUNT(*) > 5"
    ir, err = sql_to_ir(sql)
    assert err is None and ir is not None
    # GROUP BY
    assert len(ir.group_by) == 1
    assert isinstance(ir.group_by[0], ColumnRef)
    assert ir.group_by[0].column == "category"
    # Aggregate in SELECT
    assert isinstance(ir.select[1], AggCall)
    assert ir.select[1].func == AggFunc.COUNT
    assert ir.select[1].alias == "cnt"
    # HAVING
    assert ir.having is not None
    assert isinstance(ir.having, BinOp)
    assert ir.having.op == BinOpKind.GT


# ---------------------------------------------------------------------------
# DISTINCT
# ---------------------------------------------------------------------------


def test_distinct_flag():
    """DISTINCT flag is captured on the IR."""
    ir, err = sql_to_ir("SELECT DISTINCT id FROM t")
    assert err is None and ir is not None
    assert ir.distinct is True

    ir2, err2 = sql_to_ir("SELECT id FROM t")
    assert err2 is None and ir2 is not None
    assert ir2.distinct is False


# ---------------------------------------------------------------------------
# ORDER BY + LIMIT
# ---------------------------------------------------------------------------


def test_order_by_and_limit():
    """ORDER BY and LIMIT are captured on the IR."""
    sql = "SELECT id, name FROM t ORDER BY name DESC LIMIT 10"
    ir, err = sql_to_ir(sql)
    assert err is None and ir is not None
    assert len(ir.order_by) == 1
    assert ir.order_by[0].direction == SortDir.DESC
    assert isinstance(ir.order_by[0].expr, ColumnRef)
    assert ir.order_by[0].expr.column == "name"
    assert ir.limit == 10


# ---------------------------------------------------------------------------
# Set operation (UNION ALL)
# ---------------------------------------------------------------------------


def test_union_all():
    """UNION ALL parses into set_op / set_right fields."""
    sql = "SELECT id FROM a UNION ALL SELECT id FROM b"
    ir, err = sql_to_ir(sql)
    assert err is None and ir is not None
    assert ir.set_op == SetOpKind.UNION_ALL
    assert ir.set_right is not None
    assert isinstance(ir.set_right.from_table, RelRef)
    assert ir.set_right.from_table.table == "b"


# ---------------------------------------------------------------------------
# Subquery in WHERE (IN subquery)
# ---------------------------------------------------------------------------


def test_in_subquery():
    """IN (SELECT ...) in WHERE is parsed as InSubquery."""
    sql = "SELECT id FROM t WHERE id IN (SELECT a_id FROM s)"
    ir, err = sql_to_ir(sql)
    assert err is None and ir is not None
    assert ir.where is not None
    assert isinstance(ir.where, InSubquery)
    assert isinstance(ir.where.query, QueryIR)
    assert isinstance(ir.where.query.from_table, RelRef)
    assert ir.where.query.from_table.table == "s"


# ---------------------------------------------------------------------------
# Invalid SQL
# ---------------------------------------------------------------------------


def test_invalid_sql_returns_none_and_error():
    """Completely invalid SQL returns (None, error_string)."""
    ir, err = sql_to_ir("NOT VALID SQL AT ALL !!!")
    assert ir is None
    assert err is not None
    assert isinstance(err, str)
    assert len(err) > 0


def test_no_from_clause_uses_dual():
    """A SELECT without FROM uses __values_dual__ sentinel table."""
    ir, err = sql_to_ir("SELECT 1+1")
    assert ir is not None
    assert err is None
    assert ir.from_table.table == "__values_dual__"


# ---------------------------------------------------------------------------
# Dialect (sqlite is default)
# ---------------------------------------------------------------------------


def test_default_dialect_is_sqlite():
    """Default dialect parses SQLite-flavoured SQL."""
    ir, err = sql_to_ir("SELECT id FROM t")
    assert err is None and ir is not None


def test_explicit_sqlite_dialect():
    """Explicit sqlite dialect works the same as default."""
    ir_default, _ = sql_to_ir("SELECT id FROM t")
    ir_explicit, _ = sql_to_ir("SELECT id FROM t", dialect="sqlite")
    assert ir_default is not None and ir_explicit is not None
    assert ir_default.from_table.table == ir_explicit.from_table.table


# ---------------------------------------------------------------------------
# Roundtrip: parse → render → parse preserves structure
# ---------------------------------------------------------------------------


def test_roundtrip_preserves_structure():
    """parse → render → parse produces an IR with the same shape."""
    sql = "SELECT id, name FROM t WHERE id > 5 ORDER BY name ASC LIMIT 20"
    ir1, err1 = sql_to_ir(sql)
    assert err1 is None and ir1 is not None

    rendered = render(ir1, dialect="sqlite")
    ir2, err2 = sql_to_ir(rendered, dialect="sqlite")
    assert err2 is None and ir2 is not None

    # Structural checks
    assert len(ir1.select) == len(ir2.select)
    assert ir1.from_table.table == ir2.from_table.table
    assert ir1.limit == ir2.limit
    assert len(ir1.order_by) == len(ir2.order_by)
    assert ir1.distinct == ir2.distinct
    assert ir1.where is not None and ir2.where is not None


def test_roundtrip_agg_query():
    """Roundtrip for aggregate query preserves GROUP BY and HAVING."""
    sql = "SELECT category, COUNT(*) AS cnt FROM products GROUP BY category HAVING COUNT(*) > 1"
    ir1, err1 = sql_to_ir(sql)
    assert err1 is None and ir1 is not None

    rendered = render(ir1, dialect="sqlite")
    ir2, err2 = sql_to_ir(rendered, dialect="sqlite")
    assert err2 is None and ir2 is not None

    assert len(ir1.group_by) == len(ir2.group_by)
    assert ir1.having is not None and ir2.having is not None
    assert ir1.has_aggregation() == ir2.has_aggregation()


# ---------------------------------------------------------------------------
# JOIN USING / NATURAL JOIN (FIX.23 parser extensions)
# ---------------------------------------------------------------------------


def test_join_using_single_column():
    """JOIN ... USING(col) desugars to ON clause."""
    ir, err = sql_to_ir("SELECT * FROM A LEFT JOIN B USING(ID)")
    assert ir is not None and err is None
    assert len(ir.joins) == 1
    on = ir.joins[0].on
    assert isinstance(on, BinOp) and on.op == BinOpKind.EQ


def test_join_using_multiple_columns():
    """JOIN ... USING(c1, c2) desugars to AND-ed ON clauses."""
    ir, err = sql_to_ir("SELECT * FROM A JOIN B USING(X, Y)")
    assert ir is not None and err is None
    on = ir.joins[0].on
    assert isinstance(on, BinOp) and on.op == BinOpKind.AND


def test_natural_join_with_catalog():
    """NATURAL JOIN desugars using catalog column lookup."""
    from optim.schema.catalog import Catalog, TableInfo, ColumnInfo

    catalog = Catalog(tables={
        'T1': TableInfo(name='T1', columns=[
            ColumnInfo(name='ID', sem_type=SemType.INT, nullable=False, is_primary_key=True),
            ColumnInfo(name='NAME', sem_type=SemType.STRING, nullable=True),
        ], primary_keys=['ID']),
        'T2': TableInfo(name='T2', columns=[
            ColumnInfo(name='ID', sem_type=SemType.INT, nullable=False, is_primary_key=False),
            ColumnInfo(name='VALUE', sem_type=SemType.INT, nullable=True),
        ], primary_keys=[]),
    })
    ir, err = sql_to_ir("SELECT * FROM T1 NATURAL JOIN T2", catalog=catalog)
    assert ir is not None and err is None
    assert len(ir.joins) == 1


# ---------------------------------------------------------------------------
# Date/Interval functions (FIX.23 parser extensions)
# ---------------------------------------------------------------------------


def test_interval_expression():
    """INTERVAL N DAY parses as integer literal."""
    ir, err = sql_to_ir("SELECT x + INTERVAL 1 DAY FROM T", dialect="mysql")
    assert ir is not None and err is None


def test_date_add():
    """DATE_ADD parses to BinOp ADD."""
    ir, err = sql_to_ir("SELECT DATE_ADD(x, INTERVAL 1 DAY) FROM T", dialect="mysql")
    assert ir is not None and err is None
    assert isinstance(ir.select[0], BinOp) and ir.select[0].op == BinOpKind.ADD


def test_date_sub():
    """DATE_SUB parses to BinOp SUB."""
    ir, err = sql_to_ir("SELECT DATE_SUB(x, INTERVAL 1 DAY) FROM T", dialect="mysql")
    assert ir is not None and err is None
    assert isinstance(ir.select[0], BinOp) and ir.select[0].op == BinOpKind.SUB


def test_datediff():
    """DATEDIFF parses to BinOp SUB."""
    ir, err = sql_to_ir("SELECT DATEDIFF(a, b) FROM T", dialect="mysql")
    assert ir is not None and err is None
    assert isinstance(ir.select[0], BinOp) and ir.select[0].op == BinOpKind.SUB


# ---------------------------------------------------------------------------
# SELECT without FROM (FIX.23)
# ---------------------------------------------------------------------------


def test_select_without_from_scalar():
    """SELECT scalar_expr without FROM uses dual table."""
    ir, err = sql_to_ir("SELECT 1 + 2 AS x")
    assert ir is not None and err is None
    assert ir.from_table.table == "__values_dual__"


# ---------------------------------------------------------------------------
# IIF / IFNULL (FIX.23 parser extensions)
# ---------------------------------------------------------------------------


def test_ifnull_parses():
    """IFNULL(a, b) parses as FuncCall."""
    ir, err = sql_to_ir("SELECT IFNULL(x, 0) FROM T")
    assert ir is not None and err is None


def test_iif_parses():
    """IIF(cond, a, b) / IF(cond, a, b) parses as FuncCall."""
    ir, err = sql_to_ir("SELECT IIF(x > 5, 1, 0) FROM T")
    assert ir is not None and err is None
