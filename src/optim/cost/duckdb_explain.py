"""DuckDB EXPLAIN cost model.

Similar to PostgreSQL EXPLAIN but uses DuckDB's EXPLAIN output format.
Falls back gracefully when duckdb is not available.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..ir.render_sql import render
from ..ir.types import QueryIR
from ..schema.catalog import Catalog
from .estimator import CostEstimate

logger = logging.getLogger(__name__)

# Scoring weights for DuckDB physical operators
_OP_COSTS: dict[str, float] = {
    "SEQ_SCAN": 100.0,
    "INDEX_SCAN": 10.0,
    "HASH_JOIN": 15.0,
    "NESTED_LOOP": 80.0,
    "FILTER": 5.0,
}


class DuckDBExplainCostEstimator:
    """Cost model using DuckDB EXPLAIN output.

    Scoring:
      SEQ_SCAN     = 100  (full sequential scan)
      INDEX_SCAN   = 10   (index-based scan)
      HASH_JOIN    = 15   (hash join)
      NESTED_LOOP  = 80   (nested loop join)
      FILTER       = 5    (filter operator)
      Other        = 5    (default)
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or ":memory:"
        self._duckdb = None  # lazy import

    def _get_duckdb(self):
        """Lazily import duckdb module."""
        if self._duckdb is None:
            try:
                import duckdb
                self._duckdb = duckdb
            except ImportError:
                raise ImportError(
                    "duckdb is not installed. Install it with: pip install duckdb"
                )
        return self._duckdb

    def estimate(self, ir: QueryIR, catalog: Catalog) -> CostEstimate:
        """Render IR to SQL, run EXPLAIN in DuckDB, parse and score."""
        try:
            duckdb = self._get_duckdb()
        except ImportError as e:
            logger.warning("DuckDB not available: %s", e)
            return CostEstimate(
                total_cost=float("inf"),
                source="duckdb_explain_unavailable",
            )

        sql = render(ir, dialect="duckdb")
        breakdown: dict[str, float] = {}

        try:
            conn = duckdb.connect(self.db_path)
            # Create tables from catalog so EXPLAIN can resolve them
            for table_name, table_info in catalog.tables.items():
                col_defs = []
                for col in table_info.columns:
                    col_type = _sem_type_to_duckdb(col.sem_type.value)
                    col_defs.append(f'"{col.name}" {col_type}')
                if col_defs:
                    ddl = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(col_defs)})'
                    try:
                        conn.execute(ddl)
                    except Exception:
                        pass

            result = conn.execute(f"EXPLAIN {sql}").fetchall()
            conn.close()
        except Exception as e:
            logger.warning("DuckDB EXPLAIN failed: %s", e)
            return CostEstimate(
                total_cost=float("inf"),
                source="duckdb_explain_error",
            )

        scan_cost = 0.0
        join_cost = 0.0
        filter_cost = 0.0
        other_cost = 0.0

        for row in result:
            line = str(row[-1]) if row else ""
            line_upper = line.upper()

            if "SEQ_SCAN" in line_upper or "TABLE_SCAN" in line_upper:
                scan_cost += _OP_COSTS["SEQ_SCAN"]
            elif "INDEX_SCAN" in line_upper:
                scan_cost += _OP_COSTS["INDEX_SCAN"]
            elif "NESTED_LOOP" in line_upper:
                join_cost += _OP_COSTS["NESTED_LOOP"]
            elif "HASH_JOIN" in line_upper:
                join_cost += _OP_COSTS["HASH_JOIN"]
            elif "FILTER" in line_upper:
                filter_cost += _OP_COSTS["FILTER"]
            else:
                other_cost += 5.0

        breakdown["scan"] = scan_cost
        breakdown["join"] = join_cost
        breakdown["filter"] = filter_cost
        breakdown["other"] = other_cost

        return CostEstimate(
            total_cost=sum(breakdown.values()),
            breakdown=breakdown,
            source="duckdb_explain",
        )


def _sem_type_to_duckdb(sem_type: str) -> str:
    """Map SemType values to DuckDB column types."""
    mapping = {
        "INT": "INTEGER",
        "FLOAT": "DOUBLE",
        "DECIMAL": "DECIMAL",
        "BOOL": "BOOLEAN",
        "STRING": "VARCHAR",
        "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP",
        "UNKNOWN": "VARCHAR",
    }
    return mapping.get(sem_type.upper(), "VARCHAR")
