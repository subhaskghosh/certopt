# SQLStorm Benchmark: stackoverflow

## Overall

| Metric | Value |
|---|---|
| Total pairs | 400 |
| Parsed | 239 (59.8%) |
| Our EQU | 207 |
| Our NEQ (witness-validated) | 0 |
| Our UNKNOWN | 32 |
| Our TMO | 110 |
| Our PARSE_FAIL | 51 |
| Total time | 2119.81s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 5299ms |
| Median time per pair | 2844ms |
| P95 time per pair | 12006ms |
| Max time per pair | 12087ms |
| Total wall time | 2119.8s |

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