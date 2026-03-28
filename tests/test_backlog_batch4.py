"""Tests for remaining backlog items: D.9-D.13, A.4, ENG.1-ENG.2."""

import pytest
from optim.ir.types import (
    QueryIR, RelRef, ColumnRef, BinOp, BinOpKind, JoinClause, JoinType, Literal, SemType,
)
from optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from optim.rewrite.rules import RewriteDelta

# D.9
def test_rewrite_delta_creation():
    delta = RewriteDelta(rule_id="R1", affected_aliases={"a", "b"}, description="pushdown")
    assert delta.rule_id == "R1"
    assert "a" in delta.affected_aliases

# D.10
def test_local_block_boundary():
    from optim.cegis.compositional import LocalBlock, InterfaceColumn
    block = LocalBlock(
        aliases={"a", "b", "c"},
        internal_aliases={"c"},
        exported_columns=[],
        original_block=QueryIR(select=[Literal(value=1)], from_table=RelRef(table="a")),
        rewrite_block=QueryIR(select=[Literal(value=1)], from_table=RelRef(table="a")),
    )
    assert block.boundary_aliases == {"a", "b"}

# D.11
def test_row_identity_columns():
    from optim.cegis.compositional import _add_row_identity_columns, InterfaceColumn
    cols = [InterfaceColumn(table_alias="a", column_name="id", sem_type=SemType.INT)]
    result = _add_row_identity_columns(cols, {"a"})
    assert len(result) == 2
    assert result[1].column_name == "__rid"

# D.12
def test_compute_closed_block():
    from optim.cegis.compositional import _compute_closed_block
    ir = QueryIR(
        select=[ColumnRef(table="a", column="id")],
        from_table=RelRef(table="a"),
        joins=[JoinClause(
            join_type=JoinType.INNER,
            right=RelRef(table="b"),
            on=BinOp(op=BinOpKind.EQ, left=ColumnRef(table="a", column="id"), right=ColumnRef(table="b", column="a_id")),
        )],
    )
    result = _compute_closed_block(ir, {"a"})
    assert result is not None
    assert "a" in result

def test_compute_closed_block_exceeds_limit():
    from optim.cegis.compositional import _compute_closed_block
    ir = QueryIR(
        select=[ColumnRef(table="a", column="id")],
        from_table=RelRef(table="a"),
    )
    result = _compute_closed_block(ir, {"a"}, max_aliases=1)
    assert result is not None  # only 1 alias, within limit

# D.13
def test_composition_checker_predicate_move():
    from optim.cegis.composition_checker import check_composition_validity
    from optim.cegis.compositional import LocalRegion, InterfaceColumn
    region = LocalRegion(
        join_idx=0,
        local_aliases={"a", "b"},
        boundary_aliases={"a"},
        interface_columns=[InterfaceColumn(table_alias="a", column_name="id", sem_type=SemType.INT)],
        original_local=QueryIR(select=[Literal(value=1)], from_table=RelRef(table="a")),
        rewrite_local=QueryIR(select=[Literal(value=1)], from_table=RelRef(table="a")),
        proof_kind="predicate_move",
    )
    original = QueryIR(
        select=[ColumnRef(table="a", column="id")],
        from_table=RelRef(table="a"),
        joins=[JoinClause(join_type=JoinType.INNER, right=RelRef(table="b"),
                          on=BinOp(op=BinOpKind.EQ, left=ColumnRef(table="a", column="id"),
                                   right=ColumnRef(table="b", column="a_id")))],
    )
    valid, reason = check_composition_validity(region, original)
    assert valid is True

def test_composition_checker_unknown_kind():
    from optim.cegis.composition_checker import check_composition_validity
    from optim.cegis.compositional import LocalRegion, InterfaceColumn
    region = LocalRegion(
        join_idx=0, local_aliases=set(), boundary_aliases=set(), interface_columns=[],
        original_local=QueryIR(select=[Literal(value=1)], from_table=RelRef(table="a")),
        rewrite_local=QueryIR(select=[Literal(value=1)], from_table=RelRef(table="a")),
        proof_kind="unknown_kind",
    )
    original = QueryIR(select=[Literal(value=1)], from_table=RelRef(table="a"))
    valid, reason = check_composition_validity(region, original)
    assert valid is False

# A.4 / ENG.2
def test_incremental_verifier():
    from optim.cegis.incremental import IncrementalVerifier
    from optim.verify.encode_z3 import BoundedScope
    catalog = Catalog(tables={
        "t": TableInfo(name="t", columns=[ColumnInfo(name="id", sem_type=SemType.INT)]),
    })
    scope = BoundedScope(k_rows=1)
    original = QueryIR(select=[ColumnRef(table="t", column="id")], from_table=RelRef(table="t"))
    verifier = IncrementalVerifier(original_ir=original, catalog=catalog, scope=scope)
    result = verifier.verify(original)
    assert result.status == "unsat"

# ENG.1
def test_verification_context():
    from optim.cegis.verification_context import VerificationContext
    from optim.verify.encode_z3 import BoundedScope
    catalog = Catalog(tables={})
    ctx = VerificationContext(catalog=catalog, scope=BoundedScope())
    ctx.record_result("unsat")
    ctx.record_result("sat")
    ctx.record_result("unknown")
    summary = ctx.summary()
    assert summary["n_verified"] == 3
    assert summary["n_unsat"] == 1
    assert summary["n_sat"] == 1


# D.6 — Witness lifting
def test_lift_local_witness_no_sat():
    """Non-SAT result → None."""
    from optim.cegis.compositional import lift_local_witness
    from optim.cegis.witness_synthesis import WitnessResult
    result = WitnessResult(status="unsat", witness_db=None, solver_time_ms=0)
    assert lift_local_witness(result, None, None, None, None) is None

def test_lift_local_witness_empty_db():
    """SAT with empty witness_db → None."""
    from optim.cegis.compositional import lift_local_witness
    from optim.cegis.witness_synthesis import WitnessResult
    result = WitnessResult(status="sat", witness_db={}, solver_time_ms=0)
    assert lift_local_witness(result, None, None, None, None) is None

def test_default_value_for_type():
    """Check default values are generated correctly."""
    from optim.cegis.compositional import _default_value_for_type
    from optim.ir.types import SemType
    assert _default_value_for_type(SemType.INT) == 1
    assert _default_value_for_type(SemType.STRING) == "default"
    assert _default_value_for_type(SemType.FLOAT) == 1.0
    assert _default_value_for_type(SemType.BOOL) is True
    assert _default_value_for_type(SemType.DATE) == "2025-01-01"
    assert _default_value_for_type(SemType.DECIMAL) == 1.0
    assert _default_value_for_type(SemType.UNKNOWN) is None
