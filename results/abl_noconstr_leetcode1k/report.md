# VeriEQL Benchmark: leetcode

## Overall

| Metric | Value |
|---|---|
| Total pairs | 1000 |
| Parsed | 992 (99.2%) |
| Our EQU | 378 |
| Our NEQ (witness-validated) | 450 |
| Our UNKNOWN | 164 |
| Our TMO | 0 |
| Our PARSE_FAIL | 8 |
| Both proved EQU | 287 |
| Both proved NEQ | 97 |
| Our NEQ vs VeriEQL EQU | 216 (false rej: 36.2%) |
| Speedup vs VeriEQL | 0.7× |
| Total time | 2163.35s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 287 | 38 | 15 | 38 | 0 | **378** |
| **Our NEQ** | 216 | 97 | 60 | 77 | 0 | **450** |
| **Our UNKNOWN** | 94 | 19 | 18 | 33 | 0 | **164** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 8 | 0 | **8** |
| **Total** | **597** | **154** | **93** | **156** | **0** | **1000** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 15 | 60 | 18 | 93 |
| VeriEQL ERR | 38 | 77 | 33 | 156 |
| **Total decided** | — | — | — | **190** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 2163ms |
| Median time per pair | 171ms |
| P95 time per pair | 30048ms |
| Max time per pair | 72449ms |
| Total wall time | 2163.3s |

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
  "ignore_constraints": true
}
```