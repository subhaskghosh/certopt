# JOB-Complex Evaluation

## Overall

| Metric | Value |
|---|---|
| Total queries | 30 |
| Improved | 27 |
| Errors | 0 |
| Avg speedup | 1.085× |
| Total time | 492.1s |

## Timing

| Metric | Value |
|---|---|
| Mean time per query | 16404ms |
| Median time per query | 4425ms |
| P95 time per query | 57431ms |
| Max time per query | 57624ms |

## Configuration

```json
{
  "benchmark": "job-complex",
  "dialect": "postgres",
  "k_rows": 3,
  "max_pairs": null,
  "data_dir": "data/JOB-Complex"
}
```