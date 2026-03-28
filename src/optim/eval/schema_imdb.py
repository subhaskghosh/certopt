"""IMDB schema ingestion for JOB-Complex evaluation.

Provides two approaches to build a Catalog for the IMDB database:
1. Dynamic ingestion from a DuckDB file (``ingest_imdb_duckdb``).
2. Hard-coded manual schema (``build_imdb_catalog_manual``).

``get_imdb_catalog`` is the recommended entry-point – it tries DuckDB first
and falls back to the manual catalog.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..ir.types import SemType
from ..schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo

logger = logging.getLogger(__name__)

_DEFAULT_DUCKDB_PATH = os.path.join("data", "JOB-Complex", "imdb.duckdb")


# ---------------------------------------------------------------------------
# DuckDB type → SemType mapping
# ---------------------------------------------------------------------------

def _map_duckdb_type(type_str: str) -> SemType:
    """Map a DuckDB column type string to a SemType."""
    t = type_str.upper().split("(")[0].strip()
    mapping: dict[str, SemType] = {
        "INTEGER": SemType.INT,
        "INT": SemType.INT,
        "BIGINT": SemType.INT,
        "SMALLINT": SemType.INT,
        "TINYINT": SemType.INT,
        "HUGEINT": SemType.INT,
        "INT4": SemType.INT,
        "INT8": SemType.INT,
        "INT2": SemType.INT,
        "FLOAT": SemType.FLOAT,
        "DOUBLE": SemType.FLOAT,
        "REAL": SemType.FLOAT,
        "FLOAT4": SemType.FLOAT,
        "FLOAT8": SemType.FLOAT,
        "DECIMAL": SemType.DECIMAL,
        "NUMERIC": SemType.DECIMAL,
        "BOOLEAN": SemType.BOOL,
        "BOOL": SemType.BOOL,
        "VARCHAR": SemType.STRING,
        "TEXT": SemType.STRING,
        "STRING": SemType.STRING,
        "CHAR": SemType.STRING,
        "BPCHAR": SemType.STRING,
        "DATE": SemType.DATE,
        "TIMESTAMP": SemType.TIMESTAMP,
        "TIMESTAMP WITH TIME ZONE": SemType.TIMESTAMP,
        "TIMESTAMPTZ": SemType.TIMESTAMP,
        "DATETIME": SemType.TIMESTAMP,
    }
    return mapping.get(t, SemType.UNKNOWN)


# ---------------------------------------------------------------------------
# Approach 1 – dynamic DuckDB ingestion
# ---------------------------------------------------------------------------

def ingest_imdb_duckdb(db_path: str) -> Catalog:
    """Read the IMDB schema from a DuckDB database file.

    Parameters
    ----------
    db_path:
        Path to the ``imdb.duckdb`` file.

    Returns
    -------
    Catalog
        A catalog populated with table/column metadata and foreign keys.

    Raises
    ------
    ImportError
        If the ``duckdb`` package is not installed.
    FileNotFoundError
        If *db_path* does not exist.
    """
    import duckdb  # noqa: WPS433 – optional dependency

    db_path_resolved = str(Path(db_path).resolve())
    if not Path(db_path_resolved).exists():
        raise FileNotFoundError(f"DuckDB file not found: {db_path_resolved}")

    con = duckdb.connect(db_path_resolved, read_only=True)
    try:
        table_rows = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
        table_names = [row[0] for row in table_rows]

        tables: dict[str, TableInfo] = {}
        for tname in table_names:
            col_rows = con.execute(f"PRAGMA table_info('{tname}')").fetchall()
            columns: list[ColumnInfo] = []
            primary_keys: list[str] = []
            for row in col_rows:
                # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
                col_name = row[1]
                col_type = row[2]
                notnull = bool(row[3])
                is_pk = bool(row[5])
                columns.append(ColumnInfo(
                    name=col_name,
                    sem_type=_map_duckdb_type(col_type),
                    nullable=not notnull,
                    is_primary_key=is_pk,
                ))
                if is_pk:
                    primary_keys.append(col_name)

            tables[tname] = TableInfo(
                name=tname,
                columns=columns,
                primary_keys=primary_keys,
            )

        foreign_keys = _build_imdb_foreign_keys()
    finally:
        con.close()

    return Catalog(tables=tables, foreign_keys=foreign_keys)


# ---------------------------------------------------------------------------
# Approach 2 – hard-coded manual schema
# ---------------------------------------------------------------------------

def _col(
    name: str,
    sem_type: SemType = SemType.STRING,
    nullable: bool = True,
    is_pk: bool = False,
) -> ColumnInfo:
    """Shorthand for building a ColumnInfo."""
    return ColumnInfo(
        name=name,
        sem_type=sem_type,
        nullable=nullable,
        is_primary_key=is_pk,
    )


def _pk(name: str = "id") -> ColumnInfo:
    return _col(name, SemType.INT, nullable=False, is_pk=True)


def _int_nn(name: str) -> ColumnInfo:
    return _col(name, SemType.INT, nullable=False)


def _int(name: str) -> ColumnInfo:
    return _col(name, SemType.INT, nullable=True)


def _str(name: str) -> ColumnInfo:
    return _col(name, SemType.STRING, nullable=True)


def _str_nn(name: str) -> ColumnInfo:
    return _col(name, SemType.STRING, nullable=False)


def build_imdb_catalog_manual() -> Catalog:
    """Build the IMDB catalog from hard-coded schema definitions.

    Covers all 21 tables in the IMDB database used by JOB / JOB-Complex
    queries, with full column definitions matching the Postgres DDL.
    """
    tables: dict[str, TableInfo] = {}

    def _add(name: str, cols: list[ColumnInfo]) -> None:
        pks = [c.name for c in cols if c.is_primary_key]
        tables[name] = TableInfo(name=name, columns=cols, primary_keys=pks)

    # --- aka_name ---
    _add("aka_name", [
        _pk(), _int_nn("person_id"), _str("name"), _str("imdb_index"),
        _str("name_pcode_cf"), _str("name_pcode_nf"), _str("surname_pcode"),
        _str("md5sum"),
    ])

    # --- aka_title ---
    _add("aka_title", [
        _pk(), _int_nn("movie_id"), _str("title"), _str("imdb_index"),
        _int_nn("kind_id"), _int("production_year"), _str("phonetic_code"),
        _int("episode_of_id"), _int("season_nr"), _int("episode_nr"),
        _str("note"), _str("md5sum"),
    ])

    # --- cast_info ---
    _add("cast_info", [
        _pk(), _int_nn("person_id"), _int_nn("movie_id"),
        _int("person_role_id"), _str("note"), _int("nr_order"),
        _int_nn("role_id"),
    ])

    # --- char_name ---
    _add("char_name", [
        _pk(), _str_nn("name"), _str("imdb_index"), _int("imdb_id"),
        _str("name_pcode_nf"), _str("surname_pcode"), _str("md5sum"),
    ])

    # --- comp_cast_type ---
    _add("comp_cast_type", [
        _pk(), _str_nn("kind"),
    ])

    # --- company_name ---
    _add("company_name", [
        _pk(), _str_nn("name"), _str("country_code"), _int("imdb_id"),
        _str("name_pcode_nf"), _str("name_pcode_sf"), _str("md5sum"),
    ])

    # --- company_type ---
    _add("company_type", [
        _pk(), _str("kind"),
    ])

    # --- complete_cast ---
    _add("complete_cast", [
        _pk(), _int("movie_id"), _int_nn("subject_id"), _int_nn("status_id"),
    ])

    # --- info_type ---
    _add("info_type", [
        _pk(), _str_nn("info"),
    ])

    # --- keyword ---
    _add("keyword", [
        _pk(), _str_nn("keyword"), _str("phonetic_code"),
    ])

    # --- kind_type ---
    _add("kind_type", [
        _pk(), _str("kind"),
    ])

    # --- link_type ---
    _add("link_type", [
        _pk(), _str_nn("link"),
    ])

    # --- movie_companies ---
    _add("movie_companies", [
        _pk(), _int_nn("movie_id"), _int_nn("company_id"),
        _int_nn("company_type_id"), _str("note"),
    ])

    # --- movie_info ---
    _add("movie_info", [
        _pk(), _int_nn("movie_id"), _int_nn("info_type_id"),
        _str_nn("info"), _str("note"),
    ])

    # --- movie_info_idx ---
    _add("movie_info_idx", [
        _pk(), _int_nn("movie_id"), _int_nn("info_type_id"),
        _str_nn("info"), _str("note"),
    ])

    # --- movie_keyword ---
    _add("movie_keyword", [
        _pk(), _int_nn("movie_id"), _int_nn("keyword_id"),
    ])

    # --- movie_link ---
    _add("movie_link", [
        _pk(), _int_nn("movie_id"), _int_nn("linked_movie_id"),
        _int_nn("link_type_id"),
    ])

    # --- name ---
    _add("name", [
        _pk(), _str_nn("name"), _str("imdb_index"), _int("imdb_id"),
        _str("gender"), _str("name_pcode_cf"), _str("name_pcode_nf"),
        _str("surname_pcode"), _str("md5sum"),
    ])

    # --- person_info ---
    _add("person_info", [
        _pk(), _int_nn("person_id"), _int_nn("info_type_id"),
        _str_nn("info"), _str("note"),
    ])

    # --- role_type ---
    _add("role_type", [
        _pk(), _str_nn("role"),
    ])

    # --- title ---
    _add("title", [
        _pk(), _str_nn("title"), _str("imdb_index"), _int_nn("kind_id"),
        _int("production_year"), _int("imdb_id"), _str("phonetic_code"),
        _int("episode_of_id"), _int("season_nr"), _int("episode_nr"),
        _str("series_years"), _str("md5sum"),
    ])

    foreign_keys = _build_imdb_foreign_keys()

    return Catalog(tables=tables, foreign_keys=foreign_keys)


# ---------------------------------------------------------------------------
# Shared FK definitions
# ---------------------------------------------------------------------------

def _build_imdb_foreign_keys() -> list[ForeignKey]:
    """Return the standard IMDB foreign-key relationships.

    Derived from the ``schema.json`` shipped with JOB-Complex.
    """
    fk = ForeignKey
    return [
        # --- to title.id ---
        fk(src_table="cast_info", src_column="movie_id", dst_table="title", dst_column="id"),
        fk(src_table="movie_companies", src_column="movie_id", dst_table="title", dst_column="id"),
        fk(src_table="movie_info", src_column="movie_id", dst_table="title", dst_column="id"),
        fk(src_table="movie_info_idx", src_column="movie_id", dst_table="title", dst_column="id"),
        fk(src_table="movie_keyword", src_column="movie_id", dst_table="title", dst_column="id"),
        fk(src_table="movie_link", src_column="movie_id", dst_table="title", dst_column="id"),
        fk(src_table="aka_title", src_column="movie_id", dst_table="title", dst_column="id"),
        fk(src_table="complete_cast", src_column="movie_id", dst_table="title", dst_column="id"),
        # --- to name.id ---
        fk(src_table="aka_name", src_column="person_id", dst_table="name", dst_column="id"),
        fk(src_table="cast_info", src_column="person_id", dst_table="aka_name", dst_column="id"),
        fk(src_table="person_info", src_column="person_id", dst_table="name", dst_column="id"),
        # --- to char_name.id ---
        fk(src_table="cast_info", src_column="person_role_id", dst_table="char_name", dst_column="id"),
        # --- to company_name.id ---
        fk(src_table="movie_companies", src_column="company_id", dst_table="company_name", dst_column="id"),
        # --- to company_type.id ---
        fk(src_table="movie_companies", src_column="company_type_id", dst_table="company_type", dst_column="id"),
        # --- to info_type.id ---
        fk(src_table="movie_info", src_column="info_type_id", dst_table="info_type", dst_column="id"),
        fk(src_table="movie_info_idx", src_column="info_type_id", dst_table="info_type", dst_column="id"),
        # --- to keyword.id ---
        fk(src_table="movie_keyword", src_column="keyword_id", dst_table="keyword", dst_column="id"),
        # --- to kind_type.id ---
        fk(src_table="title", src_column="kind_id", dst_table="kind_type", dst_column="id"),
        # --- to link_type.id ---
        fk(src_table="movie_link", src_column="link_type_id", dst_table="link_type", dst_column="id"),
        # --- to role_type.id ---
        fk(src_table="cast_info", src_column="role_id", dst_table="role_type", dst_column="id"),
        # --- to comp_cast_type.id ---
        fk(src_table="complete_cast", src_column="subject_id", dst_table="comp_cast_type", dst_column="id"),
        # --- movie_id cross-table joins (shared FK column) ---
        fk(src_table="movie_keyword", src_column="movie_id", dst_table="cast_info", dst_column="movie_id"),
        fk(src_table="movie_keyword", src_column="movie_id", dst_table="movie_info", dst_column="movie_id"),
        fk(src_table="movie_keyword", src_column="movie_id", dst_table="movie_info_idx", dst_column="movie_id"),
        fk(src_table="movie_keyword", src_column="movie_id", dst_table="movie_companies", dst_column="movie_id"),
        fk(src_table="cast_info", src_column="movie_id", dst_table="movie_info", dst_column="movie_id"),
        fk(src_table="cast_info", src_column="movie_id", dst_table="movie_info_idx", dst_column="movie_id"),
        fk(src_table="cast_info", src_column="movie_id", dst_table="movie_companies", dst_column="movie_id"),
        fk(src_table="movie_info", src_column="movie_id", dst_table="movie_info_idx", dst_column="movie_id"),
        fk(src_table="movie_info", src_column="movie_id", dst_table="movie_companies", dst_column="movie_id"),
        fk(src_table="movie_info_idx", src_column="movie_id", dst_table="movie_companies", dst_column="movie_id"),
    ]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def get_imdb_catalog(data_dir: str | None = None) -> Catalog:
    """Return an IMDB Catalog, preferring dynamic DuckDB ingestion.

    Parameters
    ----------
    data_dir:
        Directory containing ``imdb.duckdb``.  When *None*, defaults to
        ``data/JOB-Complex`` relative to the working directory.

    Returns
    -------
    Catalog
    """
    if data_dir is not None:
        db_path = os.path.join(data_dir, "imdb.duckdb")
    else:
        db_path = _DEFAULT_DUCKDB_PATH

    try:
        catalog = ingest_imdb_duckdb(db_path)
        logger.info("IMDB catalog loaded from DuckDB: %s", db_path)
        return catalog
    except ImportError:
        logger.warning(
            "duckdb package not installed; falling back to manual IMDB schema"
        )
    except FileNotFoundError:
        logger.warning(
            "DuckDB file not found at %s; falling back to manual IMDB schema",
            db_path,
        )
    except Exception:
        logger.warning(
            "Failed to read DuckDB file at %s; falling back to manual IMDB schema",
            db_path,
            exc_info=True,
        )

    catalog = build_imdb_catalog_manual()
    logger.info("IMDB catalog built from manual schema definitions")
    return catalog
