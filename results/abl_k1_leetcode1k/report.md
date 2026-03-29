# VeriEQL Benchmark: leetcode

## Overall

| Metric | Value |
|---|---|
| Total pairs | 1000 |
| Parsed | 992 (99.2%) |
| Our EQU | 686 |
| Our NEQ (witness-validated) | 112 |
| Our UNKNOWN | 194 |
| Our TMO | 0 |
| Our PARSE_FAIL | 8 |
| Both proved EQU | 506 |
| Both proved NEQ | 69 |
| Our NEQ vs VeriEQL EQU | 6 (false rej: 1.0%) |
| Speedup vs VeriEQL | 5256.2× |
| Total time | 43.85s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 506 | 45 | 68 | 67 | 0 | **686** |
| **Our NEQ** | 6 | 69 | 4 | 33 | 0 | **112** |
| **Our UNKNOWN** | 85 | 40 | 21 | 48 | 0 | **194** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 8 | 0 | **8** |
| **Total** | **597** | **154** | **93** | **156** | **0** | **1000** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 68 | 4 | 21 | 93 |
| VeriEQL ERR | 67 | 33 | 48 | 156 |
| **Total decided** | — | — | — | **172** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 43ms |
| Median time per pair | 10ms |
| P95 time per pair | 81ms |
| Max time per pair | 19221ms |
| Total wall time | 43.9s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "leetcode",
  "k_rows": 1,
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