# SQLStorm Benchmark: job

## Overall

| Metric | Value |
|---|---|
| Total pairs | 400 |
| Parsed | 66 (16.5%) |
| Our EQU | 51 |
| Our NEQ (witness-validated) | 0 |
| Our UNKNOWN | 15 |
| Our TMO | 18 |
| Our PARSE_FAIL | 316 |
| Total time | 472.9s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 1182ms |
| Median time per pair | 375ms |
| P95 time per pair | 10315ms |
| Max time per pair | 12035ms |
| Total wall time | 472.9s |

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