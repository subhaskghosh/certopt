# SQLStorm Benchmark: tpcds

## Overall

| Metric | Value |
|---|---|
| Total pairs | 400 |
| Parsed | 380 (95.0%) |
| Our EQU | 282 |
| Our NEQ (witness-validated) | 59 |
| Our UNKNOWN | 39 |
| Our TMO | 19 |
| Our PARSE_FAIL | 1 |
| Total time | 793.7s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 1984ms |
| Median time per pair | 879ms |
| P95 time per pair | 11720ms |
| Max time per pair | 12061ms |
| Total wall time | 793.7s |

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