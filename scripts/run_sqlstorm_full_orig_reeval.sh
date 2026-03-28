#!/bin/bash
# Re-evaluate full SQLStorm orig pairs with cleaned data.
cd /Users/subhaskumarghosh/src/Query_Optimization_LLM

K=2
TIMEOUT=10000
FULL_DIR="scripts/sqlstorm_full"
LOG="logs/sqlstorm_full_orig_reeval.log"
mkdir -p logs

echo "=== SQLStorm FULL Orig re-eval (cleaned data) started $(date) ===" >> "$LOG"

for DS in tpch tpcds stackoverflow job; do
    N=$(wc -l < "${FULL_DIR}/${DS}.jsonl")
    echo "=== Starting $DS orig eval ($N pairs) $(date) ===" | tee -a "$LOG"
    python3 -m scripts.run_eval \
        --benchmark sqlstorm \
        --dataset "$DS" \
        --sqlstorm-source orig \
        --sample-dir "$FULL_DIR" \
        --k-rows "$K" \
        --at-most-k \
        --validate \
        --timeout-ms "$TIMEOUT" \
        --output "results/sqlstorm_full_${DS}" \
        2>&1 | tee -a "$LOG"
    echo "=== Finished $DS orig eval $(date) ===" | tee -a "$LOG"
done

echo "=== ALL orig re-eval COMPLETE $(date) ===" | tee -a "$LOG"
