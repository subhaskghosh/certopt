# JOB-Complex Evaluation

## Overall

| Metric | Value |
|---|---|
| Total queries | 30 |
| Improved | 27 |
| Errors | 0 |
| Avg speedup | 1.085× |
| Total time | 496.8s |

## Timing

| Metric | Value |
|---|---|
| Mean time per query | 16560ms |
| Median time per query | 4390ms |
| P95 time per query | 58391ms |
| Max time per query | 58606ms |

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