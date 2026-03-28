# SQLStorm Benchmark: tpch

## Overall

| Metric | Value |
|---|---|
| Total pairs | 400 |
| Parsed | 333 (83.2%) |
| Our EQU | 254 |
| Our NEQ (witness-validated) | 5 |
| Our UNKNOWN | 74 |
| Our TMO | 66 |
| Our PARSE_FAIL | 1 |
| Total time | 1706.38s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 4265ms |
| Median time per pair | 2426ms |
| P95 time per pair | 12005ms |
| Max time per pair | 12048ms |
| Total wall time | 1706.4s |

## Configuration

```json
{
  "benchmark": "sqlstorm",
  "dataset": "tpch",
  "k_rows": 2,
  "timeout_ms": 5000,
  "validate": true,
  "max_pairs": 400,
  "dialect": "postgres",
  "at_most_k": true
}
```