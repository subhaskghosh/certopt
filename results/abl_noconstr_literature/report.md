# VeriEQL Benchmark: literature

## Overall

| Metric | Value |
|---|---|
| Total pairs | 64 |
| Parsed | 63 (98.4%) |
| Our EQU | 28 |
| Our NEQ (witness-validated) | 20 |
| Our UNKNOWN | 15 |
| Our TMO | 0 |
| Our PARSE_FAIL | 1 |
| Both proved EQU | 27 |
| Both proved NEQ | 18 |
| Our NEQ vs VeriEQL EQU | 2 (false rej: 5.9%) |
| Speedup vs VeriEQL | 7.9× |
| Total time | 252.72s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 27 | 1 | 0 | 0 | 0 | **28** |
| **Our NEQ** | 2 | 18 | 0 | 0 | 0 | **20** |
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
| Mean time per pair | 3948ms |
| Median time per pair | 45ms |
| P95 time per pair | 1514ms |
| Max time per pair | 124114ms |
| Total wall time | 252.7s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "literature",
  "k_rows": 3,
  "timeout_ms": 30000,
  "validate": true,
  "max_pairs": null,
  "dialect": "sqlite",
  "pair_indices": null,
  "random_sample": false,
  "seed": 42,
  "at_most_k": true,
  "ignore_constraints": true
}
```