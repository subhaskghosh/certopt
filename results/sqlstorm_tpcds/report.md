# SQLStorm Benchmark: tpcds

## Overall

| Metric | Value |
|---|---|
| Total pairs | 400 |
| Parsed | 301 (75.2%) |
| Our EQU | 270 |
| Our NEQ (witness-validated) | 1 |
| Our UNKNOWN | 30 |
| Our TMO | 56 |
| Our PARSE_FAIL | 43 |
| Total time | 1351.73s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 3379ms |
| Median time per pair | 1570ms |
| P95 time per pair | 12005ms |
| Max time per pair | 12007ms |
| Total wall time | 1351.7s |

## Configuration

```json
{
  "benchmark": "sqlstorm",
  "dataset": "tpcds",
  "k_rows": 2,
  "timeout_ms": 5000,
  "validate": true,
  "max_pairs": 400,
  "dialect": "postgres",
  "at_most_k": true
}
```