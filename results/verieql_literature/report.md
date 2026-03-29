# VeriEQL Benchmark: literature

## Overall

| Metric | Value |
|---|---|
| Total pairs | 64 |
| Parsed | 63 (98.4%) |
| Our EQU | 30 |
| Our NEQ (witness-validated) | 18 |
| Our UNKNOWN | 15 |
| Our TMO | 0 |
| Our PARSE_FAIL | 1 |
| Both proved EQU | 29 |
| Both proved NEQ | 18 |
| Our NEQ vs VeriEQL EQU | 0 (false rej: 0.0%) |
| Speedup vs VeriEQL | 286.2× |
| Total time | 32.42s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 29 | 1 | 0 | 0 | 0 | **30** |
| **Our NEQ** | 0 | 18 | 0 | 0 | 0 | **18** |
| **Our UNKNOWN** | 5 | 10 | 0 | 0 | 0 | **15** |
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
| Mean time per pair | 506ms |
| Median time per pair | 38ms |
| P95 time per pair | 1730ms |
| Max time per pair | 9640ms |
| Total wall time | 32.4s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "literature",
  "k_rows": 3,
  "timeout_ms": 10000,
  "validate": true,
  "max_pairs": null,
  "dialect": "sqlite",
  "pair_indices": null,
  "random_sample": false,
  "seed": 42,
  "at_most_k": true
}
```