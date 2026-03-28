# VeriEQL Benchmark: leetcode

## Overall

| Metric | Value |
|---|---|
| Total pairs | 1000 |
| Parsed | 992 (99.2%) |
| Our EQU | 619 |
| Our NEQ (witness-validated) | 170 |
| Our UNKNOWN | 203 |
| Our TMO | 0 |
| Our PARSE_FAIL | 8 |
| Both proved EQU | 469 |
| Both proved NEQ | 98 |
| Our NEQ vs VeriEQL EQU | 7 (false rej: 1.2%) |
| Speedup vs VeriEQL | 0.2× |
| Total time | 7020.81s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 469 | 32 | 54 | 64 | 0 | **619** |
| **Our NEQ** | 7 | 98 | 13 | 52 | 0 | **170** |
| **Our UNKNOWN** | 121 | 24 | 26 | 32 | 0 | **203** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 8 | 0 | **8** |
| **Total** | **597** | **154** | **93** | **156** | **0** | **1000** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 54 | 13 | 26 | 93 |
| VeriEQL ERR | 64 | 52 | 32 | 156 |
| **Total decided** | — | — | — | **183** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 7020ms |
| Median time per pair | 1439ms |
| P95 time per pair | 34486ms |
| Max time per pair | 193576ms |
| Total wall time | 7020.8s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "leetcode",
  "k_rows": 5,
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