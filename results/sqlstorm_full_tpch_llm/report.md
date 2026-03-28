# SQLStorm Benchmark: tpch

## Overall

| Metric | Value |
|---|---|
| Total pairs | 866 |
| Parsed | 539 (62.2%) |
| Our EQU | 245 |
| Our NEQ (witness-validated) | 1 |
| Our UNKNOWN | 293 |
| Our TMO | 298 |
| Our PARSE_FAIL | 29 |
| Total time | 8099.81s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 9353ms |
| Median time per pair | 5546ms |
| P95 time per pair | 20006ms |
| Max time per pair | 20057ms |
| Total wall time | 8099.8s |

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