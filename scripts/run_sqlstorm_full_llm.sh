#!/bin/bash
# Run full SQLStorm LLM evaluation on the exact 11,112 queries from the
# full original run (scripts/sqlstorm_full/{ds}.jsonl).
#
#   Step 1: Generate LLM rewrites → scripts/sqlstorm_full/{ds}_llm.jsonl
#   Step 2: Evaluate with verifier → results/sqlstorm_full_{ds}_llm/
cd /Users/subhaskumarghosh/src/Query_Optimization_LLM

K=2
TIMEOUT=10000
FULL_DIR="scripts/sqlstorm_full"
LOG="logs/sqlstorm_full_llm_progress.log"
mkdir -p logs

echo "=== SQLStorm FULL LLM run started $(date) ===" >> "$LOG"

# Step 1: Generate LLM rewrites for the exact 11,112 source queries
echo "=== Step 1: Generating LLM rewrites $(date) ===" | tee -a "$LOG"
python3 -m scripts.gen_sqlstorm_full_llm \
    --concurrency 8 \
    2>&1 | tee -a "$LOG"

# Step 2: Evaluate each dataset
echo "=== Step 2: Running verifier $(date) ===" | tee -a "$LOG"

for DS in tpch tpcds stackoverflow job; do
    if [ ! -f "${FULL_DIR}/${DS}_llm.jsonl" ]; then
        echo "SKIP $DS: no LLM JSONL found" | tee -a "$LOG"
        continue
    fi
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

echo "=== ALL SQLStorm FULL LLM runs COMPLETE $(date) ===" | tee -a "$LOG"
