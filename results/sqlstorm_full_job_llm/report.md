# SQLStorm Benchmark: job

## Overall

| Metric | Value |
|---|---|
| Total pairs | 423 |
| Parsed | 299 (70.7%) |
| Our EQU | 120 |
| Our NEQ (witness-validated) | 7 |
| Our UNKNOWN | 172 |
| Our TMO | 95 |
| Our PARSE_FAIL | 29 |
| Total time | 2789.39s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 6594ms |
| Median time per pair | 1890ms |
| P95 time per pair | 20008ms |
| Max time per pair | 20009ms |
| Total wall time | 2789.4s |

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