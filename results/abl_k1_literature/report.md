# VeriEQL Benchmark: literature

## Overall

| Metric | Value |
|---|---|
| Total pairs | 64 |
| Parsed | 63 (98.4%) |
| Our EQU | 28 |
| Our NEQ (witness-validated) | 10 |
| Our UNKNOWN | 25 |
| Our TMO | 0 |
| Our PARSE_FAIL | 1 |
| Both proved EQU | 24 |
| Both proved NEQ | 10 |
| Our NEQ vs VeriEQL EQU | 0 (false rej: 0.0%) |
| Speedup vs VeriEQL | 10423.8× |
| Total time | 0.89s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 24 | 4 | 0 | 0 | 0 | **28** |
| **Our NEQ** | 0 | 10 | 0 | 0 | 0 | **10** |
| **Our UNKNOWN** | 10 | 15 | 0 | 0 | 0 | **25** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 1 | 0 | **1** |
| **Total** | **34** | **29** | **0** | **1** | **0** | **64** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 0 | 0 | 0 | 0 |
| VeriEQL ERR | 0 | 0 | 0 | 1 |
| **Total decided** | — | — | — | **0** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 14ms |
| Median time per pair | 3ms |
| P95 time per pair | 17ms |
| Max time per pair | 543ms |
| Total wall time | 0.9s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "literature",
  "k_rows": 1,
  "timeout_ms": 30000,
  "validate": true,
  "max_pairs": null,
  "dialect": "sqlite",
  "pair_indices": null,
  "random_sample": false,
  "seed": 42,
  "at_most_k": true,
  "ignore_constraints": false
}
```