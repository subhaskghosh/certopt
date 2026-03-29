# VeriEQL Benchmark: leetcode

## Overall

| Metric | Value |
|---|---|
| Total pairs | 1000 |
| Parsed | 992 (99.2%) |
| Our EQU | 645 |
| Our NEQ (witness-validated) | 163 |
| Our UNKNOWN | 184 |
| Our TMO | 0 |
| Our PARSE_FAIL | 8 |
| Both proved EQU | 480 |
| Both proved NEQ | 94 |
| Our NEQ vs VeriEQL EQU | 8 (false rej: 1.3%) |
| Speedup vs VeriEQL | 51.1× |
| Total time | 4511.81s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 480 | 36 | 60 | 69 | 0 | **645** |
| **Our NEQ** | 8 | 94 | 12 | 49 | 0 | **163** |
| **Our UNKNOWN** | 109 | 24 | 21 | 30 | 0 | **184** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 8 | 0 | **8** |
| **Total** | **597** | **154** | **93** | **156** | **0** | **1000** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 60 | 12 | 21 | 93 |
| VeriEQL ERR | 69 | 49 | 30 | 156 |
| **Total decided** | — | — | — | **190** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 4511ms |
| Median time per pair | 576ms |
| P95 time per pair | 30400ms |
| Max time per pair | 196798ms |
| Total wall time | 4511.8s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "leetcode",
  "k_rows": 4,
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