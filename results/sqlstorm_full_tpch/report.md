# SQLStorm Benchmark: tpch

## Overall

| Metric | Value |
|---|---|
| Total pairs | 887 |
| Parsed | 522 (58.9%) |
| Our EQU | 350 |
| Our NEQ (witness-validated) | 0 |
| Our UNKNOWN | 172 |
| Our TMO | 333 |
| Our PARSE_FAIL | 32 |
| Total time | 8859.95s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 9988ms |
| Median time per pair | 7664ms |
| P95 time per pair | 20005ms |
| Max time per pair | 20079ms |
| Total wall time | 8860.0s |

## Configuration

```json
{
  "benchmark": "sqlstorm",
  "dataset": "tpch",
  "k_rows": 2,
  "timeout_ms": 10000,
  "validate": true,
  "max_pairs": null,
  "dialect": "postgres",
  "at_most_k": true
}
```