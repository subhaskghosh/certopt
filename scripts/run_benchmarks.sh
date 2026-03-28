#!/bin/bash
cd /Users/subhaskumarghosh/src/Query_Optimization_LLM

echo "=== Starting Literature re-run ===" >> logs/benchmark_progress.log
python3 -m scripts.run_eval --benchmark verieql --suite literature --k-rows 3 --at-most-k --validate --timeout-ms 5000 --output results/verieql_literature 2>&1 | tee -a logs/benchmark_progress.log

echo "=== Starting LeetCode full run ===" >> logs/benchmark_progress.log
python3 -m scripts.run_eval --benchmark verieql --suite leetcode --k-rows 3 --at-most-k --validate --timeout-ms 10000 --output results/verieql_leetcode 2>&1 | tee -a logs/benchmark_progress.log

echo "=== ALL BENCHMARKS COMPLETE ===" >> logs/benchmark_progress.log
