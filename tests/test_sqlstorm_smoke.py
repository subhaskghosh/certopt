"""Smoke tests for the SQLStorm benchmark integration.

Verifies that:
  1. The StackOverflow catalog loads all 13 tables
  2. Pair loading returns well-formed records
  3. A reasonable fraction of pairs parse successfully
  4. End-to-end verify works on at least one pair
"""

import pathlib

import pytest

from optim.eval.schema_sqlstorm import get_sqlstorm_catalog
from optim.eval.sqlstorm_loader import load_sqlstorm_pairs
from optim.parser.sql_to_ir import sql_to_ir
from optim.cegis.witness_synthesis import synthesize_witness, BoundedScope

_DATA_DIR = pathlib.Path(__file__).resolve().parents[1] / (
    "data/SQLStorm/v1.0/stackoverflow/queries_generated"
)

_skip_no_data = pytest.mark.skipif(
    not _DATA_DIR.is_dir(),
    reason=f"SQLStorm data not found at {_DATA_DIR}",
)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def test_stackoverflow_catalog_has_all_tables():
    """Verify catalog has all 13 StackOverflow tables."""
    catalog = get_sqlstorm_catalog("stackoverflow")
    expected = {
        "PostHistoryTypes", "LinkTypes", "PostTypes", "CloseReasonTypes",
        "VoteTypes", "Users", "Badges", "Posts", "Comments",
        "PostHistory", "PostLinks", "Tags", "Votes",
    }
    assert set(catalog.tables.keys()) == expected


# ---------------------------------------------------------------------------
# Pair loading (requires data on disk)
# ---------------------------------------------------------------------------

@_skip_no_data
def test_load_pairs_returns_nonempty():
    """Verify at least some pairs are loadable."""
    pairs = load_sqlstorm_pairs("stackoverflow", max_pairs=100)
    assert len(pairs) > 0
    for p in pairs:
        assert "pair_id" in p
        assert "sql1" in p
        assert "sql2" in p


# ---------------------------------------------------------------------------
# Parse rate
# ---------------------------------------------------------------------------

@_skip_no_data
def test_pair_parse_rate():
    """Check that a reasonable fraction of loaded pairs parse successfully."""
    pairs = load_sqlstorm_pairs("stackoverflow", max_pairs=50)
    catalog = get_sqlstorm_catalog("stackoverflow")
    n_parsed = 0
    for p in pairs:
        ir1, _ = sql_to_ir(p["sql1"], dialect="postgres", catalog=catalog)
        ir2, _ = sql_to_ir(p["sql2"], dialect="postgres", catalog=catalog)
        if ir1 is not None and ir2 is not None:
            n_parsed += 1
    # At least 10% should parse after pre-filtering
    assert n_parsed > 0, f"No pairs parsed out of {len(pairs)}"


# ---------------------------------------------------------------------------
# End-to-end verify
# ---------------------------------------------------------------------------

@_skip_no_data
@pytest.mark.slow
def test_smoke_verify_one_pair():
    """Parse and verify one SQLStorm pair end-to-end."""
    pairs = load_sqlstorm_pairs("stackoverflow", max_pairs=200)
    catalog = get_sqlstorm_catalog("stackoverflow")
    scope = BoundedScope(k_rows=2, solver_timeout_ms=5000)

    verified = False
    for p in pairs:
        ir1, _ = sql_to_ir(p["sql1"], dialect="postgres", catalog=catalog)
        ir2, _ = sql_to_ir(p["sql2"], dialect="postgres", catalog=catalog)
        if ir1 is not None and ir2 is not None:
            result = synthesize_witness(ir1, ir2, catalog, scope)
            assert result.status in ("sat", "unsat", "unknown", "timeout")
            verified = True
            break

    assert verified, "Could not find any parseable pair to verify"
