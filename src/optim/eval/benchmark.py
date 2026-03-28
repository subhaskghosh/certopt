"""Benchmark loading for JOB-Complex query workloads.

Provides data classes and loaders for the JOB-Complex benchmark suite,
including SQL parsing, plan-dataset matching, and query characteristic
extraction.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkQuery:
    """A single benchmark query with metadata and optional plan statistics."""

    id: str
    sql: str
    num_tables: int
    num_joins: int
    has_string_joins: bool
    has_like_predicates: bool
    pg_optimal_runtime_ms: float | None = None
    pg_default_runtime_ms: float | None = None


@dataclass
class BenchmarkSuite:
    """A named collection of benchmark queries."""

    name: str
    queries: list[BenchmarkQuery]
    metadata: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SQL characteristic extraction
# ---------------------------------------------------------------------------

_STRING_JOIN_PATTERNS = re.compile(
    r"\b(name_pcode\w*|surname_pcode\w*|imdb_index)\b", re.IGNORECASE
)

_JOIN_CONDITION_PATTERN = re.compile(
    r"\b\w+\.\w+\s*=\s*\w+\.\w+\b"
)


def _count_tables(sql: str) -> int:
    """Count table aliases in FROM clause (comma-separated) + explicit JOINs."""
    upper = sql.upper()

    # Extract FROM clause: everything between FROM and WHERE (or end)
    from_match = re.search(r"\bFROM\b(.+?)(?:\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|$)", upper, re.DOTALL)
    if not from_match:
        return 0

    from_clause = from_match.group(1)

    # Count comma-separated table refs (before any JOIN keyword)
    join_split = re.split(r"\bJOIN\b", from_clause)
    first_part = join_split[0]
    comma_tables = len([t for t in first_part.split(",") if t.strip()])

    # Count explicit JOINs
    join_count = len(join_split) - 1

    return comma_tables + join_count


def _count_joins(sql: str) -> int:
    """Count join conditions (table.col = table.col patterns) in WHERE clause."""
    upper = sql.upper()
    where_match = re.search(r"\bWHERE\b(.+?)(?:\bGROUP\b|\bORDER\b|\bLIMIT\b|$)", upper, re.DOTALL)
    if not where_match:
        return 0

    where_clause = where_match.group(1)
    # Split on AND and count table.col = table.col patterns
    conditions = re.split(r"\bAND\b", where_clause)
    join_count = 0
    for cond in conditions:
        if _JOIN_CONDITION_PATTERN.search(cond):
            join_count += 1
    return join_count


def _has_string_joins(sql: str) -> bool:
    """Check for string-based join patterns (name_pcode, surname_pcode, imdb_index)."""
    return bool(_STRING_JOIN_PATTERNS.search(sql))


def _has_like_predicates(sql: str) -> bool:
    """Check for LIKE keyword in the query."""
    return bool(re.search(r"\bLIKE\b", sql, re.IGNORECASE))


def _extract_characteristics(sql: str) -> dict[str, object]:
    """Extract all query characteristics from SQL text."""
    return {
        "num_tables": _count_tables(sql),
        "num_joins": _count_joins(sql),
        "has_string_joins": _has_string_joins(sql),
        "has_like_predicates": _has_like_predicates(sql),
    }


# ---------------------------------------------------------------------------
# SQL normalisation helpers
# ---------------------------------------------------------------------------


def _normalize_sql(sql: str) -> str:
    """Normalize whitespace and strip for comparison."""
    return " ".join(sql.split()).strip()


# ---------------------------------------------------------------------------
# Plan-dataset helpers
# ---------------------------------------------------------------------------


def _runtime_from_entry(entry: dict) -> float | None:
    """Extract runtime in ms from a single query_list entry.

    Uses Execution Time from the first analyze_plan, falling back to
    Plan -> Actual Total Time.
    """
    plans = entry.get("analyze_plans") or []
    if not plans:
        return None

    plan = plans[0]
    exec_time = plan.get("Execution Time")
    if exec_time is not None:
        return float(exec_time)

    inner = plan.get("Plan") or {}
    actual = inner.get("Actual Total Time")
    if actual is not None:
        return float(actual)

    return None


def _find_optimal_and_default(
    entries: list[dict],
) -> tuple[float | None, float | None]:
    """Find optimal runtime (min across non-timeout plans) and PG default runtime.

    Returns (optimal_ms, default_ms).
    """
    optimal: float | None = None
    default: float | None = None

    for entry in entries:
        if entry.get("timeout", False):
            continue

        rt = _runtime_from_entry(entry)
        if rt is None:
            continue

        # Track overall minimum
        if optimal is None or rt < optimal:
            optimal = rt

        # PG default = empty hint
        hint = entry.get("hint", "")
        if hint == "" and default is None:
            default = rt

    # If no empty-hint entry found, use the first valid non-timeout entry
    if default is None:
        for entry in entries:
            if not entry.get("timeout", False):
                rt = _runtime_from_entry(entry)
                if rt is not None:
                    default = rt
                    break

    return optimal, default


# ---------------------------------------------------------------------------
# Public loaders
# ---------------------------------------------------------------------------


def load_job_complex(data_dir: str) -> BenchmarkSuite:
    """Load the JOB-Complex benchmark with plan-selection data.

    Args:
        data_dir: Path to the data directory containing ``JOB-Complex/``
            sub-directory and ``JOB-Complex.json``.

    Returns:
        A :class:`BenchmarkSuite` with 30 queries (IDs ``JOB-C01`` to
        ``JOB-C30``) enriched with optimal/default runtimes from the
        plan dataset.
    """
    sql_file = os.path.join(data_dir, "JOB-Complex", "JOB-Complex", "JOB-Complex.sql")
    json_file = os.path.join(data_dir, "JOB-Complex", "JOB-Complex.json")

    # --- Read raw SQL queries ---
    with open(sql_file, "r") as f:
        raw_lines = [line.strip() for line in f if line.strip()]

    logger.info("Loaded %d SQL queries from %s", len(raw_lines), sql_file)

    # --- Read plan dataset ---
    with open(json_file, "r") as f:
        plan_data = json.load(f)

    query_list: list[dict] = plan_data.get("query_list", [])
    logger.info("Loaded %d plan entries from %s", len(query_list), json_file)

    # --- Build index: normalized SQL -> list of entries ---
    sql_index: dict[str, list[dict]] = {}
    prefix_index: dict[str, list[dict]] = {}
    for entry in query_list:
        norm = _normalize_sql(entry["sql"])
        sql_index.setdefault(norm, []).append(entry)
        prefix = norm[:80]
        prefix_index.setdefault(prefix, []).append(entry)

    # --- Match and build queries ---
    queries: list[BenchmarkQuery] = []
    for i, raw_sql in enumerate(raw_lines):
        qid = f"JOB-C{i + 1:02d}"
        norm_sql = _normalize_sql(raw_sql)
        chars = _extract_characteristics(raw_sql)

        # Try exact match first, then prefix match
        matched_entries = sql_index.get(norm_sql)
        if not matched_entries:
            prefix = norm_sql[:80]
            matched_entries = prefix_index.get(prefix)

        optimal_ms: float | None = None
        default_ms: float | None = None
        if matched_entries:
            optimal_ms, default_ms = _find_optimal_and_default(matched_entries)
        else:
            logger.warning("No plan data match for %s", qid)

        queries.append(
            BenchmarkQuery(
                id=qid,
                sql=raw_sql,
                num_tables=chars["num_tables"],
                num_joins=chars["num_joins"],
                has_string_joins=chars["has_string_joins"],
                has_like_predicates=chars["has_like_predicates"],
                pg_optimal_runtime_ms=optimal_ms,
                pg_default_runtime_ms=default_ms,
            )
        )

    metadata: dict[str, object] = {
        "sql_file": sql_file,
        "json_file": json_file,
        "total_plan_entries": len(query_list),
        "unique_sqls_in_json": len(sql_index),
    }

    return BenchmarkSuite(name="JOB-Complex", queries=queries, metadata=metadata)


def load_job_complex_queries(sql_file: str) -> list[BenchmarkQuery]:
    """Load JOB-Complex queries from the SQL file only (no plan data).

    This is a lighter alternative to :func:`load_job_complex` for use
    when the JSON plan files are not available.

    Args:
        sql_file: Path to ``JOB-Complex.sql``.

    Returns:
        List of :class:`BenchmarkQuery` with IDs ``JOB-C01`` to ``JOB-C30``
        but without runtime data.
    """
    with open(sql_file, "r") as f:
        raw_lines = [line.strip() for line in f if line.strip()]

    logger.info("Loaded %d SQL queries from %s", len(raw_lines), sql_file)

    queries: list[BenchmarkQuery] = []
    for i, raw_sql in enumerate(raw_lines):
        chars = _extract_characteristics(raw_sql)
        queries.append(
            BenchmarkQuery(
                id=f"JOB-C{i + 1:02d}",
                sql=raw_sql,
                num_tables=chars["num_tables"],
                num_joins=chars["num_joins"],
                has_string_joins=chars["has_string_joins"],
                has_like_predicates=chars["has_like_predicates"],
            )
        )

    return queries
