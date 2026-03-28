# SQLStorm Benchmark: tpcds

## Overall

| Metric | Value |
|---|---|
| Total pairs | 1222 |
| Parsed | 1058 (86.6%) |
| Our EQU | 821 |
| Our NEQ (witness-validated) | 9 |
| Our UNKNOWN | 228 |
| Our TMO | 130 |
| Our PARSE_FAIL | 34 |
| Total time | 5547.6s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 4539ms |
| Median time per pair | 1475ms |
| P95 time per pair | 20005ms |
| Max time per pair | 20008ms |
| Total wall time | 5547.6s |

## Configuration

```json
{
  "benchmark": "sqlstorm",
  "dataset": "tpcds",
  "k_rows": 2,
  "timeout_ms": 10000,
  "validate": true,
  "max_pairs": null,
  "dialect": "postgres",
  "at_most_k": true
}
```