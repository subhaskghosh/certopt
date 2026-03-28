"""Tests for IR types and basic functionality."""

from optim.ir.types import (
    AggCall,
    AggFunc,
    BinOp,
    BinOpKind,
    ColumnRef,
    Literal,
    QueryIR,
    RelRef,
    SemType,
    _contains_agg,
)


def test_literal_auto_type():
    assert Literal(value=42).sem_type == SemType.INT
    assert Literal(value=3.14).sem_type == SemType.FLOAT
    assert Literal(value="hello").sem_type == SemType.STRING
    assert Literal(value=True).sem_type == SemType.BOOL
    assert Literal(value=None).sem_type == SemType.UNKNOWN


def test_column_ref_fqn():
    col = ColumnRef(table="orders", column="total")
    assert col.fqn() == "orders.total"

    col_bare = ColumnRef(column="total")
    assert col_bare.fqn() == "total"


def test_contains_agg():
    simple_col = ColumnRef(column="x")
    assert not _contains_agg(simple_col)

    agg = AggCall(func=AggFunc.SUM, arg=ColumnRef(column="x"))
    assert _contains_agg(agg)

    nested = BinOp(
        op=BinOpKind.ADD,
        left=AggCall(func=AggFunc.COUNT),
        right=Literal(value=1),
    )
    assert _contains_agg(nested)


def test_query_ir_has_aggregation(simple_select_ir, agg_join_ir):
    assert not simple_select_ir.has_aggregation()
    assert agg_join_ir.has_aggregation()


def test_query_ir_projected_columns(agg_join_ir):
    cols = agg_join_ir.projected_columns()
    assert cols == ["name", "total_spent"]
