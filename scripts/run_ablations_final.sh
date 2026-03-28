#!/bin/bash
# Final ablation runs: VeriEQL no-preprocessing + JOB compositional.
# Usage: bash scripts/run_ablations_final.sh 2>&1 | tee -a logs/ablations.log

set -euo pipefail

echo ""
echo "=========================================="
echo "FINAL ABLATIONS — $(date)"
echo "=========================================="

# ──────────────────────────────────────────────
# A2. No-preprocessing on VeriEQL calcite (397 pairs)
# ──────────────────────────────────────────────

echo ""
echo "=== A2: VeriEQL calcite — no preprocessing ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite calcite \
  --k-rows 3 \
  --validate \
  --at-most-k \
  --no-preprocessing \
  --timeout-ms 30000 \
  --output results/abl_nopreproc_calcite

# ──────────────────────────────────────────────
# A3. Compositional vs monolithic on JOB-Complex
# ──────────────────────────────────────────────

echo ""
echo "=== A3: JOB-Complex — compositional ==="
python3 -u -m scripts.run_eval \
  --benchmark job-complex \
  --data-dir data/JOB-Complex \
  --enable-compositional \
  --k-rows 2 \
  --timeout-ms 30000 \
  --output results/abl_job_compositional

echo ""
echo "=========================================="
echo "FINAL ABLATIONS COMPLETE — $(date)"
echo "=========================================="
