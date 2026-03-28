"""Tests for IR normalization."""

from optim.ir.normalization import normalize, _expr_sort_key
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
)


def test_normalize_deterministic(simple_select_ir):
    """Normalizing the same IR twice produces identical results."""
    n1 = normalize(simple_select_ir)
    n2 = normalize(simple_select_ir)
    assert n1.model_dump() == n2.model_dump()


def test_normalize_column_lowercase():
    """Column and table names are lowercased."""
    ir = QueryIR(
        select=[ColumnRef(table="Orders", column="Total", sem_type=SemType.DECIMAL)],
        from_table=RelRef(table="orders"),
    )
    normed = normalize(ir)
    col = normed.select[0]
    assert isinstance(col, ColumnRef)
    assert col.table == "orders"
    assert col.column == "total"


def test_normalize_count_literal_to_star():
    """COUNT(1) should normalize to COUNT(*)."""
    ir = QueryIR(
        select=[AggCall(func=AggFunc.COUNT, arg=Literal(value=1), alias="cnt")],
        from_table=RelRef(table="t"),
    )
    normed = normalize(ir)
    agg = normed.select[0]
    assert isinstance(agg, AggCall)
    assert agg.arg is None  # COUNT(*)


def test_normalize_count_distinct_preserved():
    """COUNT(DISTINCT col) should not lose DISTINCT."""
    ir = QueryIR(
        select=[
            AggCall(
                func=AggFunc.COUNT,
                arg=ColumnRef(column="id"),
                distinct=True,
                alias="cnt",
            )
        ],
        from_table=RelRef(table="t"),
    )
    normed = normalize(ir)
    agg = normed.select[0]
    assert isinstance(agg, AggCall)
    assert agg.distinct is True
    assert agg.arg is not None


def test_normalize_commutative_eq_orientation():
    """a = b should be canonicalized so smaller key is on the left."""
    # b = a → a = b
    ir = QueryIR(
        select=[ColumnRef(column="x")],
        from_table=RelRef(table="t"),
        where=BinOp(
            op=BinOpKind.EQ,
            left=ColumnRef(table="t", column="z"),
            right=ColumnRef(table="t", column="a"),
        ),
    )
    normed = normalize(ir)
    assert isinstance(normed.where, BinOp)
    # 'a' < 'z' lexicographically
    assert isinstance(normed.where.left, ColumnRef)
    assert normed.where.left.column == "a"
    assert isinstance(normed.where.right, ColumnRef)
    assert normed.where.right.column == "z"


def test_normalize_and_sorting():
    """AND children should be sorted deterministically."""
    # Build: z = 1 AND a = 2 → after norm: a = 2 AND z = 1
    ir = QueryIR(
        select=[ColumnRef(column="x")],
        from_table=RelRef(table="t"),
        where=BinOp(
            op=BinOpKind.AND,
            left=BinOp(
                op=BinOpKind.EQ,
                left=ColumnRef(table="t", column="z"),
                right=Literal(value=1),
            ),
            right=BinOp(
                op=BinOpKind.EQ,
                left=ColumnRef(table="t", column="a"),
                right=Literal(value=2),
            ),
        ),
    )
    normed = normalize(ir)
    assert isinstance(normed.where, BinOp)
    assert normed.where.op == BinOpKind.AND
    # First child should be the one with 'a' (sorts before 'z')
    left = normed.where.left
    assert isinstance(left, BinOp)
    assert isinstance(left.left, ColumnRef)
    assert left.left.column == "a"
