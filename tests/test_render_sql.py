"""Tests for IR → SQL rendering."""

from optim.ir.render_sql import render


def test_simple_select_renders(simple_select_ir):
    """A simple SELECT with WHERE and LIMIT renders valid sqlite3 SQL."""
    sql = render(simple_select_ir, dialect="sqlite")
    assert "SELECT" in sql.upper()
    assert "customers" in sql.lower()
    assert "LIMIT" in sql.upper()


def test_agg_join_renders(agg_join_ir):
    """An aggregate + JOIN query renders valid SQL."""
    sql = render(agg_join_ir, dialect="sqlite")
    assert "JOIN" in sql.upper()
    assert "GROUP BY" in sql.upper()
    assert "SUM" in sql.upper()


def test_simple_select_executes(simple_select_ir, sqlite_conn):
    """Rendered SQL for simple select actually executes on sqlite3."""
    sql = render(simple_select_ir, dialect="sqlite")
    result = sqlite_conn.execute(sql).fetchall()
    # Should return active customers: Alice and Carol
    assert len(result) == 2


def test_agg_join_executes(agg_join_ir, sqlite_conn):
    """Rendered SQL for aggregate join actually executes on sqlite3."""
    sql = render(agg_join_ir, dialect="sqlite")
    result = sqlite_conn.execute(sql).fetchall()
    # Alice: 100 + 200 = 300, Bob: 50
    assert len(result) == 2
    # Find Alice's total
    for name, total in result:
        if name == "Alice":
            assert float(total) == 300.0
        elif name == "Bob":
            assert float(total) == 50.0


def test_roundtrip_parse(simple_select_ir):
    """Rendered SQL survives a sqlglot round-trip parse."""
    # render() already does roundtrip_check internally; if it doesn't raise, we're good
    sql = render(simple_select_ir, dialect="sqlite")
    assert len(sql) > 0
