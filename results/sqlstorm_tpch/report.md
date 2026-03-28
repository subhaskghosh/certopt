# SQLStorm Benchmark: tpch

## Overall

| Metric | Value |
|---|---|
| Total pairs | 400 |
| Parsed | 166 (41.5%) |
| Our EQU | 89 |
| Our NEQ (witness-validated) | 0 |
| Our UNKNOWN | 77 |
| Our TMO | 166 |
| Our PARSE_FAIL | 68 |
| Total time | 2466.42s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 6166ms |
| Median time per pair | 4719ms |
| P95 time per pair | 12005ms |
| Max time per pair | 12070ms |
| Total wall time | 2466.4s |

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