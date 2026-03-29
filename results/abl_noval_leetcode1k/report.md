# VeriEQL Benchmark: leetcode

## Overall

| Metric | Value |
|---|---|
| Total pairs | 1000 |
| Parsed | 992 (99.2%) |
| Our EQU | 671 |
| Our NEQ (witness-validated) | 160 |
| Our UNKNOWN | 161 |
| Our TMO | 0 |
| Our PARSE_FAIL | 8 |
| Both proved EQU | 499 |
| Both proved NEQ | 93 |
| Our NEQ vs VeriEQL EQU | 6 (false rej: 1.0%) |
| Speedup vs VeriEQL | 86.6× |
| Total time | 2662.95s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 499 | 38 | 64 | 70 | 0 | **671** |
| **Our NEQ** | 6 | 93 | 13 | 48 | 0 | **160** |
| **Our UNKNOWN** | 92 | 23 | 16 | 30 | 0 | **161** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 8 | 0 | **8** |
| **Total** | **597** | **154** | **93** | **156** | **0** | **1000** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 64 | 13 | 16 | 93 |
| VeriEQL ERR | 70 | 48 | 30 | 156 |
| **Total decided** | — | — | — | **195** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 2662ms |
| Median time per pair | 244ms |
| P95 time per pair | 30114ms |
| Max time per pair | 64044ms |
| Total wall time | 2662.9s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "leetcode",
  "k_rows": 3,
  "timeout_ms": 30000,
  "validate": false,
  "max_pairs": 1000,
  "dialect": "sqlite",
  "pair_indices": null,
  "random_sample": true,
  "seed": 42,
  "at_most_k": true,
  "ignore_constraints": false
}
```