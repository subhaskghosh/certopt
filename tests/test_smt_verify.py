"""Tests for SMT-backed verification and certificates."""

from optim.ir.types import (
    AggCall,
    AggFunc,
    BinOp,
    BinOpKind,
    ColumnRef,
    JoinClause,
    JoinType,
    Literal,
    QueryIR,
    RelRef,
    SemType,
)
from optim.schema.catalog import Catalog
from optim.verify.constraints import smt_verify, ConstraintKind
from optim.verify.unsat_cores import analyze_unsat_core, format_diagnostics
from optim.verify.certificate import (
    create_certificate,
    replay_certificate,
)


# ---------------------------------------------------------------------------
# SMT verification tests
# ---------------------------------------------------------------------------

def test_valid_query_is_sat(simple_select_ir, sample_catalog):
    """A valid query should verify as SAT."""
    result = smt_verify(simple_select_ir, sample_catalog)
    assert result.ok, f"Expected SAT but got {result.status}: {result.unsat_core_labels}"
    assert result.certified


def test_valid_agg_join_is_sat(agg_join_ir, sample_catalog):
    """A valid aggregate + join query should verify as SAT."""
    result = smt_verify(agg_join_ir, sample_catalog)
    assert result.ok, f"Expected SAT: {result.unsat_core_labels}"


def test_bad_table_is_unsat(sample_catalog):
    """Referencing a nonexistent table → UNSAT."""
    ir = QueryIR(
        select=[ColumnRef(table="ghost_table", column="x")],
        from_table=RelRef(table="ghost_table"),
        limit=10,
    )
    result = smt_verify(ir, sample_catalog)
    assert not result.ok
    assert result.status == "unsat"
    assert len(result.unsat_core_labels) > 0
    # The core should mention the ghost table
    assert any("ghost_table" in label for label in result.unsat_core_labels)


def test_bad_column_is_unsat(sample_catalog):
    """Referencing a nonexistent column → UNSAT."""
    ir = QueryIR(
        select=[ColumnRef(table="customers", column="nonexistent_col")],
        from_table=RelRef(table="customers"),
        limit=10,
    )
    result = smt_verify(ir, sample_catalog)
    assert not result.ok
    assert any("nonexistent_col" in label for label in result.unsat_core_labels)


def test_sum_on_string_is_unsat(sample_catalog):
    """SUM on a string column → UNSAT."""
    ir = QueryIR(
        select=[
            AggCall(
                func=AggFunc.SUM,
                arg=ColumnRef(table="customers", column="name", sem_type=SemType.STRING),
            ),
        ],
        from_table=RelRef(table="customers"),
        limit=10,
    )
    result = smt_verify(ir, sample_catalog)
    assert not result.ok
    # Should have a type_soundness violation
    failed = result.failed_constraints()
    assert any(c.kind == ConstraintKind.TYPE_SOUNDNESS for c in failed)


def test_grouping_violation_is_unsat(sample_catalog):
    """Non-aggregated column not in GROUP BY → UNSAT."""
    ir = QueryIR(
        select=[
            ColumnRef(table="customers", column="name", sem_type=SemType.STRING),
            ColumnRef(table="customers", column="email", sem_type=SemType.STRING),
            AggCall(func=AggFunc.COUNT, alias="cnt"),
        ],
        from_table=RelRef(table="customers"),
        group_by=[
            ColumnRef(table="customers", column="name", sem_type=SemType.STRING),
        ],
        limit=10,
    )
    # Use non-sqlite dialect to enforce standard SQL grouping rules
    result = smt_verify(ir, sample_catalog, dialect="postgres")
    assert not result.ok
    failed = result.failed_constraints()
    assert any(c.kind == ConstraintKind.GROUPING_LEGALITY for c in failed)


def test_invalid_join_is_unsat(sample_catalog):
    """Join on columns with no FK relationship → UNSAT."""
    ir = QueryIR(
        select=[
            ColumnRef(table="c", column="name"),
        ],
        from_table=RelRef(table="customers", alias="c"),
        joins=[
            JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="products", alias="p"),
                on=BinOp(
                    op=BinOpKind.EQ,
                    left=ColumnRef(table="c", column="name"),
                    right=ColumnRef(table="p", column="category"),
                    # customers.name = products.category has no FK
                ),
            ),
        ],
        limit=10,
    )
    result = smt_verify(ir, sample_catalog)
    assert not result.ok
    failed = result.failed_constraints()
    assert any(c.kind == ConstraintKind.JOIN_VALIDITY for c in failed)


def test_same_name_non_pk_column_join_rejected(sample_catalog):
    """Join on same-named non-PK columns without explicit FK should be rejected."""
    # customers.name = products.name — same column name but neither is PK, no FK declared
    ir = QueryIR(
        select=[
            ColumnRef(table="c", column="name"),
            ColumnRef(table="p", column="category"),
        ],
        from_table=RelRef(table="customers", alias="c"),
        joins=[
            JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="products", alias="p"),
                on=BinOp(
                    op=BinOpKind.EQ,
                    left=ColumnRef(table="c", column="name"),
                    right=ColumnRef(table="p", column="name"),
                ),
            ),
        ],
        limit=10,
    )
    result = smt_verify(ir, sample_catalog)
    assert not result.ok, "Same-name non-PK join should be UNSAT"


def test_same_name_pk_column_join_allowed():
    """Join on same-named columns where one is PK should be allowed."""
    from optim.schema.catalog import ColumnInfo, TableInfo
    catalog = Catalog(
        tables={
            "table_a": TableInfo(
                name="table_a",
                columns=[
                    ColumnInfo(name="code", sem_type=SemType.STRING, nullable=False),
                ],
                primary_keys=[],
            ),
            "table_b": TableInfo(
                name="table_b",
                columns=[
                    ColumnInfo(name="code", sem_type=SemType.STRING, nullable=False, is_primary_key=True),
                ],
                primary_keys=["code"],
            ),
        },
        foreign_keys=[],
    )
    ir = QueryIR(
        select=[ColumnRef(table="a", column="code")],
        from_table=RelRef(table="table_a", alias="a"),
        joins=[
            JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="table_b", alias="b"),
                on=BinOp(
                    op=BinOpKind.EQ,
                    left=ColumnRef(table="a", column="code"),
                    right=ColumnRef(table="b", column="code"),
                ),
            ),
        ],
        limit=10,
    )
    result = smt_verify(ir, catalog)
    assert result.ok, f"Same-name PK join should be SAT: {result.unsat_core_labels}"


def test_pk_prefix_join_allowed():
    """Join where one col is PK and the other is a prefix/suffix match should be allowed."""
    from optim.schema.catalog import ColumnInfo, ForeignKey, TableInfo
    # Mimics cards.setCode = sets.code where sets.code is PK
    catalog = Catalog(
        tables={
            "cards": TableInfo(
                name="cards",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="setCode", sem_type=SemType.STRING, nullable=False),
                ],
                primary_keys=["id"],
            ),
            "sets": TableInfo(
                name="sets",
                columns=[
                    ColumnInfo(name="code", sem_type=SemType.STRING, nullable=False, is_primary_key=True),
                    ColumnInfo(name="name", sem_type=SemType.STRING, nullable=True),
                ],
                primary_keys=["code"],
            ),
        },
        foreign_keys=[],  # No FK declared
    )
    ir = QueryIR(
        select=[
            ColumnRef(table="cards", column="id"),
            ColumnRef(table="sets", column="name"),
        ],
        from_table=RelRef(table="cards"),
        joins=[
            JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="sets"),
                on=BinOp(
                    op=BinOpKind.EQ,
                    left=ColumnRef(table="cards", column="setCode"),
                    right=ColumnRef(table="sets", column="code"),
                ),
            ),
        ],
        limit=10,
    )
    result = smt_verify(ir, catalog)
    assert result.ok, f"PK prefix join should be SAT: {result.unsat_core_labels}"


def test_unrelated_column_join_still_rejected(sample_catalog):
    """Join on different-named, non-PK columns should still be rejected."""
    # customers.email = products.category — no name relationship, not a PK
    ir = QueryIR(
        select=[ColumnRef(table="c", column="name")],
        from_table=RelRef(table="customers", alias="c"),
        joins=[
            JoinClause(
                join_type=JoinType.INNER,
                right=RelRef(table="products", alias="p"),
                on=BinOp(
                    op=BinOpKind.EQ,
                    left=ColumnRef(table="c", column="email"),
                    right=ColumnRef(table="p", column="category"),
                ),
            ),
        ],
        limit=10,
    )
    result = smt_verify(ir, sample_catalog)
    assert not result.ok
    failed = result.failed_constraints()
    assert any(c.kind == ConstraintKind.JOIN_VALIDITY for c in failed)


def test_cross_join_policy_violation(sample_catalog):
    """CROSS JOIN should be rejected by policy."""
    ir = QueryIR(
        select=[ColumnRef(table="customers", column="name")],
        from_table=RelRef(table="customers"),
        joins=[
            JoinClause(
                join_type=JoinType.CROSS,
                right=RelRef(table="orders"),
                on=Literal(value=True, sem_type=SemType.BOOL),
            ),
        ],
        limit=10,
    )
    result = smt_verify(ir, sample_catalog)
    assert not result.ok
    failed = result.failed_constraints()
    assert any(c.kind == ConstraintKind.POLICY for c in failed)


# ---------------------------------------------------------------------------
# Unsat core diagnostics tests
# ---------------------------------------------------------------------------

def test_unsat_core_diagnostics(sample_catalog):
    """Diagnostics should produce meaningful error messages."""
    ir = QueryIR(
        select=[ColumnRef(table="ghost", column="x")],
        from_table=RelRef(table="ghost"),
        limit=10,
    )
    result = smt_verify(ir, sample_catalog)
    report = analyze_unsat_core(result)
    assert report.has_errors
    assert len(report.diagnostics) > 0
    formatted = format_diagnostics(report)
    assert "error" in formatted.lower() or "ERROR" in formatted


def test_sat_has_no_diagnostics(simple_select_ir, sample_catalog):
    """SAT results should have empty diagnostics."""
    result = smt_verify(simple_select_ir, sample_catalog)
    report = analyze_unsat_core(result)
    assert not report.has_errors


# ---------------------------------------------------------------------------
# Certificate tests
# ---------------------------------------------------------------------------

def test_create_certificate(simple_select_ir, sample_catalog):
    """A certified query produces a valid certificate."""
    result = smt_verify(simple_select_ir, sample_catalog)
    assert result.certified

    cert = create_certificate(simple_select_ir, sample_catalog, result)
    assert cert.solver_status == "sat"
    assert len(cert.ir_hash) > 0
    assert len(cert.sql_hash) > 0
    assert "SELECT" in cert.sql.upper()
    assert len(cert.constraints) > 0


def test_certificate_json_roundtrip(simple_select_ir, sample_catalog):
    """Certificate serializes to JSON and back."""
    result = smt_verify(simple_select_ir, sample_catalog)
    cert = create_certificate(simple_select_ir, sample_catalog, result)
    json_str = cert.to_json()
    assert len(json_str) > 0
    # Should be valid JSON
    import json
    data = json.loads(json_str)
    assert data["solver_status"] == "sat"


def test_certificate_save_load(simple_select_ir, sample_catalog, tmp_path):
    """Certificate saves to disk and loads back."""
    result = smt_verify(simple_select_ir, sample_catalog)
    cert = create_certificate(simple_select_ir, sample_catalog, result)
    cert.save(tmp_path / "cert")

    loaded = cert.load(tmp_path / "cert")
    assert loaded.solver_status == cert.solver_status
    assert loaded.ir_hash == cert.ir_hash
    assert loaded.sql_hash == cert.sql_hash


def test_replay_certificate(simple_select_ir, sample_catalog):
    """Replaying a valid certificate succeeds."""
    result = smt_verify(simple_select_ir, sample_catalog)
    cert = create_certificate(simple_select_ir, sample_catalog, result)
    replay = replay_certificate(cert, sample_catalog)
    assert replay.valid, f"Replay failed: {replay.errors}"
    assert replay.ir_hash_match
    assert replay.sql_hash_match
    assert replay.constraint_count_match


def test_replay_agg_join_certificate(agg_join_ir, sample_catalog):
    """Replaying a certificate for agg+join query succeeds."""
    result = smt_verify(agg_join_ir, sample_catalog)
    assert result.certified
    cert = create_certificate(agg_join_ir, sample_catalog, result)
    replay = replay_certificate(cert, sample_catalog)
    assert replay.valid, f"Replay failed: {replay.errors}"


def test_replay_fails_if_ir_tampered(simple_select_ir, sample_catalog):
    """Replaying a certificate with tampered IR detects the mismatch."""
    result = smt_verify(simple_select_ir, sample_catalog)
    assert result.certified
    cert = create_certificate(simple_select_ir, sample_catalog, result)

    # Tamper: change a column name in the serialized IR to break schema validity
    cert.ir_json["select"][0]["column"] = "nonexistent_column"
    replay = replay_certificate(cert, sample_catalog)
    assert not replay.valid
    assert not replay.ir_hash_match


def test_certificate_save_load_with_catalog(simple_select_ir, sample_catalog, tmp_path):
    """Certificate saved with catalog can be loaded and replayed standalone."""
    result = smt_verify(simple_select_ir, sample_catalog)
    cert = create_certificate(simple_select_ir, sample_catalog, result)
    cert.save(tmp_path / "cert", catalog=sample_catalog)

    # Load certificate + catalog from disk
    from optim.verify.certificate import Certificate
    loaded_cert = Certificate.load(tmp_path / "cert")
    loaded_catalog = Certificate.load_catalog(tmp_path / "cert")

    assert loaded_catalog is not None
    assert set(loaded_catalog.tables.keys()) == set(sample_catalog.tables.keys())

    replay = replay_certificate(loaded_cert, loaded_catalog)
    assert replay.valid, f"Replay with loaded catalog failed: {replay.errors}"


def test_cannot_certify_unsat(sample_catalog):
    """Cannot create a certificate from an UNSAT result."""
    ir = QueryIR(
        select=[ColumnRef(table="ghost", column="x")],
        from_table=RelRef(table="ghost"),
    )
    result = smt_verify(ir, sample_catalog)
    import pytest
    with pytest.raises(ValueError, match="Cannot certify"):
        create_certificate(ir, sample_catalog, result)


# ---------------------------------------------------------------------------
# DerivedTable verification regression tests
# ---------------------------------------------------------------------------

from optim.ir.types import DerivedTable


def _derived_table_ir(sample_catalog) -> QueryIR:
    """SELECT sub.total_spent FROM (SELECT customer_id, SUM(total) AS total_spent FROM orders GROUP BY customer_id) AS sub."""
    inner = QueryIR(
        select=[
            ColumnRef(table="orders", column="customer_id", sem_type=SemType.INT),
            AggCall(func=AggFunc.SUM, arg=ColumnRef(table="orders", column="total", sem_type=SemType.DECIMAL), alias="total_spent"),
        ],
        from_table=RelRef(table="orders"),
        group_by=[ColumnRef(table="orders", column="customer_id", sem_type=SemType.INT)],
    )
    return QueryIR(
        select=[ColumnRef(table="sub", column="total_spent")],
        from_table=DerivedTable(query=inner, alias="sub"),
        limit=10,
    )


def test_derived_table_valid_ref_is_sat(sample_catalog):
    """Valid qualified ref to a derived table column → SAT."""
    ir = _derived_table_ir(sample_catalog)
    result = smt_verify(ir, sample_catalog)
    assert result.ok, f"Expected SAT but got {result.status}: {result.unsat_core_labels}"


def test_derived_table_invalid_ref_is_unsat(sample_catalog):
    """Invalid qualified ref to nonexistent derived table column → UNSAT."""
    inner = QueryIR(
        select=[
            ColumnRef(table="orders", column="customer_id", sem_type=SemType.INT, alias="cid"),
        ],
        from_table=RelRef(table="orders"),
    )
    ir = QueryIR(
        select=[ColumnRef(table="sub", column="nonexistent_col")],
        from_table=DerivedTable(query=inner, alias="sub"),
        limit=10,
    )
    result = smt_verify(ir, sample_catalog)
    assert not result.ok
    assert any("nonexistent_col" in label for label in result.unsat_core_labels)


def test_derived_table_unqualified_ref_is_sat(sample_catalog):
    """Unqualified ref to a derived table column → SAT."""
    inner = QueryIR(
        select=[
            AggCall(func=AggFunc.COUNT, alias="cnt"),
        ],
        from_table=RelRef(table="orders"),
    )
    ir = QueryIR(
        select=[ColumnRef(column="cnt")],
        from_table=DerivedTable(query=inner, alias="sub"),
        limit=10,
    )
    result = smt_verify(ir, sample_catalog)
    assert result.ok, f"Expected SAT but got {result.status}: {result.unsat_core_labels}"


def test_derived_table_arithmetic_on_string_is_unsat(sample_catalog):
    """Arithmetic on a STRING column through a derived table → UNSAT."""
    inner = QueryIR(
        select=[
            ColumnRef(table="customers", column="name", sem_type=SemType.STRING, alias="nm"),
        ],
        from_table=RelRef(table="customers"),
    )
    ir = QueryIR(
        select=[BinOp(
            op=BinOpKind.ADD,
            left=ColumnRef(table="sub", column="nm"),
            right=Literal(value=1),
        )],
        from_table=DerivedTable(query=inner, alias="sub"),
        limit=10,
    )
    result = smt_verify(ir, sample_catalog)
    assert not result.ok, f"Expected UNSAT for arithmetic on STRING through derived table, got {result.status}"
    assert any("type_arith" in label for label in result.unsat_core_labels)


def test_set_right_invalid_column_is_unsat(sample_catalog):
    """Invalid column in UNION right branch should be caught by verifier."""
    from optim.ir.types import SetOpKind
    ir = QueryIR(
        select=[ColumnRef(table="customers", column="name")],
        from_table=RelRef(table="customers"),
        set_op=SetOpKind.UNION,
        set_right=QueryIR(
            select=[ColumnRef(table="customers", column="nonexistent_col")],
            from_table=RelRef(table="customers"),
        ),
    )
    result = smt_verify(ir, sample_catalog)
    assert not result.ok, f"Expected UNSAT for invalid column in set_right, got {result.status}"
    assert any("setR" in label for label in result.unsat_core_labels)


def test_set_right_valid_union_is_sat(sample_catalog):
    """Valid UNION query should pass verification."""
    from optim.ir.types import SetOpKind
    ir = QueryIR(
        select=[ColumnRef(table="customers", column="name")],
        from_table=RelRef(table="customers"),
        set_op=SetOpKind.UNION,
        set_right=QueryIR(
            select=[ColumnRef(table="customers", column="name")],
            from_table=RelRef(table="customers"),
        ),
    )
    result = smt_verify(ir, sample_catalog)
    assert result.ok, f"Expected SAT for valid UNION, got {result.status}: {result.unsat_core_labels}"
