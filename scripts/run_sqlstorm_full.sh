#!/bin/bash
# Run full SQLStorm Option 1 (orig_vs_rewritten) for all 4 datasets.
# Results go to results/sqlstorm_full_* to avoid overwriting existing results.
cd /Users/subhaskumarghosh/src/Query_Optimization_LLM

SAMPLE_DIR="scripts/sqlstorm_full"
K=2
TIMEOUT=10000
LOG="logs/sqlstorm_full_progress.log"

echo "=== SQLStorm FULL run started $(date) ===" >> "$LOG"

for DS in tpch tpcds stackoverflow job; do
    echo "=== Starting $DS full run $(date) ===" >> "$LOG"
    python3 -m scripts.run_eval \
        --benchmark sqlstorm \
        --dataset "$DS" \
        --sqlstorm-source orig \
        --sample-dir "$SAMPLE_DIR" \
        --k-rows "$K" \
        --at-most-k \
        --validate \
        --timeout-ms "$TIMEOUT" \
        --output "results/sqlstorm_full_${DS}" \
        2>&1 | tee -a "$LOG"
    echo "=== Finished $DS full run $(date) ===" >> "$LOG"
done

echo "=== ALL SQLStorm FULL runs COMPLETE $(date) ===" >> "$LOG"
