#!/bin/bash
# Run all ablation studies sequentially.
# Usage: bash scripts/run_ablations.sh 2>&1 | tee logs/ablations.log

set -euo pipefail

echo "=========================================="
echo "ABLATION STUDIES — $(date)"
echo "=========================================="

# ──────────────────────────────────────────────
# A1. Effect of k (k=1, k=2, k=3) on VeriEQL
# ──────────────────────────────────────────────

echo ""
echo "=== A1: k sweep — calcite ==="
for k in 1 2 3; do
  echo "--- k=$k calcite ---"
  python3 -u -m scripts.run_eval \
    --benchmark verieql \
    --suite calcite \
    --k-rows $k \
    --validate \
    --at-most-k \
    --timeout-ms 30000 \
    --output results/abl_k${k}_calcite
done

echo ""
echo "=== A1: k sweep — literature ==="
for k in 1 2 3; do
  echo "--- k=$k literature ---"
  python3 -u -m scripts.run_eval \
    --benchmark verieql \
    --suite literature \
    --k-rows $k \
    --validate \
    --at-most-k \
    --timeout-ms 30000 \
    --output results/abl_k${k}_literature
done

echo ""
echo "=== A1: k sweep — leetcode 1K sample ==="
for k in 1 2 3; do
  echo "--- k=$k leetcode-1k ---"
  python3 -u -m scripts.run_eval \
    --benchmark verieql \
    --suite leetcode \
    --max-pairs 1000 \
    --random-sample \
    --seed 42 \
    --k-rows $k \
    --validate \
    --at-most-k \
    --timeout-ms 30000 \
    --output results/abl_k${k}_leetcode1k
done

# ──────────────────────────────────────────────
# A5. Safety: witness validation ablation
# (no --validate = accept SAT without checking)
# ──────────────────────────────────────────────

echo ""
echo "=== A5a: No witness validation — calcite ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite calcite \
  --k-rows 3 \
  --at-most-k \
  --timeout-ms 30000 \
  --output results/abl_noval_calcite

echo ""
echo "=== A5a: No witness validation — literature ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite literature \
  --k-rows 3 \
  --at-most-k \
  --timeout-ms 30000 \
  --output results/abl_noval_literature

echo ""
echo "=== A5a: No witness validation — leetcode 1K ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite leetcode \
  --max-pairs 1000 \
  --random-sample \
  --seed 42 \
  --k-rows 3 \
  --at-most-k \
  --timeout-ms 30000 \
  --output results/abl_noval_leetcode1k

# ──────────────────────────────────────────────
# A5b. Safety: ignore integrity constraints
# ──────────────────────────────────────────────

echo ""
echo "=== A5b: No constraints — calcite ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite calcite \
  --k-rows 3 \
  --validate \
  --at-most-k \
  --ignore-constraints \
  --timeout-ms 30000 \
  --output results/abl_noconstr_calcite

echo ""
echo "=== A5b: No constraints — literature ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite literature \
  --k-rows 3 \
  --validate \
  --at-most-k \
  --ignore-constraints \
  --timeout-ms 30000 \
  --output results/abl_noconstr_literature

echo ""
echo "=== A5b: No constraints — leetcode 1K ==="
python3 -u -m scripts.run_eval \
  --benchmark verieql \
  --suite leetcode \
  --max-pairs 1000 \
  --random-sample \
  --seed 42 \
  --k-rows 3 \
  --validate \
  --at-most-k \
  --ignore-constraints \
  --timeout-ms 30000 \
  --output results/abl_noconstr_leetcode1k

# ──────────────────────────────────────────────
# A2. JOB-Complex: preprocessing ablation
# ──────────────────────────────────────────────

echo ""
echo "=== A2: JOB-Complex — no preprocessing ==="
python3 -u -m scripts.run_eval \
  --benchmark job-complex \
  --data-dir data/JOB-Complex \
  --no-preprocessing \
  --k-rows 2 \
  --timeout-ms 30000 \
  --output results/abl_job_no_preprocessing

echo ""
echo "=== A2: JOB-Complex — no family pruning ==="
python3 -u -m scripts.run_eval \
  --benchmark job-complex \
  --data-dir data/JOB-Complex \
  --no-family-pruning \
  --k-rows 2 \
  --timeout-ms 30000 \
  --output results/abl_job_no_family_pruning

# ──────────────────────────────────────────────
# A3. JOB-Complex: compositional vs monolithic
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

# ──────────────────────────────────────────────
# A4. JOB-Complex: rules-only vs LLM-only vs combined
# (rules_only is the baseline from existing results)
# ──────────────────────────────────────────────

echo ""
echo "=== A4: JOB-Complex — LLM-only ==="
python3 -u -m scripts.run_eval \
  --benchmark job-complex \
  --data-dir data/JOB-Complex \
  --enable-llm \
  --ablation llm_only \
  --llm-mode smart \
  --llm-n-candidates 5 \
  --k-rows 2 \
  --timeout-ms 30000 \
  --output results/abl_job_llm_only

echo ""
echo "=== A4: JOB-Complex — rules + LLM combined ==="
python3 -u -m scripts.run_eval \
  --benchmark job-complex \
  --data-dir data/JOB-Complex \
  --enable-llm \
  --ablation full \
  --llm-mode smart \
  --llm-n-candidates 5 \
  --k-rows 2 \
  --timeout-ms 30000 \
  --output results/abl_job_full

echo ""
echo "=========================================="
echo "ALL ABLATION STUDIES COMPLETE — $(date)"
echo "=========================================="
