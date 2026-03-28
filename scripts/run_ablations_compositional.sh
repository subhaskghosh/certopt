#!/bin/bash
# Compositional vs monolithic ablation at k=4 and k=5 on JOB-Complex.
# Usage: bash scripts/run_ablations_compositional.sh 2>&1 | tee -a logs/abl_compositional.log

set -euo pipefail

echo ""
echo "=========================================="
echo "COMPOSITIONAL ABLATIONS — $(date)"
echo "=========================================="

# ──────────────────────────────────────────────
# 1. Monolithic k=4
# ──────────────────────────────────────────────
echo ""
echo "=== JOB-Complex monolithic k=4 — $(date) ==="
python3 -u -m scripts.run_eval \
  --benchmark job-complex \
  --data-dir data/JOB-Complex \
  --k-rows 4 \
  --timeout-ms 30000 \
  --output results/abl_job_monolithic_k4

# ──────────────────────────────────────────────
# 2. Compositional k=4
# ──────────────────────────────────────────────
echo ""
echo "=== JOB-Complex compositional k=4 — $(date) ==="
python3 -u -m scripts.run_eval \
  --benchmark job-complex \
  --data-dir data/JOB-Complex \
  --enable-compositional \
  --k-rows 4 \
  --timeout-ms 30000 \
  --output results/abl_job_compositional_k4

# ──────────────────────────────────────────────
# 3. Monolithic k=5
# ──────────────────────────────────────────────
echo ""
echo "=== JOB-Complex monolithic k=5 — $(date) ==="
python3 -u -m scripts.run_eval \
  --benchmark job-complex \
  --data-dir data/JOB-Complex \
  --k-rows 5 \
  --timeout-ms 30000 \
  --output results/abl_job_monolithic_k5

# ──────────────────────────────────────────────
# 4. Compositional k=5
# ──────────────────────────────────────────────
echo ""
echo "=== JOB-Complex compositional k=5 — $(date) ==="
python3 -u -m scripts.run_eval \
  --benchmark job-complex \
  --data-dir data/JOB-Complex \
  --enable-compositional \
  --k-rows 5 \
  --timeout-ms 30000 \
  --output results/abl_job_compositional_k5

echo ""
echo "=========================================="
echo "COMPOSITIONAL ABLATIONS COMPLETE — $(date)"
echo "=========================================="
