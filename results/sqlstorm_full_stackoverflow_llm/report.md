# SQLStorm Benchmark: stackoverflow

## Overall

| Metric | Value |
|---|---|
| Total pairs | 7679 |
| Parsed | 5770 (75.1%) |
| Our EQU | 1935 |
| Our NEQ (witness-validated) | 33 |
| Our UNKNOWN | 3802 |
| Our TMO | 1585 |
| Our PARSE_FAIL | 324 |
| Total time | 56976.8s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 7418ms |
| Median time per pair | 3570ms |
| P95 time per pair | 20008ms |
| Max time per pair | 20047ms |
| Total wall time | 56976.8s |

## Configuration

```json
{
  "benchmark": "sqlstorm",
  "dataset": "stackoverflow",
  "k_rows": 2,
  "timeout_ms": 10000,
  "validate": true,
  "max_pairs": null,
  "dialect": "postgres",
  "at_most_k": true
}
```