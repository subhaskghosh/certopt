# SQLStorm Benchmark: job

## Overall

| Metric | Value |
|---|---|
| Total pairs | 400 |
| Parsed | 279 (69.8%) |
| Our EQU | 175 |
| Our NEQ (witness-validated) | 14 |
| Our UNKNOWN | 90 |
| Our TMO | 113 |
| Our PARSE_FAIL | 8 |
| Total time | 1854.31s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 4635ms |
| Median time per pair | 1502ms |
| P95 time per pair | 12007ms |
| Max time per pair | 12047ms |
| Total wall time | 1854.3s |

## Configuration

```json
{
  "benchmark": "sqlstorm",
  "dataset": "job",
  "k_rows": 2,
  "timeout_ms": 5000,
  "validate": true,
  "max_pairs": 400,
  "dialect": "postgres",
  "at_most_k": true
}
```