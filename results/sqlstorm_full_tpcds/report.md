# SQLStorm Benchmark: tpcds

## Overall

| Metric | Value |
|---|---|
| Total pairs | 1234 |
| Parsed | 1074 (87.0%) |
| Our EQU | 968 |
| Our NEQ (witness-validated) | 7 |
| Our UNKNOWN | 99 |
| Our TMO | 109 |
| Our PARSE_FAIL | 51 |
| Total time | 5190.36s |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 4206ms |
| Median time per pair | 1304ms |
| P95 time per pair | 20004ms |
| Max time per pair | 20007ms |
| Total wall time | 5190.4s |

## Configuration

```json
{
  "benchmark": "sqlstorm",
  "dataset": "tpcds",
  "k_rows": 2,
  "timeout_ms": 10000,
  "validate": true,
  "max_pairs": null,
  "dialect": "postgres",
  "at_most_k": true
}
```