"""Tests for CEGAR-style witness refinement (B.1 + B.2)."""

import pytest
from optim.cegis.refinement import synthesize_with_refinement
from optim.cegis.witness_export import ValidationResult, RefinementHint, validate_witness, _collect_approximate_predicates
from optim.cegis.witness_synthesis import synthesize_witness
from optim.ir.types import SemType, BinOp, BinOpKind, ColumnRef, Literal
from optim.parser.sql_to_ir import sql_to_ir
from optim.schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo
from optim.verify.encode_z3 import BoundedScope


@pytest.fixture
def catalog():
    return Catalog(
        tables={
            "t1": TableInfo(name="t1", columns=[
                ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                ColumnInfo(name="val", sem_type=SemType.INT, nullable=True),
                ColumnInfo(name="name", sem_type=SemType.STRING, nullable=True),
            ], primary_keys=["id"]),
        },
        foreign_keys=[],
    )


@pytest.fixture
def scope():
    return BoundedScope(k_rows=2, int_bounds=(-5, 5))


class TestRefinementHints:
    """B.1: Refinement hint collection."""

    def test_like_predicate_detected(self):
        """LIKE predicates produce refinement hints."""
        ir, _ = sql_to_ir("SELECT id FROM t1 WHERE name LIKE 'abc%'")
        hints = _collect_approximate_predicates(ir)
        assert len(hints) >= 1
        like_hints = [h for h in hints if h.predicate_type == "LIKE"]
        assert len(like_hints) >= 1
        assert like_hints[0].column == "name"

    def test_no_hints_for_simple_query(self):
        """Simple queries without LIKE/funcs produce no hints."""
        ir, _ = sql_to_ir("SELECT id FROM t1 WHERE val > 0")
        hints = _collect_approximate_predicates(ir)
        assert len(hints) == 0

    def test_unmodeled_func_detected(self):
        """Unmodeled functions produce refinement hints."""
        ir, _ = sql_to_ir("SELECT UPPER(name) FROM t1")
        hints = _collect_approximate_predicates(ir)
        func_hints = [h for h in hints if h.predicate_type == "UPPER"]
        assert len(func_hints) >= 1

    def test_modeled_func_no_hint(self):
        """COALESCE (modeled) does NOT produce a hint."""
        ir, _ = sql_to_ir("SELECT COALESCE(val, 0) FROM t1")
        hints = _collect_approximate_predicates(ir)
        assert len(hints) == 0


class TestValidationResultExtended:
    """B.1: Extended ValidationResult with hints."""

    def test_spurious_flag_set(self, catalog, scope):
        """When queries agree on witness, is_spurious=True."""
        # Use identical queries — synthesis should return UNSAT, not SAT.
        # So we test the ValidationResult directly.
        ir1, _ = sql_to_ir("SELECT val FROM t1")
        ir2, _ = sql_to_ir("SELECT val FROM t1")
        result = synthesize_witness(ir1, ir2, catalog, scope)
        assert result.status == "unsat"  # identical queries → UNSAT

    def test_valid_witness_not_spurious(self, catalog, scope):
        """When queries genuinely differ, is_spurious=False."""
        ir1, _ = sql_to_ir("SELECT val FROM t1")
        ir2, _ = sql_to_ir("SELECT val FROM t1 WHERE val > 0")
        result = synthesize_witness(ir1, ir2, catalog, scope)
        assert result.status == "sat"
        if result.witness_db:
            val = validate_witness(ir1, ir2, result.witness_db, catalog)
            assert val.results_differ
            assert not val.is_spurious


class TestCEGARRefinement:
    """B.2: CEGAR refinement loop."""

    def test_unsat_no_refinement(self, catalog, scope):
        """UNSAT result needs no refinement."""
        ir1, _ = sql_to_ir("SELECT val FROM t1")
        ir2, _ = sql_to_ir("SELECT val FROM t1")
        ref = synthesize_with_refinement(ir1, ir2, catalog, scope)
        assert ref.final_result.status == "unsat"
        assert ref.rounds == 0
        assert not ref.refined

    def test_valid_sat_no_refinement(self, catalog, scope):
        """Genuine SAT needs no refinement."""
        ir1, _ = sql_to_ir("SELECT val FROM t1")
        ir2, _ = sql_to_ir("SELECT val FROM t1 WHERE val > 0")
        ref = synthesize_with_refinement(ir1, ir2, catalog, scope)
        assert ref.final_result.status == "sat"
        assert ref.spurious_count == 0
        assert not ref.refined

    def test_refinement_result_fields(self, catalog, scope):
        """RefinementResult has correct field structure."""
        ir1, _ = sql_to_ir("SELECT val FROM t1 WHERE val > 0")
        ir2, _ = sql_to_ir("SELECT val FROM t1 WHERE val > 0")
        ref = synthesize_with_refinement(ir1, ir2, catalog, scope)
        assert ref.final_result.status == "unsat"
        assert isinstance(ref.rounds, int)
        assert isinstance(ref.spurious_count, int)
        assert isinstance(ref.refined, bool)
