#!/bin/bash
# Extended k sweep: k=4 and k=5 for all three suites.
# Runs after the main ablation batch finishes.
# Usage: bash scripts/run_ablations_k45.sh 2>&1 | tee -a logs/ablations.log

set -euo pipefail

echo ""
echo "=========================================="
echo "EXTENDED K SWEEP (k=4, k=5) — $(date)"
echo "=========================================="

# ──────────────────────────────────────────────
# k=4: all three suites
# ──────────────────────────────────────────────

echo ""
echo "=== k=4 — calcite ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite calcite \
  --k-rows 4 \
  --validate \
  --at-most-k \
  --timeout-ms 30000 \
  --output results/abl_k4_calcite

echo ""
echo "=== k=4 — literature ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite literature \
  --k-rows 4 \
  --validate \
  --at-most-k \
  --timeout-ms 30000 \
  --output results/abl_k4_literature

echo ""
echo "=== k=4 — leetcode 1K ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite leetcode \
  --max-pairs 1000 \
  --random-sample \
  --seed 42 \
  --k-rows 4 \
  --validate \
  --at-most-k \
  --timeout-ms 30000 \
  --output results/abl_k4_leetcode1k

# ──────────────────────────────────────────────
# k=5: all three suites
# ──────────────────────────────────────────────

echo ""
echo "=== k=5 — calcite ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite calcite \
  --k-rows 5 \
  --validate \
  --at-most-k \
  --timeout-ms 30000 \
  --output results/abl_k5_calcite

echo ""
echo "=== k=5 — literature ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite literature \
  --k-rows 5 \
  --validate \
  --at-most-k \
  --timeout-ms 30000 \
  --output results/abl_k5_literature

echo ""
echo "=== k=5 — leetcode 1K ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite leetcode \
  --max-pairs 1000 \
  --random-sample \
  --seed 42 \
  --k-rows 5 \
  --validate \
  --at-most-k \
  --timeout-ms 30000 \
  --output results/abl_k5_leetcode1k

echo ""
echo "=========================================="
echo "EXTENDED K SWEEP COMPLETE — $(date)"
echo "=========================================="
