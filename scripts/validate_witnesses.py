"""Re-validate all NEQ witnesses from evaluation results.

Independently re-executes both queries on each witness database
using DuckDB (primary) and SQLite (fallback) to confirm that the
results genuinely differ.

Usage:
    python3 -m scripts.validate_witnesses
    python3 -m scripts.validate_witnesses --suite calcite
    python3 -m scripts.validate_witnesses --filter our-neq-vq-equ
    python3 -m scripts.validate_witnesses --results-dir results/verieql_leetcode
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from src.optim.cegis.witness_export import (
    validate_witness_duckdb,
    validate_witness_sql,
)
from src.optim.ir.types import SemType
from src.optim.schema.catalog import Catalog, ColumnInfo, TableInfo

logger = logging.getLogger(__name__)

SUITES = {
    "leetcode": "results/verieql_leetcode",
    "calcite": "results/verieql_calcite",
    "literature": "results/verieql_literature",
}

_TYPE_MAP = {
    "int": SemType.INT, "integer": SemType.INT,
    "str": SemType.STRING, "string": SemType.STRING,
    "text": SemType.STRING, "varchar": SemType.STRING,
    "float": SemType.FLOAT, "real": SemType.FLOAT,
    "double": SemType.FLOAT, "decimal": SemType.DECIMAL,
    "date": SemType.DATE, "datetime": SemType.TIMESTAMP,
    "bool": SemType.BOOL, "boolean": SemType.BOOL,
}


def _catalog_from_schema(schema_dict: dict) -> Catalog:
    """Build Catalog from VeriEQL trace schema {TABLE: {COL: TYPE}}."""
    tables = {}
    for tname, cols in schema_dict.items():
        columns = [
            ColumnInfo(
                name=c,
                sem_type=_TYPE_MAP.get(t.lower(), SemType.UNKNOWN),
            )
            for c, t in cols.items()
        ]
        tables[tname] = TableInfo(name=tname, columns=columns)
    return Catalog(tables=tables)


def _format_witness_table(table_name: str, rows: list[dict]) -> str:
    """Format a witness table as a readable text table."""
    if not rows:
        return f"  {table_name}: (empty)"
    cols = list(rows[0].keys())
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    sep = "-+-".join("-" * widths[c] for c in cols)
    lines = [f"  {table_name}:", f"    {header}", f"    {sep}"]
    for row in rows:
        line = " | ".join(str(row.get(c, "")).ljust(widths[c]) for c in cols)
        lines.append(f"    {line}")
    return "\n".join(lines)


def _show_detail(pair_index: int, sql1: str, sql2: str,
                 witness_db: dict, catalog: Catalog, status: str,
                 q1_result: list, q2_result: list) -> None:
    """Print detailed witness reasoning for one pair."""
    print(f"\n{'='*70}")
    print(f"Pair {pair_index}  [{status.upper()}]")
    print(f"{'='*70}")
    print(f"\n  Q1: {sql1}")
    print(f"\n  Q2: {sql2}")
    print(f"\n  Witness database:")
    for tname, rows in witness_db.items():
        print(_format_witness_table(tname, rows))
    print(f"\n  Q1 result: {q1_result}")
    print(f"  Q2 result: {q2_result}")
    if status == "confirmed":
        print(f"\n  Verdict: Results differ -- witness confirms non-equivalence.")
    elif status == "spurious":
        print(f"\n  Verdict: Results match -- witness is spurious.")
    else:
        print(f"\n  Verdict: Could not execute queries on witness database.")


def _validate_one(
    sql1: str, sql2: str, witness_db: dict, catalog: Catalog,
) -> tuple[str, list, list]:
    """Validate a single witness.

    Returns (status, q1_result, q2_result) where status is
    'confirmed', 'spurious', or 'error'.
    """
    # DuckDB primary
    try:
        vr = validate_witness_duckdb(sql1, sql2, witness_db, catalog)
        if not vr.error:
            status = "confirmed" if vr.results_differ else "spurious"
            return status, vr.q1_result, vr.q2_result
    except Exception:
        pass

    # SQLite fallback
    try:
        vr = validate_witness_sql(sql1, sql2, witness_db, catalog)
        if not vr.error:
            status = "confirmed" if vr.results_differ else "spurious"
            return status, vr.q1_result, vr.q2_result
    except Exception:
        pass

    return "error", [], []


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-validate NEQ witnesses from evaluation results.",
    )
    parser.add_argument(
        "--suite", choices=list(SUITES.keys()),
        default="leetcode",
        help="VeriEQL suite to validate (default: leetcode)",
    )
    parser.add_argument(
        "--results-dir", type=str, default=None,
        help="Path to results directory (overrides --suite)",
    )
    parser.add_argument(
        "--filter", choices=["all-neq", "our-neq-vq-equ"],
        default="all-neq",
        help="Which NEQ pairs to validate (default: all-neq)",
    )
    parser.add_argument(
        "--show", type=int, default=0, metavar="N",
        help="Show detailed witness reasoning for the first N pairs",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    results_dir = Path(args.results_dir) if args.results_dir else Path(SUITES[args.suite])
    results_path = results_dir / "results.json"
    traces_path = results_dir / "traces.jsonl"

    if not results_path.exists():
        print(f"ERROR: {results_path} not found", file=sys.stderr)
        sys.exit(1)
    if not traces_path.exists():
        print(f"ERROR: {traces_path} not found (needed for schemas)", file=sys.stderr)
        sys.exit(1)

    # Load traces for schemas
    trace_schemas: dict[int, dict] = {}
    with open(traces_path) as f:
        for line in f:
            t = json.loads(line)
            trace_schemas[t["pair_index"]] = t.get("schema", {})

    # Load results and filter
    results = json.load(open(results_path))
    if args.filter == "our-neq-vq-equ":
        pairs = [r for r in results
                 if r["our_result"] == "NEQ"
                 and r.get("verieql_result") == "EQU"
                 and r.get("witness_db")]
    else:
        pairs = [r for r in results
                 if r["our_result"] == "NEQ"
                 and r.get("witness_db")]

    print(f"Validating {len(pairs)} NEQ witnesses from {results_dir}")
    if args.filter == "our-neq-vq-equ":
        print(f"  Filter: Our NEQ where VeriEQL says EQU")
    print()

    confirmed = 0
    spurious = 0
    errors = 0
    spurious_indices = []
    error_indices = []
    shown = 0

    for i, r in enumerate(pairs):
        schema = trace_schemas.get(r["pair_index"], {})
        catalog = _catalog_from_schema(schema)
        status, q1_res, q2_res = _validate_one(
            r["sql1"], r["sql2"], r["witness_db"], catalog,
        )

        if status == "confirmed":
            confirmed += 1
        elif status == "spurious":
            spurious += 1
            spurious_indices.append(r["pair_index"])
        else:
            errors += 1
            error_indices.append(r["pair_index"])

        if shown < args.show:
            _show_detail(
                r["pair_index"], r["sql1"], r["sql2"],
                r["witness_db"], catalog, status, q1_res, q2_res,
            )
            shown += 1

        if (i + 1) % 100 == 0 and args.show == 0:
            print(f"  Progress: {i + 1}/{len(pairs)}")

    print(f"\nResults ({len(pairs)} witnesses):")
    print(f"  Confirmed (results differ): {confirmed}")
    print(f"  Spurious (results match):   {spurious}")
    print(f"  Cannot execute:             {errors}")
    print(f"  Confirmation rate:          {confirmed}/{confirmed + spurious}"
          f" = {confirmed / (confirmed + spurious) * 100:.1f}%"
          if (confirmed + spurious) > 0 else "")

    if spurious_indices:
        print(f"\n  Spurious pair indices: {spurious_indices}")
    if error_indices:
        print(f"\n  Error pair indices (first 10): {error_indices[:10]}")

    # Exit code: 0 if all confirmed, 1 if any spurious/error
    sys.exit(0 if spurious == 0 and errors == 0 else 1)


if __name__ == "__main__":
    main()
