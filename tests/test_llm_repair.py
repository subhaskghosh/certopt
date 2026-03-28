"""Tests for schema-grounded LLM repair and CandidateOutcome."""

import pytest

from optim.ir.types import (
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
from optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from optim.llm.repair import (
    _levenshtein,
    _normalize_case,
    _repair_columns,
    _complete_join_paths,
    repair_candidate,
)
from optim.optimizer.result import CandidateOutcome


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def catalog():
    """Simple customers/orders catalog."""
    return Catalog(
        tables={
            "customers": TableInfo(
                name="customers",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="name", sem_type=SemType.STRING, nullable=False),
                ],
                primary_keys=["id"],
            ),
            "orders": TableInfo(
                name="orders",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="customer_id", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="amount", sem_type=SemType.INT, nullable=False),
                ],
                primary_keys=["id"],
            ),
        },
        foreign_keys=[
            ForeignKey(src_table="orders", src_column="customer_id",
                       dst_table="customers", dst_column="id"),
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCaseNormalization:

    def test_case_normalization(self, catalog):
        """IR with 'Customers' (uppercase) → repaired to 'customers'."""
        ir = QueryIR(
            select=[ColumnRef(table="Customers", column="Name")],
            from_table=RelRef(table="Customers"),
            where=BinOp(
                op=BinOpKind.EQ,
                left=ColumnRef(table="Customers", column="id"),
                right=Literal(value=1),
            ),
        )
        repaired = _normalize_case(ir.model_copy(deep=True), catalog)
        assert repaired.from_table.table == "customers"
        assert repaired.select[0].table == "customers"
        assert repaired.select[0].column == "name"
        assert repaired.where.left.table == "customers"


class TestColumnRepair:

    def test_column_repair_typo(self, catalog):
        """IR with ColumnRef(column='naem') → repaired to 'name' (edit distance 1)."""
        ir = QueryIR(
            select=[ColumnRef(table="customers", column="naem")],
            from_table=RelRef(table="customers"),
        )
        repaired, details = _repair_columns(ir.model_copy(deep=True), catalog)
        assert repaired.select[0].column == "name"
        assert any("naem" in d and "name" in d for d in details)

    def test_column_repair_ambiguous_skipped(self):
        """Two columns within distance ≤ 2 → no repair (ambiguous)."""
        catalog = Catalog(
            tables={
                "t": TableInfo(
                    name="t",
                    columns=[
                        ColumnInfo(name="ab", sem_type=SemType.STRING, nullable=False),
                        ColumnInfo(name="ac", sem_type=SemType.STRING, nullable=False),
                    ],
                    primary_keys=[],
                ),
            },
            foreign_keys=[],
        )
        ir = QueryIR(
            select=[ColumnRef(table="t", column="aa")],
            from_table=RelRef(table="t"),
        )
        repaired, details = _repair_columns(ir.model_copy(deep=True), catalog)
        # "aa" is distance 1 from both "ab" and "ac" → ambiguous, no repair
        assert repaired.select[0].column == "aa"
        assert len(details) == 0


class TestJoinPathCompletion:

    def test_join_path_completion(self, catalog):
        """JOIN with ON=Literal(True) + FK exists → ON clause generated."""
        ir = QueryIR(
            select=[
                ColumnRef(table="customers", column="name"),
                ColumnRef(table="orders", column="amount"),
            ],
            from_table=RelRef(table="customers"),
            joins=[
                JoinClause(
                    join_type=JoinType.INNER,
                    right=RelRef(table="orders"),
                    on=Literal(value=True),
                ),
            ],
        )
        repaired = _complete_join_paths(ir.model_copy(deep=True), catalog)
        on = repaired.joins[0].on
        assert isinstance(on, BinOp)
        assert on.op == BinOpKind.EQ
        # Should reference the FK columns
        cols = {on.left.column, on.right.column}
        assert "id" in cols
        assert "customer_id" in cols


class TestRepairCandidate:

    def test_no_repair_needed(self, catalog):
        """Valid IR → returned unchanged."""
        ir = QueryIR(
            select=[ColumnRef(table="customers", column="name")],
            from_table=RelRef(table="customers"),
            where=BinOp(
                op=BinOpKind.EQ,
                left=ColumnRef(table="customers", column="id"),
                right=Literal(value=1),
            ),
        )
        original_ir = ir.model_copy(deep=True)
        repaired = repair_candidate(ir, catalog, original_ir)
        assert repaired.select[0].column == "name"
        assert repaired.from_table.table == "customers"


class TestCandidateOutcome:

    def test_candidate_outcome_creation(self):
        """CandidateOutcome dataclass instantiation."""
        outcome = CandidateOutcome(
            candidate_id="amp_0",
            source="llm",
            category="verified_equivalent",
            repair_applied=True,
            repair_details=["case_normalized:3", "column_repaired:naem→name"],
            witness_db=None,
            cost=42.0,
        )
        assert outcome.candidate_id == "amp_0"
        assert outcome.source == "llm"
        assert outcome.category == "verified_equivalent"
        assert outcome.repair_applied is True
        assert len(outcome.repair_details) == 2
        assert outcome.cost == 42.0

    def test_candidate_outcome_defaults(self):
        """CandidateOutcome with minimal args uses defaults."""
        outcome = CandidateOutcome(
            candidate_id="rule_1",
            source="rule",
            category="non_equivalent",
        )
        assert outcome.repair_applied is False
        assert outcome.repair_details is None
        assert outcome.witness_db is None
        assert outcome.cost is None


class TestLevenshtein:

    def test_identical(self):
        assert _levenshtein("abc", "abc") == 0

    def test_single_insert(self):
        assert _levenshtein("abc", "abcd") == 1

    def test_single_delete(self):
        assert _levenshtein("abcd", "abc") == 1

    def test_single_substitute(self):
        assert _levenshtein("abc", "adc") == 1

    def test_empty(self):
        assert _levenshtein("", "abc") == 3
        assert _levenshtein("abc", "") == 3

    def test_both_empty(self):
        assert _levenshtein("", "") == 0
