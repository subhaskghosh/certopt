"""VeriEQL ``.jsonlines`` → Catalog adapter.

Loads VeriEQL benchmark entries (schema, constraints, SQL pairs) and result
files, producing :class:`~optim.schema.catalog.Catalog` instances compatible
with the rest of the optimiser pipeline.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..ir.types import SemType
from ..schema.catalog import Catalog, ColumnInfo, ForeignKey, TableInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, SemType] = {
    "INT": SemType.INT,
    "INTEGER": SemType.INT,
    "VARCHAR": SemType.STRING,
    "DATE": SemType.DATE,
    "BOOLEAN": SemType.BOOL,
    "BOOL": SemType.BOOL,
    "DECIMAL": SemType.DECIMAL,
    "NUMERIC": SemType.DECIMAL,
    "FLOAT": SemType.FLOAT,
    "DOUBLE": SemType.FLOAT,
    "REAL": SemType.FLOAT,
}


def _map_type(type_str: str | None) -> SemType:
    """Map a VeriEQL column type string to a SemType."""
    if type_str is None:
        return SemType.UNKNOWN
    t = type_str.upper().split("(")[0].strip()
    return _TYPE_MAP.get(t, SemType.UNKNOWN)


# ---------------------------------------------------------------------------
# Single-entry loader
# ---------------------------------------------------------------------------

def load_verieql_entry(entry: dict) -> tuple[Catalog, str, str]:
    """Build a :class:`Catalog` from one parsed VeriEQL JSON entry.

    Parameters
    ----------
    entry:
        A dict parsed from a single line in a ``.jsonlines`` file.  Must
        contain ``"schema"``, ``"constraint"``, and ``"pair"`` keys.

    Returns
    -------
    tuple[Catalog, str, str]
        ``(catalog, sql1, sql2)`` where *catalog* is populated from the
        schema and constraints and *sql1*/*sql2* are the SQL pair.
    """
    schema_def: dict[str, dict[str, str]] = entry["schema"]
    constraints: list[dict] = entry.get("constraint") or []
    sql1, sql2 = entry["pair"]

    # -- build tables and columns (all nullable by default) -----------------
    tables: dict[str, TableInfo] = {}
    for table_name, col_defs in schema_def.items():
        columns = [
            ColumnInfo(
                name=col_name,
                sem_type=_map_type(col_type),
                nullable=True,
                is_primary_key=False,
            )
            for col_name, col_type in col_defs.items()
        ]
        tables[table_name] = TableInfo(
            name=table_name,
            columns=columns,
            primary_keys=[],
        )

    # -- apply constraints --------------------------------------------------
    foreign_keys: list[ForeignKey] = []
    value_constraints: list[dict] = []

    for constraint in constraints:
        if "not_null" in constraint:
            ref = constraint["not_null"]["value"]
            tbl, col = ref.split("__", 1)
            table_info = tables.get(tbl)
            if table_info is not None:
                col_info = table_info.get_column(col)
                if col_info is not None:
                    col_info.nullable = False
                else:
                    logger.warning("not_null: column %s not found in table %s", col, tbl)
            else:
                logger.warning("not_null: table %s not found in schema", tbl)

        elif "primary" in constraint:
            pk_refs = constraint["primary"]
            # Collect columns per table for this constraint
            group_by_table: dict[str, list[str]] = {}
            for pk_ref in pk_refs:
                ref = pk_ref["value"]
                tbl, col = ref.split("__", 1)
                table_info = tables.get(tbl)
                if table_info is not None:
                    col_info = table_info.get_column(col)
                    if col_info is not None:
                        col_info.is_primary_key = True
                        col_info.nullable = False  # PKs are NOT NULL by definition
                    group_by_table.setdefault(tbl, []).append(col)
                else:
                    logger.warning("primary: table %s not found in schema", tbl)
            # Store as composite PK group(s) and maintain backward compat
            for tbl, cols in group_by_table.items():
                table_info = tables[tbl]
                table_info.primary_key_groups.append(cols)
                if len(cols) == 1:
                    # Single-column PK: also add to primary_keys for backward compat
                    if cols[0] not in table_info.primary_keys:
                        table_info.primary_keys.append(cols[0])

        elif "foreign" in constraint:
            fk_refs = constraint["foreign"]
            if len(fk_refs) >= 2:
                src_ref = fk_refs[0]["value"]
                dst_ref = fk_refs[1]["value"]
                src_tbl, src_col = src_ref.split("__", 1)
                dst_tbl, dst_col = dst_ref.split("__", 1)
                foreign_keys.append(ForeignKey(
                    src_table=src_tbl,
                    src_column=src_col,
                    dst_table=dst_tbl,
                    dst_column=dst_col,
                ))
                # VeriEQL FK semantics: child FK column must match a parent
                # PK value exactly (including NULL status).  Since parent PKs
                # are NOT NULL, the child FK is effectively NOT NULL too.
                table_info = tables.get(src_tbl)
                if table_info is not None:
                    col_info = table_info.get_column(src_col)
                    if col_info is not None:
                        col_info.nullable = False

        else:
            # gt, gte, lt, lte, in, between, neq, imply, inc, consec
            value_constraints.append(constraint)

    catalog = Catalog(tables=tables, foreign_keys=foreign_keys,
                      value_constraints=value_constraints)
    return catalog, sql1, sql2


# ---------------------------------------------------------------------------
# Suite loader
# ---------------------------------------------------------------------------

def load_verieql_suite(jsonlines_path: str) -> list[dict]:
    """Parse an entire VeriEQL ``.jsonlines`` file.

    Parameters
    ----------
    jsonlines_path:
        Path to the ``.jsonlines`` file.

    Returns
    -------
    list[dict]
        Each element is the parsed JSON object for one benchmark entry.
    """
    path = Path(jsonlines_path)
    entries: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSON at %s:%d", jsonlines_path, lineno)
    logger.info("Loaded %d entries from %s", len(entries), jsonlines_path)
    return entries


# ---------------------------------------------------------------------------
# Results loader
# ---------------------------------------------------------------------------

def load_verieql_results(out_path: str) -> dict[int, dict]:
    """Parse a VeriEQL ``.out`` result file.

    Each line in the ``.out`` file corresponds to the same-numbered line
    in the matching ``.jsonlines`` suite file.  We key by **line position**
    (0-based) rather than the ``index`` JSON field, because ``index`` is
    per-problem-file in multi-file suites (e.g. leetcode) and therefore
    not globally unique.

    Parameters
    ----------
    out_path:
        Path to the result ``.out`` file (one JSON object per line).

    Returns
    -------
    dict[int, dict]
        Mapping from 0-based line position to the full result dict.
    """
    path = Path(out_path)
    results: dict[int, dict] = {}
    pos = 0
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSON at %s:%d", out_path, lineno)
                pos += 1
                continue
            results[pos] = obj
            pos += 1
    logger.info("Loaded %d results from %s", len(results), out_path)
    return results


# ---------------------------------------------------------------------------
# Verdict extraction
# ---------------------------------------------------------------------------

def verieql_verdict(result: dict) -> str:
    """Extract the VeriEQL verdict from a result dict.

    VeriEQL runs with *increasing* k (bound on rows per table).  The
    ``states`` array has one entry per k value tested.  A verdict of
    ``"EQU"`` at k=n means equivalence was proved for all databases with
    ≤n rows per table — this is a valid bounded-equivalence proof even
    if a later k timed out.

    The verdict is determined by:

    1. If any entry in ``states`` is ``"NEQ"``, return ``"NEQ"`` (a
       counterexample is always definitive, regardless of ``err``).
    2. If ``err`` is set, check for known categories (``"NSE"``,
       ``"NIE"``, ``"NOT EQUIVALENT"``); otherwise return ``"ERR"``.
    3. If any entry is ``"EQU"``, return ``"EQU"`` (equivalence was
       proved at some bounded k).
    4. If the only non-None state is ``"TMO"``, return ``"TMO"``.
    5. Falls back to ``"ERR"`` if no state is found.

    Returns
    -------
    str
        One of ``"EQU"``, ``"NEQ"``, ``"TMO"``, ``"NSE"``, ``"NIE"``, ``"ERR"``.
    """
    # Check states first — NEQ in states is always definitive regardless
    # of err field (VeriEQL sometimes sets err="Symbolic reasoning: NOT
    # EQUIVALENT." as a summary alongside NEQ states).
    states = result.get("states")
    non_none = [s for s in (states or []) if s is not None]

    # NEQ at any k is definitive — a counterexample was found
    if "NEQ" in non_none:
        return "NEQ"

    err = result.get("err")
    if err is not None:
        err_upper = str(err).upper()
        if "NOT SUPPORTED" in err_upper:
            return "NSE"
        if "NOT IMPLEMENTED" in err_upper:
            return "NIE"
        if "NOT EQUIVALENT" in err_upper:
            return "NEQ"
        return "ERR"

    if not non_none:
        return "TMO"

    # EQU at any k is a valid bounded proof
    if "EQU" in non_none:
        return "EQU"

    # Only TMO states (no EQU or NEQ ever reached)
    if "TMO" in non_none:
        return "TMO"

    return "ERR"
