# SQLStorm Benchmark: stackoverflow

## Overall

| Metric | Value |
|---|---|
| Total pairs | 8558 |
| Parsed | 6183 (72.2%) |
| Our EQU | 5087 |
| Our NEQ (witness-validated) | 0 |
| Our UNKNOWN | 1096 |
| Our TMO | 2017 |
| Our PARSE_FAIL | 358 |
| Total time | 66289.43s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 7745ms |
| Median time per pair | 3521ms |
| P95 time per pair | 20005ms |
| Max time per pair | 20108ms |
| Total wall time | 66289.4s |

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