# VeriEQL Benchmark: leetcode

## Overall

| Metric | Value |
|---|---|
| Total pairs | 1000 |
| Parsed | 992 (99.2%) |
| Our EQU | 672 |
| Our NEQ (witness-validated) | 161 |
| Our UNKNOWN | 159 |
| Our TMO | 0 |
| Our PARSE_FAIL | 8 |
| Both proved EQU | 498 |
| Both proved NEQ | 93 |
| Our NEQ vs VeriEQL EQU | 7 (false rej: 1.2%) |
| Speedup vs VeriEQL | 95.6× |
| Total time | 2410.5s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 498 | 38 | 66 | 70 | 0 | **672** |
| **Our NEQ** | 7 | 93 | 13 | 48 | 0 | **161** |
| **Our UNKNOWN** | 92 | 23 | 14 | 30 | 0 | **159** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 8 | 0 | **8** |
| **Total** | **597** | **154** | **93** | **156** | **0** | **1000** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 66 | 13 | 14 | 93 |
| VeriEQL ERR | 70 | 48 | 30 | 156 |
| **Total decided** | — | — | — | **197** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 2410ms |
| Median time per pair | 183ms |
| P95 time per pair | 30087ms |
| Max time per pair | 43131ms |
| Total wall time | 2410.5s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "leetcode",
  "k_rows": 3,
  "timeout_ms": 30000,
  "validate": true,
  "max_pairs": 1000,
  "dialect": "sqlite",
  "pair_indices": null,
  "random_sample": true,
  "seed": 42,
  "at_most_k": true,
  "ignore_constraints": false
}
```