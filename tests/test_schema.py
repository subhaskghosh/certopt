"""Tests for schema catalog and SQLite ingestion."""

from optim.schema.catalog import Catalog
from optim.schema.ingest import ingest_sqlite
from optim.schema.fk_graph import build_fk_graph, find_join_paths, find_join_tree
from optim.ir.types import SemType


def test_catalog_lookup(sample_catalog: Catalog):
    """Basic catalog lookups work."""
    assert "customers" in sample_catalog.list_tables()
    assert sample_catalog.get_table("customers") is not None
    assert sample_catalog.get_column("customers", "name") is not None
    assert sample_catalog.get_column("customers", "nonexistent") is None


def test_catalog_fk_lookup(sample_catalog: Catalog):
    fks = sample_catalog.get_foreign_keys_from("orders")
    assert len(fks) == 1
    assert fks[0].dst_table == "customers"

    fks_to = sample_catalog.get_foreign_keys_to("customers")
    assert len(fks_to) == 1


def test_ingest_sqlite(sqlite_db_path):
    """Ingesting from sqlite3 produces a valid catalog."""
    catalog = ingest_sqlite(sqlite_db_path)
    assert len(catalog.list_tables()) == 4
    assert catalog.get_table("customers") is not None

    col = catalog.get_column("customers", "name")
    assert col is not None
    assert col.sem_type == SemType.STRING

    id_col = catalog.get_column("customers", "id")
    assert id_col is not None
    assert id_col.sem_type == SemType.INT


def test_fk_graph_paths(sample_catalog: Catalog):
    """FK graph finds join paths between tables."""
    g = build_fk_graph(sample_catalog)
    paths = find_join_paths(g, "customers", "products")
    assert len(paths) > 0
    # Must go through orders and order_items
    for path in paths:
        assert path[0] == "customers"
        assert path[-1] == "products"


def test_fk_graph_join_tree(sample_catalog: Catalog):
    """FK graph finds a connecting tree for multiple tables."""
    g = build_fk_graph(sample_catalog)
    trees = find_join_tree(g, ["customers", "products"])
    assert len(trees) > 0
    for tree in trees:
        assert "customers" in tree
        assert "products" in tree
