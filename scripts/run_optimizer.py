"""Run CEGIS optimizer on a single SQL query.

Usage:
    python3 -m scripts.run_optimizer "SELECT ..." [--catalog CATALOG] [--dialect DIALECT]
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run CEGIS optimizer on a single SQL query.",
    )
    parser.add_argument(
        "sql",
        help="SQL query to optimize",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to data/JOB-Complex for IMDB schema (optional)",
    )
    parser.add_argument(
        "--dialect",
        default="postgres",
        help="SQL dialect (default: postgres)",
    )
    parser.add_argument(
        "--k-rows",
        type=int,
        default=2,
        help="k_rows for BoundedScope (default: 2)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # --- Build catalog ---
    if args.data_dir is not None:
        from optim.eval.schema_imdb import get_imdb_catalog
        catalog = get_imdb_catalog(args.data_dir)
    else:
        from optim.schema.catalog import Catalog
        catalog = Catalog(tables={}, foreign_keys=[])

    # --- Create scope ---
    from optim.verify.encode_z3 import BoundedScope

    scope = BoundedScope(k_rows=args.k_rows)

    # --- Run optimizer ---
    from optim.optimizer.loop import optimize

    try:
        result = optimize(
            args.sql,
            catalog,
            scope=scope,
            dialect=args.dialect,
            validate_witnesses=False,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Print results ---
    print(f"\n{'='*60}")
    print(f"  Original SQL:")
    print(f"    {result.original_sql}")
    print(f"\n  Optimized SQL:")
    print(f"    {result.optimized_sql}")
    print(f"\n  Cost (original):  {result.cost_original.total_cost:.1f}")
    print(f"  Cost (optimized): {result.cost_optimized.total_cost:.1f}")
    print(f"  Speedup:          {result.speedup:.2f}×")
    print(f"  Improved:         {result.improved}")
    print(f"\n  Candidates:       {result.total_candidates}")
    print(f"  Verified:         {result.n_verified}")
    print(f"  Rejected:         {result.n_rejected}")
    print(f"\n  Solver time:      {result.solver_time_ms:.1f} ms")
    print(f"  Total time:       {result.total_time_ms:.1f} ms")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
