"""Materialize witness DB in sqlite3 and validate query disagreement.

After SMT synthesis finds a witness, this module:
  1. Creates sqlite3 tables from the witness data
  2. Executes both candidate SQLs on the witness
  3. Confirms the results actually differ (sanity check)
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

from ..ir.render_sql import quote_ident, render
from ..ir.types import QueryIR
from ..schema.catalog import Catalog


@dataclass
class RefinementHint:
    """Actionable hint for re-synthesis when a witness is spurious."""
    table: str
    column: str
    predicate_type: str  # "LIKE", "SUBSTR", "UPPER", "LOWER", "func"
    pattern: Optional[str] = None  # for LIKE: the pattern string


@dataclass
class ValidationResult:
    """Result of executing both queries on the witness DB."""
    q1_result: list[tuple]
    q2_result: list[tuple]
    results_differ: bool
    q1_sql: str
    q2_sql: str
    error: Optional[str] = None
    refinement_hints: list[RefinementHint] = field(default_factory=list)
    is_spurious: bool = False


def materialize_witness(
    witness_db: dict[str, list[dict[str, object]]],
    catalog: Catalog,
    conn: Optional[sqlite3.Connection] = None,
) -> sqlite3.Connection:
    """Create sqlite3 tables from witness data.

    Args:
        witness_db: table_name → list of row dicts
        catalog: Schema catalog for column types
        conn: Existing connection (or create in-memory)

    Returns:
        sqlite3 connection with witness tables loaded.
    """
    if conn is None:
        conn = sqlite3.connect(":memory:")

    for table_name, rows in witness_db.items():
        tinfo = catalog.get_table(table_name)
        if tinfo is None:
            continue

        # Build CREATE TABLE
        col_defs = []
        for cinfo in tinfo.columns:
            sql_type = _sem_type_to_sql(cinfo.sem_type)
            col_defs.append(f'{quote_ident(cinfo.name)} {sql_type}')

        create_sql = f'CREATE TABLE IF NOT EXISTS {quote_ident(table_name)} ({", ".join(col_defs)})'
        conn.execute(create_sql)

        # INSERT rows
        if rows:
            col_names = [c.name for c in tinfo.columns]
            placeholders = ", ".join(["?"] * len(col_names))
            quoted_cols = ", ".join(quote_ident(c) for c in col_names)
            insert_sql = f'INSERT INTO {quote_ident(table_name)} ({quoted_cols}) VALUES ({placeholders})'

            for row in rows:
                row_norm = {k.lower(): v for k, v in row.items()}
                values = [row_norm.get(c.lower(), None) for c in col_names]
                conn.execute(insert_sql, values)

    conn.commit()
    return conn


def validate_witness_sql(
    sql1: str,
    sql2: str,
    witness_db: dict[str, list[dict[str, object]]],
    catalog: Catalog,
) -> ValidationResult:
    """Validate witness using raw SQL strings (more robust than IR rendering)."""
    conn = None
    try:
        conn = materialize_witness(witness_db, catalog)
        q1_sql = _normalize_for_sqlite(sql1)
        q2_sql = _normalize_for_sqlite(sql2)

        q1_result = conn.execute(q1_sql).fetchall()
        q2_result = conn.execute(q2_sql).fetchall()

        results_differ = Counter(q1_result) != Counter(q2_result)
        return ValidationResult(
            q1_result=q1_result,
            q2_result=q2_result,
            results_differ=results_differ,
            q1_sql=q1_sql,
            q2_sql=q2_sql,
            is_spurious=not results_differ,
        )
    except Exception as e:
        logger.debug("validate_witness_sql failed: %s", e)
        return ValidationResult(
            q1_result=[],
            q2_result=[],
            results_differ=False,
            q1_sql=sql1,
            q2_sql=sql2,
            error=str(e),
        )
    finally:
        if conn:
            conn.close()


def _witness_types_valid(
    witness_db: dict[str, list[dict[str, object]]],
    catalog: "Catalog",
) -> bool:
    """Check that all witness values are compatible with declared column types.

    Returns False if any value violates its column's declared type
    (e.g., a non-numeric string in a DATE or INT column).  This catches
    witnesses that exploit type-modeling gaps in the Z3 encoding.
    """
    from ..ir.types import SemType

    for tbl_name, rows in witness_db.items():
        tbl_info = catalog.get_table(tbl_name)
        if tbl_info is None:
            continue
        for row in rows:
            for col_name, val in row.items():
                if val is None:
                    continue
                col_info = tbl_info.get_column(col_name)
                if col_info is None:
                    continue
                st = col_info.sem_type
                if st in (SemType.INT, SemType.DATE, SemType.TIMESTAMP):
                    if not isinstance(val, (int, float)):
                        logger.debug(
                            "Type-invalid witness: %s.%s (%s) has value %r",
                            tbl_name, col_name, st, val,
                        )
                        return False
                elif st == SemType.BOOL:
                    if not isinstance(val, (int, float, bool)):
                        return False
    return True


def validate_witness(
    q1: QueryIR,
    q2: QueryIR,
    witness_db: dict[str, list[dict[str, object]]],
    catalog: Catalog,
    dialect: str = "sqlite",
) -> ValidationResult:
    """Execute both queries on the witness DB and confirm they disagree.

    Args:
        q1, q2: The two candidate query IRs.
        witness_db: The synthesized witness database.
        catalog: Schema catalog.
        dialect: SQL dialect for rendering.

    Returns:
        ValidationResult with both query results and whether they differ.
    """
    conn = None
    q1_sql = ""
    q2_sql = ""
    try:
        # Type-compatibility pre-check: reject witnesses with values
        # that violate declared column types (e.g., strings in DATE cols).
        if not _witness_types_valid(witness_db, catalog):
            return ValidationResult(
                q1_result=[],
                q2_result=[],
                results_differ=False,
                q1_sql="",
                q2_sql="",
                is_spurious=True,
            )

        conn = materialize_witness(witness_db, catalog)
        q1_sql = render(q1, dialect=dialect)
        q2_sql = render(q2, dialect=dialect)

        # Normalize backtick quoting for sqlite3
        q1_sql = _normalize_for_sqlite(q1_sql)
        q2_sql = _normalize_for_sqlite(q2_sql)

        q1_result = conn.execute(q1_sql).fetchall()
        q2_result = conn.execute(q2_sql).fetchall()

        # Compare as multisets (bag semantics, order-insensitive)
        results_differ = Counter(q1_result) != Counter(q2_result)

        is_spurious = not results_differ
        refinement_hints: list[RefinementHint] = []
        if is_spurious:
            hints = _collect_approximate_predicates(q1) + _collect_approximate_predicates(q2)
            seen: set[tuple] = set()
            for h in hints:
                key = (h.table, h.column, h.predicate_type, h.pattern)
                if key not in seen:
                    seen.add(key)
                    refinement_hints.append(h)

        return ValidationResult(
            q1_result=q1_result,
            q2_result=q2_result,
            results_differ=results_differ,
            q1_sql=q1_sql,
            q2_sql=q2_sql,
            refinement_hints=refinement_hints,
            is_spurious=is_spurious,
        )
    except Exception as e:
        logger.debug("validate_witness failed: %s", e)
        return ValidationResult(
            q1_result=[],
            q2_result=[],
            results_differ=False,
            q1_sql=q1_sql,
            q2_sql=q2_sql,
            error=str(e),
        )
    finally:
        if conn:
            conn.close()


def _collect_approximate_predicates(ir: QueryIR) -> list[RefinementHint]:
    """Walk IR to find LIKE predicates and unmodeled function calls."""
    from ..ir.types import BinOp, BinOpKind, ColumnRef, FuncCall, Literal, UnaryOp, CaseExpr

    hints: list[RefinementHint] = []
    _MODELED = {"COALESCE", "NULLIF", "ABS", "CAST", "GREATEST", "LEAST"}

    def _walk(expr):
        if expr is None:
            return
        if isinstance(expr, BinOp):
            if expr.op == BinOpKind.LIKE:
                col_name = ""
                table_name = ""
                pattern = None
                if isinstance(expr.left, ColumnRef):
                    col_name = expr.left.column
                    table_name = expr.left.table or ""
                if isinstance(expr.right, Literal) and isinstance(expr.right.value, str):
                    pattern = expr.right.value
                if col_name:
                    hints.append(RefinementHint(
                        table=table_name, column=col_name,
                        predicate_type="LIKE", pattern=pattern,
                    ))
            _walk(expr.left)
            _walk(expr.right)
        elif isinstance(expr, FuncCall):
            if expr.func_name.upper() not in _MODELED:
                for arg in expr.args:
                    if isinstance(arg, ColumnRef):
                        hints.append(RefinementHint(
                            table=arg.table or "", column=arg.column,
                            predicate_type=expr.func_name.upper(),
                        ))
                        break
            for arg in expr.args:
                _walk(arg)
        elif isinstance(expr, UnaryOp):
            _walk(expr.operand)
        elif isinstance(expr, CaseExpr):
            for w in expr.whens:
                _walk(w.when)
                _walk(w.then)
            _walk(expr.else_)
        elif isinstance(expr, ColumnRef):
            pass

    for s in ir.select:
        _walk(s)
    _walk(ir.where)
    for j in ir.joins:
        _walk(j.on)
    _walk(ir.having)
    return hints


def _normalize_for_sqlite(sql: str) -> str:
    """Normalize SQL for sqlite3 execution."""
    import re
    sql = re.sub(r"`([^`]+)`", r'"\1"', sql)
    # SQLite uses SUBSTR() not SUBSTRING()
    sql = re.sub(r'\bSUBSTRING\s*\(', 'SUBSTR(', sql, flags=re.IGNORECASE)
    # Quote bare $-identifiers at identifier start (e.g., $cor0 → "$cor0",
    # t.$f0 → t."$f0") but not mid-identifier (EXPR$0 stays unchanged)
    sql = re.sub(r'(?<=[\s,.(=])\$(\w+)', r'"$\1"', sql)
    # Handle $ at start of string
    if sql.startswith('$'):
        sql = re.sub(r'^\$(\w+)', r'"$\1"', sql)
    return sql


def _normalize_for_duckdb(sql: str, dialect: str = "mysql") -> str:
    """Normalize SQL for DuckDB execution."""
    import re
    # Convert backtick quoting to double-quote quoting
    sql = re.sub(r"`([^`]+)`", r'"\1"', sql)
    # Quote bare $-identifiers at identifier start (e.g., $cor0 → "$cor0",
    # t.$f0 → t."$f0") but not mid-identifier (EXPR$0 stays unchanged)
    sql = re.sub(r'(?<=[\s,.(=])\$(\w+)', r'"$\1"', sql)
    if sql.startswith('$'):
        sql = re.sub(r'^\$(\w+)', r'"$\1"', sql)

    # FIX.28a: MySQL || is logical OR; DuckDB || is string concatenation.
    # Only rewrite for MySQL dialect — standard SQL uses || for concatenation.
    if dialect == "mysql":
        sql = re.sub(r'\|\|', ' OR ', sql)

    # FIX.28a: Rewrite MySQL functions to DuckDB equivalents
    # DATEDIFF(a, b) → DATE_DIFF('day', b, a) (note: reversed arg order)
    sql = re.sub(
        r'\bDATEDIFF\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)',
        r"DATE_DIFF('day', \2, \1)",
        sql, flags=re.IGNORECASE,
    )
    # ISNULL(expr) → (expr IS NULL)  (MySQL scalar function)
    sql = re.sub(
        r'\bISNULL\s*\(\s*([^)]+?)\s*\)',
        r'(\1 IS NULL)',
        sql, flags=re.IGNORECASE,
    )
    # MySQL IF(cond, then, else) → CASE WHEN cond THEN then ELSE else END
    # Only match standalone IF (not IFNULL, IIF, etc.)
    sql = re.sub(
        r'(?<!\w)IF\s*\(\s*([^,]+?)\s*,\s*([^,]+?)\s*,\s*([^)]+?)\s*\)',
        r'CASE WHEN \1 THEN \2 ELSE \3 END',
        sql, flags=re.IGNORECASE,
    )
    # INTERVAL N DAY → INTERVAL N DAY (DuckDB handles this but parser
    # sometimes needs DATE_ADD/DATE_SUB rewritten)
    # DATE_SUB(date, INTERVAL n DAY) → date - INTERVAL 'n' DAY
    sql = re.sub(
        r'\bDATE_SUB\s*\(\s*([^,]+?)\s*,\s*INTERVAL\s+(\w+)\s+(\w+)\s*\)',
        r"(\1 - INTERVAL '\2' \3)",
        sql, flags=re.IGNORECASE,
    )
    sql = re.sub(
        r'\bDATE_ADD\s*\(\s*([^,]+?)\s*,\s*INTERVAL\s+(\w+)\s+(\w+)\s*\)',
        r"(\1 + INTERVAL '\2' \3)",
        sql, flags=re.IGNORECASE,
    )
    return sql


def _materialize_witness_duckdb(
    witness_db: dict[str, list[dict[str, object]]],
    catalog: Catalog,
):
    """Create DuckDB in-memory tables from witness data.

    Returns a duckdb connection with witness tables loaded.
    """
    import duckdb
    from ..ir.types import SemType

    conn = duckdb.connect(":memory:")

    for table_name, rows in witness_db.items():
        tinfo = catalog.get_table(table_name)
        if tinfo is None:
            continue

        # Build CREATE TABLE — use native DuckDB types (DATE, TIMESTAMP, etc.)
        col_defs = []
        for cinfo in tinfo.columns:
            sql_type = _sem_type_to_sql(cinfo.sem_type, dialect="duckdb")
            col_defs.append(f'{quote_ident(cinfo.name)} {sql_type}')

        create_sql = f'CREATE TABLE IF NOT EXISTS {quote_ident(table_name)} ({", ".join(col_defs)})'
        conn.execute(create_sql)

        # INSERT rows — coerce values to match column types (DuckDB is
        # stricter than SQLite about type mismatches, e.g. string
        # sentinel values in INT columns).
        if rows:
            col_names = [c.name for c in tinfo.columns]
            col_types = {c.name.lower(): c.sem_type for c in tinfo.columns}
            placeholders = ", ".join(["?"] * len(col_names))
            quoted_cols = ", ".join(quote_ident(c) for c in col_names)
            insert_sql = f'INSERT INTO {quote_ident(table_name)} ({quoted_cols}) VALUES ({placeholders})'

            for row in rows:
                row_norm = {k.lower(): v for k, v in row.items()}
                values = []
                for c in col_names:
                    v = row_norm.get(c.lower(), None)
                    if v is not None:
                        ctype = col_types.get(c.lower())
                        if ctype and isinstance(v, str):
                            if ctype.is_numeric() or ctype == SemType.BOOL:
                                try:
                                    v = int(v)
                                except (ValueError, TypeError):
                                    v = None
                            elif ctype.is_temporal():
                                # Sentinel strings or non-date strings → NULL
                                if v.startswith('\x01') or v.startswith('\x7f'):
                                    v = None
                                else:
                                    # Validate the date string is parseable
                                    import re
                                    if not re.match(r'^\d{4}-\d{2}-\d{2}', v):
                                        v = None
                        elif ctype and isinstance(v, (int, float)):
                            if ctype.is_temporal():
                                # Integer in DATE/TIMESTAMP column: convert to
                                # ISO date string using epoch mapping (same as
                                # _extract_witness in witness_synthesis.py)
                                from datetime import date, timedelta
                                try:
                                    v = str(date(2024, 1, 1) + timedelta(days=int(v)))
                                    if ctype == SemType.TIMESTAMP:
                                        v += " 00:00:00"
                                except (OverflowError, ValueError):
                                    v = None
                            elif ctype == SemType.STRING:
                                v = str(v)
                    values.append(v)
                conn.execute(insert_sql, values)

    return conn


def validate_witness_duckdb(
    sql1: str,
    sql2: str,
    witness_db: dict[str, list[dict[str, object]]],
    catalog: Catalog,
    dialect: str = "mysql",
) -> ValidationResult:
    """Validate witness using raw SQL strings on DuckDB (fallback for SQLite).

    DuckDB handles constructs that SQLite cannot: VALUES in certain positions,
    INTERSECT ALL, $-identifiers, FETCH/OFFSET syntax.
    """
    conn = None
    try:
        conn = _materialize_witness_duckdb(witness_db, catalog)
        q1_sql = _normalize_for_duckdb(sql1, dialect=dialect)
        q2_sql = _normalize_for_duckdb(sql2, dialect=dialect)

        # FIX.28a: Execute with retry for MySQL implicit GROUP BY.
        # DuckDB enforces ONLY_FULL_GROUP_BY; MySQL doesn't.  When a query
        # fails with "must appear in the GROUP BY clause", retry with
        # ANY_VALUE() wrapping around the offending column.
        def _exec_with_groupby_retry(conn, sql):
            import re as _re
            for _attempt in range(5):
                try:
                    cur = conn.execute(sql)
                    return cur
                except Exception as e:
                    msg = str(e)
                    m = _re.search(
                        r'column "([^"]+)" must appear in the GROUP BY',
                        msg, _re.IGNORECASE,
                    )
                    if m:
                        col = m.group(1)
                        # Wrap bare column ref with ANY_VALUE()
                        # Match: SELECT ... col_name ... (as identifier, not inside function)
                        sql = _re.sub(
                            r'(?<![.\w])' + _re.escape(col) + r'(?!\s*\()',
                            f'ANY_VALUE({col})',
                            sql, count=1,
                        )
                        continue
                    raise

        cur1 = _exec_with_groupby_retry(conn, q1_sql)
        q1_result = cur1.fetchall()
        cols1 = [d[0].upper() for d in cur1.description] if cur1.description else []
        cur2 = _exec_with_groupby_retry(conn, q2_sql)
        q2_result = cur2.fetchall()
        cols2 = [d[0].upper() for d in cur2.description] if cur2.description else []

        # FIX.20b: Normalize column order before comparison.
        # When both queries project the same set of column names but in
        # a different order (e.g., SELECT * vs explicit column list),
        # reorder Q2's tuples to match Q1's column order.
        if (cols1 and cols2
                and len(cols1) == len(cols2)
                and sorted(cols1) == sorted(cols2)
                and cols1 != cols2):
            col2_pos: dict[str, list[int]] = {}
            for i, c in enumerate(cols2):
                col2_pos.setdefault(c, []).append(i)
            rmap = [col2_pos[c].pop(0) for c in cols1]
            q2_result = [tuple(row[rmap[i]] for i in range(len(row))) for row in q2_result]

        results_differ = Counter(q1_result) != Counter(q2_result)
        return ValidationResult(
            q1_result=q1_result,
            q2_result=q2_result,
            results_differ=results_differ,
            q1_sql=q1_sql,
            q2_sql=q2_sql,
            is_spurious=not results_differ,
        )
    except Exception as e:
        logger.debug("validate_witness_duckdb failed: %s", e)
        return ValidationResult(
            q1_result=[],
            q2_result=[],
            results_differ=False,
            q1_sql=sql1,
            q2_sql=sql2,
            error=str(e),
        )
    finally:
        if conn:
            conn.close()


def empirical_equivalence_check(
    sql1: str,
    sql2: str,
    catalog: Catalog,
    n_tests: int = 8,
) -> bool:
    """FIX.31b: Empirical equivalence check using diverse test databases.

    Generates *n_tests* random databases of varying sizes and tests whether
    both queries produce identical results on each.  Used as an escalation
    when Z3 produces consistent spurious SAT (the bounded encoding is too
    imprecise but real execution always agrees).

    Returns True if all test databases produce matching results (strong
    empirical evidence of equivalence).  Returns False if any test shows
    a difference or if execution fails.
    """
    import random
    from datetime import date, timedelta

    try:
        import duckdb
    except ImportError:
        return False

    rng = random.Random(42)
    n_successful = 0  # FIX.33b: track tests where both queries executed

    for test_idx in range(n_tests):
        conn = None
        try:
            conn = duckdb.connect(":memory:")

            # Create tables
            for tname, tinfo in catalog.tables.items():
                col_defs = []
                for col in tinfo.columns:
                    sql_type = _sem_type_to_sql(col.sem_type, dialect="duckdb")
                    null_clause = "" if col.nullable else " NOT NULL"
                    col_defs.append(f'"{col.name}" {sql_type}{null_clause}')
                pk_cols = [c.name for c in tinfo.columns if c.is_primary_key]
                if pk_cols:
                    col_defs.append(f'PRIMARY KEY ({", ".join(f"{c}" for c in pk_cols)})')
                ddl = f'CREATE TABLE "{tname}" ({", ".join(col_defs)})'
                conn.execute(ddl)

            # Generate random data with varying sizes
            n_rows = [1, 1, 2, 2, 3, 3, 4, 5][test_idx % 8]
            for tname, tinfo in catalog.tables.items():
                pk_cols = [c for c in tinfo.columns if c.is_primary_key]
                pk_names = {c.name.lower() for c in pk_cols}
                pk_groups = tinfo.primary_key_groups if tinfo.primary_key_groups else []

                # Generate unique PK combinations
                used_pks: set[tuple] = set()
                rows_inserted = 0
                for _ in range(n_rows * 3):  # attempts
                    if rows_inserted >= n_rows:
                        break
                    vals = {}
                    for col in tinfo.columns:
                        from ..ir.types import SemType
                        if col.nullable and rng.random() < 0.2:
                            vals[col.name] = None
                        elif col.sem_type == SemType.INT:
                            vals[col.name] = rng.randint(1, max(3, n_rows))
                        elif col.sem_type == SemType.DATE:
                            base = date(2020, 1, 1)
                            vals[col.name] = base + timedelta(days=rng.randint(0, max(5, n_rows * 2)))
                        elif col.sem_type == SemType.BOOL:
                            vals[col.name] = rng.choice([True, False])
                        elif col.sem_type in (SemType.FLOAT, SemType.DECIMAL):
                            vals[col.name] = round(rng.uniform(0, 10), 2)
                        else:
                            vals[col.name] = rng.choice(["a", "b", "c", "d"])

                    # Ensure NOT NULL for PK columns
                    for col in pk_cols:
                        if vals.get(col.name) is None:
                            if col.sem_type == SemType.INT:
                                vals[col.name] = rng.randint(1, max(3, n_rows))
                            elif col.sem_type == SemType.DATE:
                                base = date(2020, 1, 1)
                                vals[col.name] = base + timedelta(days=rng.randint(0, max(5, n_rows * 2)))
                            else:
                                vals[col.name] = rng.choice(["a", "b", "c"])

                    # Check PK uniqueness
                    pk_vals = tuple(vals.get(c.name) for c in pk_cols) if pk_cols else (rows_inserted,)
                    if pk_vals in used_pks:
                        continue
                    used_pks.add(pk_vals)

                    # Insert
                    col_names = [c.name for c in tinfo.columns]
                    placeholders = ", ".join(["?"] * len(col_names))
                    try:
                        conn.execute(
                            f'INSERT INTO "{tname}" ({", ".join(f"{c}" for c in col_names)}) VALUES ({placeholders})',
                            [vals[c] for c in col_names],
                        )
                        rows_inserted += 1
                    except Exception:
                        continue

            # Execute both queries
            q1_sql = _normalize_for_duckdb(sql1, dialect="mysql")
            q2_sql = _normalize_for_duckdb(sql2, dialect="mysql")

            try:
                r1 = conn.execute(q1_sql).fetchall()
            except Exception:
                continue  # Q1 fails on this DB, skip

            try:
                r2 = conn.execute(q2_sql).fetchall()
            except Exception:
                continue  # Q2 fails on this DB, skip

            n_successful += 1
            if Counter(r1) != Counter(r2):
                logger.debug("Empirical check: mismatch on test %d (r1=%s, r2=%s)", test_idx, r1, r2)
                return False

        except Exception:
            continue  # DB setup failed, skip this test
        finally:
            if conn:
                conn.close()

    # FIX.33b: If no test successfully executed both queries, we have no
    # evidence of equivalence.  This prevents vacuous promotion when both
    # SQL strings are invalid (e.g., unquoted identifiers, missing functions).
    if n_successful == 0:
        logger.debug("Empirical check: 0/%d tests succeeded, no evidence", n_tests)
        return False
    return True


def format_witness(
    witness_db: dict[str, list[dict[str, object]]],
    validation: Optional[ValidationResult] = None,
) -> str:
    """Format a witness as a human-readable string."""
    lines: list[str] = ["=== Witness Database ===", ""]

    for table_name, rows in witness_db.items():
        lines.append(f"Table: {table_name}")
        if not rows:
            lines.append("  (empty)")
            continue

        cols = list(rows[0].keys())
        # Header
        header = " | ".join(f"{c:>10}" for c in cols)
        lines.append(f"  {header}")
        lines.append(f"  {'-' * len(header)}")
        # Rows
        for row in rows:
            vals = []
            for c in cols:
                v = row.get(c)
                vals.append(f"{'NULL':>10}" if v is None else f"{v!s:>10}")
            lines.append(f"  {' | '.join(vals)}")
        lines.append("")

    if validation:
        lines.append("=== Query Results ===")
        lines.append(f"Q1: {validation.q1_sql[:100]}...")
        lines.append(f"  Result: {validation.q1_result}")
        lines.append(f"Q2: {validation.q2_sql[:100]}...")
        lines.append(f"  Result: {validation.q2_result}")
        lines.append(f"  Differ: {validation.results_differ}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sem_type_to_sql(sem_type, dialect: str = "sqlite") -> str:
    """Convert SemType to SQL type string.

    For SQLite: DATE/TIMESTAMP map to TEXT (SQLite is typeless).
    For DuckDB: DATE/TIMESTAMP map to their native types so that
    EXTRACT/date_part works correctly.
    """
    from ..ir.types import SemType
    if dialect == "duckdb":
        mapping = {
            SemType.INT: "INTEGER",
            SemType.FLOAT: "DOUBLE",
            SemType.DECIMAL: "DOUBLE",
            SemType.BOOL: "BOOLEAN",
            SemType.STRING: "VARCHAR",
            SemType.DATE: "DATE",
            SemType.TIMESTAMP: "TIMESTAMP",
            SemType.UNKNOWN: "VARCHAR",
        }
    else:
        mapping = {
            SemType.INT: "INTEGER",
            SemType.FLOAT: "REAL",
            SemType.DECIMAL: "REAL",
            SemType.BOOL: "INTEGER",
            SemType.STRING: "TEXT",
            SemType.DATE: "TEXT",
            SemType.TIMESTAMP: "TEXT",
            SemType.UNKNOWN: "TEXT",
        }
    return mapping.get(sem_type, "TEXT")
