"""Tests for E.2 — LIKE exact encoding.

Tests verify that LIKE predicates with literal patterns are encoded exactly
against the bounded string domain (symbol table), not just as equality.
Also tests the like_encoding module utilities.
"""

from optim.cegis.like_encoding import (
    classify_like_pattern,
    encode_like,
    encode_like_specialized,
    like_to_regex,
)
from optim.cegis.witness_synthesis import (
    BoundedScope,
    synthesize_witness,
)
from optim.ir.types import (
    BinOp,
    BinOpKind,
    ColumnRef,
    Literal,
    QueryIR,
    RelRef,
    SemType,
)
from optim.schema.catalog import Catalog, ColumnInfo, TableInfo

import z3


# ---------------------------------------------------------------------------
# Unit tests for like_encoding module
# ---------------------------------------------------------------------------

def test_classify_exact():
    assert classify_like_pattern("abc") == "exact"


def test_classify_prefix():
    assert classify_like_pattern("abc%") == "prefix"


def test_classify_suffix():
    assert classify_like_pattern("%xyz") == "suffix"


def test_classify_contains():
    assert classify_like_pattern("%mid%") == "contains"


def test_classify_general():
    assert classify_like_pattern("a%b%c") == "general"
    assert classify_like_pattern("a_b") == "general"


def test_classify_escaped_wildcard():
    assert classify_like_pattern("100!%", escape="!") == "exact"


def test_encode_specialized_exact():
    s = z3.String("s")
    result = encode_like_specialized(s, "hello")
    solver = z3.Solver()
    solver.add(result)
    solver.add(s == z3.StringVal("hello"))
    assert solver.check() == z3.sat


def test_encode_specialized_prefix():
    s = z3.String("s")
    result = encode_like_specialized(s, "abc%")
    solver = z3.Solver()
    solver.add(result)
    solver.add(s == z3.StringVal("abcdef"))
    assert solver.check() == z3.sat

    solver2 = z3.Solver()
    solver2.add(result)
    solver2.add(s == z3.StringVal("xyz"))
    assert solver2.check() == z3.unsat


def test_encode_specialized_contains():
    s = z3.String("s")
    result = encode_like_specialized(s, "%world%")
    solver = z3.Solver()
    solver.add(result)
    solver.add(s == z3.StringVal("hello world!"))
    assert solver.check() == z3.sat


def test_like_to_regex_general():
    s = z3.String("s")
    regex = like_to_regex("a_b%c")
    result = z3.InRe(s, regex)
    solver = z3.Solver()
    solver.add(result)
    solver.add(s == z3.StringVal("axbc"))
    assert solver.check() == z3.sat

    solver2 = z3.Solver()
    solver2.add(result)
    solver2.add(s == z3.StringVal("abc"))
    assert solver2.check() == z3.unsat


def test_encode_like_dispatches():
    """encode_like uses specialized for prefix, regex for general."""
    s = z3.String("s")
    # Prefix → specialized
    r1 = encode_like(s, "abc%")
    assert r1 is not None
    # General → regex
    r2 = encode_like(s, "a_b%c")
    assert r2 is not None


# ---------------------------------------------------------------------------
# Integration: LIKE in witness synthesis
# ---------------------------------------------------------------------------

def _catalog() -> Catalog:
    return Catalog(
        tables={
            "t": TableInfo(
                name="t",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="name", sem_type=SemType.STRING, nullable=False),
                ],
                primary_keys=["id"],
            ),
        },
    )


def _scope() -> BoundedScope:
    return BoundedScope(k_rows=2, int_bounds=(0, 10), solver_timeout_ms=10_000)


def test_like_self_equivalence():
    """WHERE name LIKE '%test%' vs itself — UNSAT."""
    catalog = _catalog()
    like_pred = BinOp(
        op=BinOpKind.LIKE,
        left=ColumnRef(table="t", column="name", sem_type=SemType.STRING),
        right=Literal(value="%test%"),
    )
    q1 = QueryIR(
        select=[ColumnRef(table="t", column="id", sem_type=SemType.INT)],
        from_table=RelRef(table="t"),
        where=like_pred,
    )
    q2 = QueryIR(
        select=[ColumnRef(table="t", column="id", sem_type=SemType.INT)],
        from_table=RelRef(table="t"),
        where=like_pred,
    )
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_like_different_patterns_sat():
    """WHERE name LIKE 'abc%' vs WHERE name LIKE 'xyz%' — SAT
    (different patterns can match different strings)."""
    catalog = _catalog()
    q1 = QueryIR(
        select=[ColumnRef(table="t", column="id", sem_type=SemType.INT)],
        from_table=RelRef(table="t"),
        where=BinOp(
            op=BinOpKind.LIKE,
            left=ColumnRef(table="t", column="name", sem_type=SemType.STRING),
            right=Literal(value="abc%"),
        ),
    )
    q2 = QueryIR(
        select=[ColumnRef(table="t", column="id", sem_type=SemType.INT)],
        from_table=RelRef(table="t"),
        where=BinOp(
            op=BinOpKind.LIKE,
            left=ColumnRef(table="t", column="name", sem_type=SemType.STRING),
            right=Literal(value="xyz%"),
        ),
    )
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "sat"
