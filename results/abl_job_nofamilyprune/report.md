# JOB-Complex Evaluation

## Overall

| Metric | Value |
|---|---|
| Total queries | 30 |
| Improved | 27 |
| Errors | 0 |
| Avg speedup | 1.085× |
| Total time | 494.6s |

## Timing

| Metric | Value |
|---|---|
| Mean time per query | 16486ms |
| Median time per query | 4503ms |
| P95 time per query | 58048ms |
| Max time per query | 58148ms |

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