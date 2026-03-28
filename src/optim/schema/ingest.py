"""Schema ingestion from SQLite databases.

Reads table/column metadata, primary keys, and foreign keys from a
SQLite database and builds a Catalog object.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

from ..ir.types import SemType
from .catalog import Catalog, ColumnInfo, ForeignKey, TableInfo


# ---------------------------------------------------------------------------
# SQLite type → SemType mapping
# ---------------------------------------------------------------------------

_SQLITE_TYPE_MAP: dict[str, SemType] = {
    "INTEGER": SemType.INT,
    "INT": SemType.INT,
    "BIGINT": SemType.INT,
    "SMALLINT": SemType.INT,
    "TINYINT": SemType.INT,
    "TEXT": SemType.STRING,
    "VARCHAR": SemType.STRING,
    "CHAR": SemType.STRING,
    "REAL": SemType.FLOAT,
    "FLOAT": SemType.FLOAT,
    "DOUBLE": SemType.FLOAT,
    "NUMERIC": SemType.DECIMAL,
    "DECIMAL": SemType.DECIMAL,
    "BLOB": SemType.STRING,
    "DATE": SemType.DATE,
    "TIMESTAMP": SemType.TIMESTAMP,
    "DATETIME": SemType.TIMESTAMP,
    "BOOLEAN": SemType.BOOL,
}


def _map_sqlite_type(sqlite_type: str) -> SemType:
    """Map a SQLite type string to a SemType."""
    upper = sqlite_type.upper().strip()
    base = upper.split("(")[0].strip()
    return _SQLITE_TYPE_MAP.get(base, SemType.UNKNOWN)


# ---------------------------------------------------------------------------
# SQLite ingestion
# ---------------------------------------------------------------------------

def ingest_sqlite(db_path: str) -> Catalog:
    """Build a Catalog from a SQLite database.

    Args:
        db_path: Path to a SQLite database file.

    Returns:
        A fully populated Catalog.
    """
    conn = sqlite3.connect(db_path)
    try:
        tables: dict[str, TableInfo] = {}
        foreign_keys: list[ForeignKey] = []

        # Get all user tables
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()

        for (table_name,) in table_rows:
            # Column info: cid, name, type, notnull, dflt_value, pk
            col_rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()

            columns: list[ColumnInfo] = []
            pk_cols: list[str] = []
            for cid, name, col_type, notnull, dflt_value, pk in col_rows:
                columns.append(ColumnInfo(
                    name=name,
                    sem_type=_map_sqlite_type(col_type or ""),
                    nullable=(not notnull),
                    is_primary_key=bool(pk),
                ))
                if pk:
                    pk_cols.append(name)

            # Row count
            try:
                result = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
                row_count = result[0] if result else None
            except Exception as e:
                logger.debug("Row count query failed for %s: %s", table_name, e)
                row_count = None

            tables[table_name] = TableInfo(
                name=table_name,
                columns=columns,
                primary_keys=pk_cols,
                unique_columns=[],  # populated after index scan below
                row_count=row_count,
            )

            # UNIQUE columns (from indexes, excluding PKs)
            unique_cols: list[str] = []
            try:
                idx_rows = conn.execute(f'PRAGMA index_list("{table_name}")').fetchall()
                for _seq, idx_name, is_unique, *_ in idx_rows:
                    if is_unique:
                        idx_info = conn.execute(f'PRAGMA index_info("{idx_name}")').fetchall()
                        if len(idx_info) == 1:
                            col_name = idx_info[0][2]
                            if col_name and col_name not in pk_cols and col_name not in unique_cols:
                                unique_cols.append(col_name)
            except Exception as e:
                logger.debug("UNIQUE index scan failed for %s: %s", table_name, e)
            tables[table_name].unique_columns = unique_cols

            # Foreign keys: id, seq, table, from, to, on_update, on_delete, match
            fk_rows = conn.execute(f'PRAGMA foreign_key_list("{table_name}")').fetchall()
            for fk_id, seq, ref_table, from_col, to_col, *_ in fk_rows:
                if not to_col:
                    ref_info = tables.get(ref_table)
                    if ref_info and ref_info.primary_keys:
                        to_col = ref_info.primary_keys[0]
                    else:
                        logger.debug(
                            "FK %s.%s → %s: empty to_col and no PK found, skipping",
                            table_name, from_col, ref_table,
                        )
                        continue
                foreign_keys.append(ForeignKey(
                    src_table=table_name,
                    src_column=from_col,
                    dst_table=ref_table,
                    dst_column=to_col,
                ))

        return Catalog(tables=tables, foreign_keys=foreign_keys)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def ingest_schema(db_path: str, dialect: str = "sqlite") -> Catalog:
    """Ingest schema from a SQLite database file."""
    return ingest_sqlite(db_path)
