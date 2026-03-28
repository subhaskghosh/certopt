#!/bin/bash
cd /Users/subhaskumarghosh/src/Query_Optimization_LLM

K=2
TIMEOUT=10000
FULL_DIR="scripts/sqlstorm_full"
LOG="logs/sqlstorm_full_llm_reeval.log"
mkdir -p logs

echo "=== SQLStorm FULL LLM re-eval (cleaned data) started $(date) ===" >> "$LOG"

for DS in tpch tpcds stackoverflow job; do
    N=$(wc -l < "${FULL_DIR}/${DS}_llm.jsonl")
    echo "=== Starting $DS LLM eval ($N pairs) $(date) ===" | tee -a "$LOG"
    python3 -m scripts.run_eval \
        --benchmark sqlstorm \
        --dataset "$DS" \
        --sqlstorm-source llm \
        --sample-dir "$FULL_DIR" \
        --k-rows "$K" \
        --at-most-k \
        --validate \
        --timeout-ms "$TIMEOUT" \
        --output "results/sqlstorm_full_${DS}_llm" \
        2>&1 | tee -a "$LOG"
    echo "=== Finished $DS LLM eval $(date) ===" | tee -a "$LOG"
done

echo "=== ALL re-eval COMPLETE $(date) ===" | tee -a "$LOG"

# Chain: run orig re-eval after LLM re-eval
echo "=== Chaining orig re-eval ===" | tee -a "$LOG"
bash scripts/run_sqlstorm_full_orig_reeval.sh
