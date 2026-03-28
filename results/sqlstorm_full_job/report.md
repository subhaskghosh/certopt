# SQLStorm Benchmark: job

## Overall

| Metric | Value |
|---|---|
| Total pairs | 432 |
| Parsed | 309 (71.5%) |
| Our EQU | 219 |
| Our NEQ (witness-validated) | 0 |
| Our UNKNOWN | 90 |
| Our TMO | 96 |
| Our PARSE_FAIL | 27 |
| Total time | 2712.19s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 6278ms |
| Median time per pair | 1505ms |
| P95 time per pair | 20005ms |
| Max time per pair | 20009ms |
| Total wall time | 2712.2s |

## Configuration

```json
{
  "benchmark": "sqlstorm",
  "dataset": "job",
  "k_rows": 2,
  "timeout_ms": 10000,
  "validate": true,
  "max_pairs": null,
  "dialect": "postgres",
  "at_most_k": true
}
```