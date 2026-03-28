"""Tests for equivalence clustering (optim.cegis.equivalence)."""

from optim.cegis.equivalence import Candidate, PairwiseCheck, cluster_candidates
from optim.ir.types import ColumnRef, QueryIR, RelRef, SemType
from optim.parser.sql_to_ir import sql_to_ir
from optim.schema.catalog import Catalog, ColumnInfo, TableInfo
from optim.verify.encode_z3 import BoundedScope


def _make_catalog() -> Catalog:
    """Single-table catalog: t(id INT PK, name TEXT)."""
    return Catalog(
        tables={
            "t": TableInfo(
                name="t",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="name", sem_type=SemType.STRING, nullable=True),
                ],
                primary_keys=["id"],
            ),
        },
    )


def _parse(sql: str) -> QueryIR:
    """Parse SQL or fail."""
    ir, err = sql_to_ir(sql)
    assert ir is not None, f"Failed to parse: {err}"
    return ir


# ---------------------------------------------------------------------------
# Candidate creation
# ---------------------------------------------------------------------------


def test_candidate_creation():
    """Candidate stores all fields correctly."""
    ir = _parse("SELECT id FROM t")
    c = Candidate(id="c1", ir=ir, confidence=0.9, source="llm", metadata={"tag": "x"})
    assert c.id == "c1"
    assert c.confidence == 0.9
    assert c.source == "llm"
    assert c.metadata == {"tag": "x"}
    assert isinstance(c.ir, QueryIR)


def test_candidate_defaults():
    """Candidate defaults are applied when fields are omitted."""
    ir = _parse("SELECT id FROM t")
    c = Candidate(id="c2", ir=ir)
    assert c.confidence == 0.5
    assert c.source == "unknown"
    assert c.metadata == {}


# ---------------------------------------------------------------------------
# PairwiseCheck creation
# ---------------------------------------------------------------------------


def test_pairwise_check_creation():
    """PairwiseCheck stores ids, status, and optional fields."""
    pc = PairwiseCheck(id_a="a", id_b="b", status="unsat", solver_time_ms=42.0)
    assert pc.id_a == "a"
    assert pc.id_b == "b"
    assert pc.status == "unsat"
    assert pc.witness_db is None
    assert pc.validation is None
    assert pc.solver_time_ms == 42.0


# ---------------------------------------------------------------------------
# cluster_candidates — empty list
# ---------------------------------------------------------------------------


def test_cluster_empty():
    """Clustering an empty list returns no classes."""
    catalog = _make_catalog()
    result = cluster_candidates([], catalog)
    assert result.n_classes == 0
    assert result.classes == []


# ---------------------------------------------------------------------------
# cluster_candidates — equivalent queries → same cluster
# ---------------------------------------------------------------------------


def test_cluster_equivalent_queries():
    """Two semantically equivalent queries should end up in the same cluster."""
    catalog = _make_catalog()
    scope = BoundedScope(k_rows=2, solver_timeout_ms=5_000)

    ir_a = _parse("SELECT id FROM t")
    ir_b = _parse("SELECT id FROM t WHERE 1 = 1")

    candidates = [
        Candidate(id="eq1", ir=ir_a, confidence=0.8),
        Candidate(id="eq2", ir=ir_b, confidence=0.7),
    ]

    result = cluster_candidates(candidates, catalog, scope, validate=False)
    assert result.n_classes == 1
    assert set(result.classes[0]) == {"eq1", "eq2"}


# ---------------------------------------------------------------------------
# cluster_candidates — non-equivalent queries → different clusters
# ---------------------------------------------------------------------------


def test_cluster_non_equivalent_queries():
    """SELECT name FROM t vs SELECT DISTINCT name FROM t are non-equivalent (name is not a key)."""
    catalog = _make_catalog()
    scope = BoundedScope(k_rows=2, solver_timeout_ms=5_000)

    ir_a = _parse("SELECT name FROM t")
    ir_b = _parse("SELECT DISTINCT name FROM t")

    candidates = [
        Candidate(id="neq1", ir=ir_a, confidence=0.8),
        Candidate(id="neq2", ir=ir_b, confidence=0.7),
    ]

    result = cluster_candidates(candidates, catalog, scope, validate=False)
    # They should be in different clusters (DISTINCT can collapse rows on non-key column)
    assert result.n_classes == 2
    all_ids = {cid for cls in result.classes for cid in cls}
    assert all_ids == {"neq1", "neq2"}


# ---------------------------------------------------------------------------
# cluster_candidates — single candidate
# ---------------------------------------------------------------------------


def test_cluster_single_candidate():
    """A single candidate produces one cluster with that candidate."""
    catalog = _make_catalog()
    ir = _parse("SELECT id FROM t")
    candidates = [Candidate(id="only", ir=ir)]
    result = cluster_candidates(candidates, catalog)
    assert result.n_classes == 1
    assert result.classes[0] == ["only"]
    assert result.representatives[0] == "only"
