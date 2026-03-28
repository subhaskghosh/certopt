"""Tests for E.3 — scalar function exact encoding.

Tests verify that NULLIF, ABS, CAST, GREATEST, LEAST produce exact Z3 encodings
(not fresh unconstrained variables), enabling correct SAT/UNSAT results.
"""

from optim.cegis.witness_synthesis import (
    BoundedScope,
    synthesize_witness,
)
from optim.ir.types import (
    BinOp,
    BinOpKind,
    ColumnRef,
    FuncCall,
    Literal,
    QueryIR,
    RelRef,
    SemType,
)
from optim.schema.catalog import Catalog, ColumnInfo, TableInfo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _catalog() -> Catalog:
    """Catalog: t(id PK, a INT, b INT, c INT nullable)."""
    return Catalog(
        tables={
            "t": TableInfo(
                name="t",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="a", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="b", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="c", sem_type=SemType.INT, nullable=True),
                ],
                primary_keys=["id"],
            ),
        },
    )


def _scope() -> BoundedScope:
    return BoundedScope(k_rows=2, int_bounds=(-5, 10), solver_timeout_ms=10_000)


def _col(name: str) -> ColumnRef:
    return ColumnRef(table="t", column=name, sem_type=SemType.INT)


# ---------------------------------------------------------------------------
# NULLIF
# ---------------------------------------------------------------------------

def test_nullif_self_equivalence():
    """SELECT NULLIF(a, b) vs SELECT NULLIF(a, b) — UNSAT (identical)."""
    catalog = _catalog()
    expr = FuncCall(func_name="NULLIF", args=[_col("a"), _col("b")])
    q1 = QueryIR(select=[expr], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[expr], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_nullif_vs_column_sat():
    """SELECT NULLIF(a, b) vs SELECT a — SAT when a=b (NULLIF returns NULL)."""
    catalog = _catalog()
    q1 = QueryIR(
        select=[FuncCall(func_name="NULLIF", args=[_col("a"), _col("b")])],
        from_table=RelRef(table="t"),
    )
    q2 = QueryIR(select=[_col("a")], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "sat"


def test_nullif_different_args_sat():
    """SELECT NULLIF(a, 0) vs SELECT NULLIF(a, 1) — SAT when a=0 or a=1."""
    catalog = _catalog()
    q1 = QueryIR(
        select=[FuncCall(func_name="NULLIF", args=[_col("a"), Literal(value=0)])],
        from_table=RelRef(table="t"),
    )
    q2 = QueryIR(
        select=[FuncCall(func_name="NULLIF", args=[_col("a"), Literal(value=1)])],
        from_table=RelRef(table="t"),
    )
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "sat"


# ---------------------------------------------------------------------------
# ABS
# ---------------------------------------------------------------------------

def test_abs_self_equivalence():
    """SELECT ABS(a) vs SELECT ABS(a) — UNSAT."""
    catalog = _catalog()
    expr = FuncCall(func_name="ABS", args=[_col("a")])
    q1 = QueryIR(select=[expr], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[expr], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_abs_neg_x_equals_abs_x():
    """SELECT ABS(-a) vs SELECT ABS(a) — UNSAT (|−x| = |x|)."""
    catalog = _catalog()
    from optim.ir.types import UnaryOp, UnaryOpKind
    q1 = QueryIR(
        select=[FuncCall(func_name="ABS", args=[
            UnaryOp(op=UnaryOpKind.NEG, operand=_col("a")),
        ])],
        from_table=RelRef(table="t"),
    )
    q2 = QueryIR(
        select=[FuncCall(func_name="ABS", args=[_col("a")])],
        from_table=RelRef(table="t"),
    )
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_abs_vs_identity_sat():
    """SELECT ABS(a) vs SELECT a — SAT when a < 0."""
    catalog = _catalog()
    q1 = QueryIR(
        select=[FuncCall(func_name="ABS", args=[_col("a")])],
        from_table=RelRef(table="t"),
    )
    q2 = QueryIR(select=[_col("a")], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "sat"


# ---------------------------------------------------------------------------
# GREATEST / LEAST
# ---------------------------------------------------------------------------

def test_greatest_self_equivalence():
    """SELECT GREATEST(a, b) vs SELECT GREATEST(a, b) — UNSAT."""
    catalog = _catalog()
    expr = FuncCall(func_name="GREATEST", args=[_col("a"), _col("b")])
    q1 = QueryIR(select=[expr], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[expr], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_greatest_vs_least_sat():
    """SELECT GREATEST(a, b) vs SELECT LEAST(a, b) — SAT when a ≠ b."""
    catalog = _catalog()
    q1 = QueryIR(
        select=[FuncCall(func_name="GREATEST", args=[_col("a"), _col("b")])],
        from_table=RelRef(table="t"),
    )
    q2 = QueryIR(
        select=[FuncCall(func_name="LEAST", args=[_col("a"), _col("b")])],
        from_table=RelRef(table="t"),
    )
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "sat"


def test_greatest_ge_any_arg_unsat():
    """WHERE GREATEST(a, b) >= a is a tautology for non-null a, b.

    SELECT a FROM t WHERE GREATEST(a,b) >= a  vs  SELECT a FROM t
    Should be UNSAT since GREATEST(a,b) >= a is always true.
    """
    catalog = _catalog()
    q1 = QueryIR(
        select=[_col("a")],
        from_table=RelRef(table="t"),
        where=BinOp(
            op=BinOpKind.GTE,
            left=FuncCall(func_name="GREATEST", args=[_col("a"), _col("b")]),
            right=_col("a"),
        ),
    )
    q2 = QueryIR(select=[_col("a")], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_greatest_null_propagation():
    """SELECT GREATEST(c, 1) vs SELECT 1 — SAT because c can be NULL → result NULL."""
    catalog = _catalog()
    q1 = QueryIR(
        select=[FuncCall(func_name="GREATEST", args=[_col("c"), Literal(value=1)])],
        from_table=RelRef(table="t"),
    )
    q2 = QueryIR(
        select=[Literal(value=1)],
        from_table=RelRef(table="t"),
    )
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "sat"


def test_least_three_args():
    """SELECT LEAST(a, b, 3) self-equivalence — UNSAT."""
    catalog = _catalog()
    expr = FuncCall(func_name="LEAST", args=[_col("a"), _col("b"), Literal(value=3)])
    q1 = QueryIR(select=[expr], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[expr], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


# ---------------------------------------------------------------------------
# CAST
# ---------------------------------------------------------------------------

def test_cast_identity():
    """SELECT CAST(a AS INT) vs SELECT a — UNSAT (identity under IntSort)."""
    catalog = _catalog()
    q1 = QueryIR(
        select=[FuncCall(func_name="CAST", args=[_col("a")])],
        from_table=RelRef(table="t"),
    )
    q2 = QueryIR(select=[_col("a")], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


# ---------------------------------------------------------------------------
# LENGTH (under integer-coded strings)
# ---------------------------------------------------------------------------

def test_length_self_equivalence():
    """SELECT LENGTH(c) vs SELECT LENGTH(c) — UNSAT."""
    catalog = _catalog()
    expr = FuncCall(func_name="LENGTH", args=[_col("c")])
    q1 = QueryIR(select=[expr], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[expr], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_length_vs_identity_sat():
    """SELECT LENGTH(a) vs SELECT a — SAT (length ≠ value in general)."""
    catalog = _catalog()
    q1 = QueryIR(
        select=[FuncCall(func_name="LENGTH", args=[_col("a")])],
        from_table=RelRef(table="t"),
    )
    q2 = QueryIR(select=[_col("a")], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "sat"
