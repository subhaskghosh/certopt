# SQLStorm Benchmark: stackoverflow

## Overall

| Metric | Value |
|---|---|
| Total pairs | 400 |
| Parsed | 313 (78.2%) |
| Our EQU | 244 |
| Our NEQ (witness-validated) | 3 |
| Our UNKNOWN | 66 |
| Our TMO | 87 |
| Our PARSE_FAIL | 0 |
| Total time | 1896.57s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 4741ms |
| Median time per pair | 2548ms |
| P95 time per pair | 12005ms |
| Max time per pair | 12042ms |
| Total wall time | 1896.6s |

## Configuration

```json
{
  "benchmark": "sqlstorm",
  "dataset": "stackoverflow",
  "k_rows": 2,
  "timeout_ms": 5000,
  "validate": true,
  "max_pairs": 400,
  "dialect": "postgres",
  "at_most_k": true
}
```