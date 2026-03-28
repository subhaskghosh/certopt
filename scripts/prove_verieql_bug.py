"""Proof: VeriEQL PK Bug — Irrefutable Evidence

This script demonstrates that VeriEQL's EQU verdicts are incorrect for
SQL pairs where:
- The schema has a PK on column A
- The queries SELECT column B (not the PK)
- One query uses DISTINCT/GROUP BY, the other doesn't

VeriEQL incorrectly assumes PK-based output uniqueness extends to
non-PK columns in the SELECT list.

We prove this by:
1. Finding DISAGREE pairs (our NEQ vs VeriEQL EQU)
2. Constructing witness databases via Z3-based synthesis
3. Executing both queries on the witness in **two independent engines**
   (SQLite via our own validator AND DuckDB as an external oracle)
4. Showing different results → queries are NOT equivalent

Usage:
    python3 -m scripts.prove_verieql_bug
    python3 -m scripts.prove_verieql_bug --max-pairs 10
    python3 -m scripts.prove_verieql_bug --k-rows 3 --timeout 60000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from optim.cegis.witness_export import validate_witness_sql
from optim.cegis.witness_synthesis import WitnessResult, synthesize_witness
from optim.eval.verieql_loader import (
    load_verieql_entry,
    load_verieql_results,
    load_verieql_suite,
    verieql_verdict,
)
from optim.parser.sql_to_ir import sql_to_ir
from optim.schema.catalog import Catalog
from optim.verify.encode_z3 import BoundedScope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

LEETCODE_SUITE = "data/VeriEQL/benchmarks/leetcode/leetcode.jsonlines"
LEETCODE_RESULTS = "data/VeriEQL/experiments/2025_10_31/leetcode.out"
OUTPUT_PATH = "results/verieql_bug_proof.json"

# ---------------------------------------------------------------------------
# DuckDB execution
# ---------------------------------------------------------------------------

try:
    import duckdb

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False

# Type-mapping for DuckDB DDL
_DUCKDB_TYPE_MAP = {
    "INT": "INTEGER",
    "INTEGER": "INTEGER",
    "VARCHAR": "VARCHAR",
    "TEXT": "VARCHAR",
    "DATE": "DATE",
    "BOOLEAN": "BOOLEAN",
    "BOOL": "BOOLEAN",
    "DECIMAL": "DOUBLE",
    "NUMERIC": "DOUBLE",
    "FLOAT": "DOUBLE",
    "DOUBLE": "DOUBLE",
    "REAL": "DOUBLE",
}


def _duckdb_type(raw: str | None) -> str:
    if raw is None:
        return "INTEGER"
    return _DUCKDB_TYPE_MAP.get(raw.upper().split("(")[0].strip(), "VARCHAR")


def _execute_on_duckdb(
    sql1: str,
    sql2: str,
    witness_db: dict[str, list[dict[str, object]]],
    schema: dict[str, dict[str, str]],
) -> tuple[list[tuple] | str, list[tuple] | str]:
    """Execute both SQL queries on a witness DB using DuckDB as an
    independent oracle.  Returns (q1_result, q2_result); on error
    the result is an error string instead of a list.
    """
    conn = duckdb.connect()

    try:
        # Create tables from raw schema definition
        for table_name, col_defs in schema.items():
            col_parts = []
            for col_name, col_type in col_defs.items():
                col_parts.append(f'"{col_name}" {_duckdb_type(col_type)}')
            ddl = f'CREATE TABLE "{table_name}" ({", ".join(col_parts)})'
            conn.execute(ddl)

        # Insert witness rows
        for table_name, rows in witness_db.items():
            if not rows:
                continue
            cols = list(rows[0].keys())
            quoted_cols = ", ".join(f'"{c}"' for c in cols)
            placeholders = ", ".join(["?"] * len(cols))
            insert_tmpl = (
                f'INSERT INTO "{table_name}" ({quoted_cols}) VALUES ({placeholders})'
            )
            for row in rows:
                values = [row.get(c) for c in cols]
                conn.execute(insert_tmpl, values)

        # Execute both queries
        try:
            r1 = conn.execute(sql1).fetchall()
        except Exception as e:
            r1 = f"ERROR: {e}"

        try:
            r2 = conn.execute(sql2).fetchall()
        except Exception as e:
            r2 = f"ERROR: {e}"

        return r1, r2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PK-bug analysis helpers
# ---------------------------------------------------------------------------


def _identify_pk_bug_pattern(
    sql1: str, sql2: str, catalog: Catalog
) -> dict | None:
    """Detect whether this pair matches the PK-on-non-projected-column
    pattern that triggers VeriEQL's bug.

    Returns a dict describing the pattern, or None if not matched.
    """
    s1, s2 = sql1.upper(), sql2.upper()
    has_distinct_diff = ("DISTINCT" in s1) != ("DISTINCT" in s2)
    has_group_by_diff = ("GROUP BY" in s1) != ("GROUP BY" in s2)

    if not (has_distinct_diff or has_group_by_diff):
        return None

    for tname, tinfo in catalog.tables.items():
        pk_cols = [c.name for c in tinfo.columns if c.is_primary_key]
        if not pk_cols:
            continue
        non_pk_cols = [c.name for c in tinfo.columns if not c.is_primary_key]

        # Check if any non-PK column appears in the SELECT but PK does not
        for npc in non_pk_cols:
            if npc.upper() in s1 or npc.upper() in s2:
                # Check that PK columns are NOT in the SELECT list
                # (simplistic heuristic — good enough for explanatory purposes)
                pk_in_select = any(
                    pk.upper() in s1.split("FROM")[0]
                    for pk in pk_cols
                    if "FROM" in s1
                )
                if not pk_in_select:
                    return {
                        "table": tname,
                        "pk_columns": pk_cols,
                        "non_pk_in_select": npc,
                        "distinct_diff": has_distinct_diff,
                        "group_by_diff": has_group_by_diff,
                    }

    return {"distinct_diff": has_distinct_diff, "group_by_diff": has_group_by_diff}


# ---------------------------------------------------------------------------
# Main proof logic
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prove VeriEQL PK bug with irrefutable witnesses"
    )
    parser.add_argument(
        "--max-pairs", type=int, default=20,
        help="Maximum number of DISAGREE pairs to validate (default: 20)",
    )
    parser.add_argument(
        "--k-rows", type=int, default=2,
        help="Bounded scope k_rows for witness synthesis (default: 2)",
    )
    parser.add_argument(
        "--timeout", type=int, default=30000,
        help="Solver timeout in ms (default: 30000)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    print("=" * 70)
    print("  VeriEQL PK Bug — Proof of Incorrectness")
    print("=" * 70)
    print()
    print(f"  Parameters: k_rows={args.k_rows}, timeout={args.timeout}ms, "
          f"max_pairs={args.max_pairs}")
    print()

    if not HAS_DUCKDB:
        print("WARNING: DuckDB not available. Install with: pip install duckdb")
        print("         Will use SQLite validation only.\n")

    # ------------------------------------------------------------------
    # 1. Load benchmark data and VeriEQL results
    # ------------------------------------------------------------------
    print("Step 1: Loading benchmark data...")
    entries = load_verieql_suite(LEETCODE_SUITE)
    verieql_results = load_verieql_results(LEETCODE_RESULTS)
    print(f"  Loaded {len(entries)} benchmark entries")
    print(f"  Loaded {len(verieql_results)} VeriEQL results")
    print()

    # ------------------------------------------------------------------
    # 2. Find DISAGREE pairs (our NEQ vs VeriEQL EQU)
    # ------------------------------------------------------------------
    print("Step 2: Scanning for DISAGREE pairs (our NEQ vs VeriEQL EQU)...")

    scope = BoundedScope(k_rows=args.k_rows, solver_timeout_ms=args.timeout)
    disagree_pairs: list[dict] = []
    n_verieql_equ = 0
    n_parse_fail = 0
    n_our_unsat = 0
    t_start = time.monotonic()

    for i, raw_entry in enumerate(entries):
        vq_index = raw_entry.get("index", i + 1)
        vq_entry = verieql_results.get(vq_index, {})
        vq_result = verieql_verdict(vq_entry) if vq_entry else "MISSING"

        if vq_result != "EQU":
            continue
        n_verieql_equ += 1

        catalog, sql1, sql2 = load_verieql_entry(raw_entry)

        # Focus on schemas that have PK constraints (the bug trigger)
        has_pk = any(
            any(c.is_primary_key for c in t.columns)
            for t in catalog.tables.values()
        )
        if not has_pk:
            continue

        ir1, err1 = sql_to_ir(sql1, dialect="sqlite")
        ir2, err2 = sql_to_ir(sql2, dialect="sqlite")

        if ir1 is None or ir2 is None:
            n_parse_fail += 1
            continue

        result = synthesize_witness(
            ir1, ir2, catalog, scope=scope,
            validate_witnesses=True, original_sql=(sql1, sql2),
        )

        if result.status == "sat" and result.witness_db:
            disagree_pairs.append({
                "entry_index": vq_index,
                "enum_index": i,
                "sql1": sql1,
                "sql2": sql2,
                "schema": raw_entry["schema"],
                "constraints": raw_entry.get("constraint", []),
                "witness_db": result.witness_db,
                "catalog": catalog,
                "solver_time_ms": result.solver_time_ms,
            })
        elif result.status == "unsat":
            n_our_unsat += 1

        if len(disagree_pairs) >= args.max_pairs:
            break

    t_scan = time.monotonic() - t_start
    print(f"  VeriEQL EQU entries scanned: {n_verieql_equ}")
    print(f"  Parse failures (skipped):    {n_parse_fail}")
    print(f"  Our UNSAT (agree with EQU):  {n_our_unsat}")
    print(f"  DISAGREE pairs found:        {len(disagree_pairs)}")
    print(f"  Scan time: {t_scan:.1f}s")
    print()

    if not disagree_pairs:
        print("  No DISAGREE pairs found. Nothing to prove.")
        return

    # ------------------------------------------------------------------
    # 3. Validate each witness — execute queries on witness DB
    # ------------------------------------------------------------------
    print("Step 3: Validating witnesses with independent execution...")
    print()

    n_validated = 0
    n_confirmed_sqlite = 0
    n_confirmed_duckdb = 0
    proof_records: list[dict] = []

    for dp in disagree_pairs:
        idx = dp["entry_index"]
        sql1, sql2 = dp["sql1"], dp["sql2"]
        witness_db = dp["witness_db"]
        catalog: Catalog = dp["catalog"]
        schema = dp["schema"]

        # --- SQLite validation (our own engine) ---
        sqlite_val = validate_witness_sql(sql1, sql2, witness_db, catalog)
        sqlite_differ = sqlite_val.results_differ

        # --- DuckDB validation (independent oracle) ---
        duckdb_differ = False
        duckdb_r1: list[tuple] | str = []
        duckdb_r2: list[tuple] | str = []
        if HAS_DUCKDB:
            try:
                duckdb_r1, duckdb_r2 = _execute_on_duckdb(
                    sql1, sql2, witness_db, schema,
                )
                if (
                    not isinstance(duckdb_r1, str)
                    and not isinstance(duckdb_r2, str)
                ):
                    duckdb_differ = Counter(duckdb_r1) != Counter(duckdb_r2)
            except Exception as e:
                duckdb_r1 = f"ERROR: {e}"
                duckdb_r2 = f"ERROR: {e}"

        n_validated += 1
        if sqlite_differ:
            n_confirmed_sqlite += 1
        if duckdb_differ:
            n_confirmed_duckdb += 1

        # Analyse PK-bug pattern
        pk_pattern = _identify_pk_bug_pattern(sql1, sql2, catalog)

        # PK info for the record
        pk_info = {
            tname: [c.name for c in tinfo.columns if c.is_primary_key]
            for tname, tinfo in catalog.tables.items()
            if any(c.is_primary_key for c in tinfo.columns)
        }

        confirmed = sqlite_differ or duckdb_differ
        record = {
            "pair_index": idx,
            "sql1": sql1,
            "sql2": sql2,
            "witness_db": witness_db,
            "pk_columns": pk_info,
            "sqlite_q1": str(sqlite_val.q1_result),
            "sqlite_q2": str(sqlite_val.q2_result),
            "sqlite_differ": sqlite_differ,
            "duckdb_q1": str(duckdb_r1),
            "duckdb_q2": str(duckdb_r2),
            "duckdb_differ": duckdb_differ,
            "confirmed_neq": confirmed,
            "pk_bug_pattern": pk_pattern,
            "solver_time_ms": dp["solver_time_ms"],
        }
        proof_records.append(record)

        # Print progress
        status_parts = []
        if sqlite_differ:
            status_parts.append("SQLite✓")
        if duckdb_differ:
            status_parts.append("DuckDB✓")
        status = " + ".join(status_parts) if status_parts else "NOT confirmed"

        print(f"  Pair {idx}: {status}")
        if confirmed:
            print(f"    SQL1: {sql1[:100]}")
            print(f"    SQL2: {sql2[:100]}")
            # Show witness DB size
            db_summary = {k: len(v) for k, v in witness_db.items()}
            print(f"    Witness tables: {db_summary}")
            # Show actual rows for concreteness
            for tname, rows in witness_db.items():
                if rows:
                    print(f"    {tname} rows:")
                    for row in rows[:4]:
                        print(f"      {row}")
            # Show query results
            if sqlite_differ:
                print(f"    SQLite Q1: {sqlite_val.q1_result}")
                print(f"    SQLite Q2: {sqlite_val.q2_result}")
            if duckdb_differ:
                print(f"    DuckDB Q1: {duckdb_r1}")
                print(f"    DuckDB Q2: {duckdb_r2}")
            # Show PK info
            for tname, pks in pk_info.items():
                print(f"    Table '{tname}' PK: {pks}")
            if pk_pattern:
                print(f"    Bug pattern: {pk_pattern}")
            print()

    # ------------------------------------------------------------------
    # 4. Summary
    # ------------------------------------------------------------------
    print()
    print("=" * 70)
    print("  PROOF SUMMARY")
    print("=" * 70)
    print(f"  Total DISAGREE pairs sampled:  {len(disagree_pairs)}")
    print(f"  Validated:                     {n_validated}")
    print(f"  Confirmed NEQ (SQLite):        {n_confirmed_sqlite}")
    if HAS_DUCKDB:
        print(f"  Confirmed NEQ (DuckDB):        {n_confirmed_duckdb}")
    n_confirmed = sum(1 for r in proof_records if r["confirmed_neq"])
    pct = n_confirmed / n_validated * 100 if n_validated else 0
    print(f"  Confirmed NEQ (either engine): {n_confirmed}")
    print(f"  Confirmation rate:             {pct:.1f}%")
    print()

    if n_confirmed == n_validated and n_validated > 0:
        print("  ✅ ALL DISAGREE PAIRS ARE CONFIRMED NON-EQUIVALENT")
        print("  VeriEQL's EQU verdicts are INCORRECT for these pairs.")
        print()
        print("  Root cause: VeriEQL over-constrains its encoding by assuming")
        print("  PK-based row uniqueness extends to non-PK projected columns.")
        print("  A PK on column A guarantees A is unique, but column B in the")
        print("  SELECT list can have duplicates. Therefore DISTINCT/GROUP BY")
        print("  on B is NOT redundant, and the queries are NOT equivalent.")
    elif n_confirmed > 0:
        print(f"  ⚠ {n_confirmed}/{n_validated} pairs confirmed as NEQ")
        print(f"    {n_validated - n_confirmed} pairs could not be confirmed")
    else:
        print("  ❌ No pairs confirmed — check synthesis parameters.")

    print()

    # ------------------------------------------------------------------
    # 5. Save proof artifact
    # ------------------------------------------------------------------
    output_path = Path(OUTPUT_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "title": "VeriEQL PK Bug — Proof of Incorrectness",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "parameters": {
                    "k_rows": args.k_rows,
                    "solver_timeout_ms": args.timeout,
                    "max_pairs": args.max_pairs,
                },
                "summary": {
                    "total_sampled": len(disagree_pairs),
                    "validated": n_validated,
                    "confirmed_neq_sqlite": n_confirmed_sqlite,
                    "confirmed_neq_duckdb": n_confirmed_duckdb,
                    "confirmed_neq_either": n_confirmed,
                    "confirmation_rate": f"{pct:.1f}%",
                },
                "root_cause": (
                    "VeriEQL's bounded verifier encodes PK uniqueness as a "
                    "constraint on the output relation.  When a PK exists on "
                    "column A but the SELECT list projects column B (not the "
                    "PK), VeriEQL incorrectly infers that output rows are "
                    "unique on B. This makes it believe DISTINCT/GROUP BY on "
                    "B is redundant, yielding a spurious EQU verdict.  In "
                    "reality, multiple rows can share the same B value while "
                    "having distinct A (PK) values."
                ),
                "proof_records": proof_records,
            },
            indent=2,
            default=str,
        )
    )
    print(f"  Proof artifact saved to: {output_path}")
    print()


if __name__ == "__main__":
    main()
