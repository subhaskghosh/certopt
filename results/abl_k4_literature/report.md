# VeriEQL Benchmark: literature

## Overall

| Metric | Value |
|---|---|
| Total pairs | 64 |
| Parsed | 63 (98.4%) |
| Our EQU | 31 |
| Our NEQ (witness-validated) | 18 |
| Our UNKNOWN | 14 |
| Our TMO | 0 |
| Our PARSE_FAIL | 1 |
| Both proved EQU | 29 |
| Both proved NEQ | 18 |
| Our NEQ vs VeriEQL EQU | 0 (false rej: 0.0%) |
| Speedup vs VeriEQL | 8.5× |
| Total time | 234.72s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 29 | 2 | 0 | 0 | 0 | **31** |
| **Our NEQ** | 0 | 18 | 0 | 0 | 0 | **18** |
| **Our UNKNOWN** | 5 | 9 | 0 | 0 | 0 | **14** |
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
| Mean time per pair | 3668ms |
| Median time per pair | 47ms |
| P95 time per pair | 4847ms |
| Max time per pair | 99741ms |
| Total wall time | 234.7s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "literature",
  "k_rows": 4,
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