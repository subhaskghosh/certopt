# VeriEQL Benchmark: calcite

## Overall

| Metric | Value |
|---|---|
| Total pairs | 397 |
| Parsed | 397 (100.0%) |
| Our EQU | 346 |
| Our NEQ (witness-validated) | 2 |
| Our UNKNOWN | 49 |
| Our TMO | 0 |
| Our PARSE_FAIL | 0 |
| Both proved EQU | 244 |
| Both proved NEQ | 0 |
| Our NEQ vs VeriEQL EQU | 0 (false rej: 0.0%) |
| Speedup vs VeriEQL | 3155.2× |
| Total time | 29.0s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 244 | 2 | 35 | 65 | 0 | **346** |
| **Our NEQ** | 0 | 0 | 2 | 0 | 0 | **2** |
| **Our UNKNOWN** | 17 | 2 | 8 | 22 | 0 | **49** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Total** | **261** | **4** | **45** | **87** | **0** | **397** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 35 | 2 | 8 | 45 |
| VeriEQL ERR | 65 | 0 | 22 | 87 |
| **Total decided** | — | — | — | **102** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 73ms |
| Median time per pair | 38ms |
| P95 time per pair | 238ms |
| Max time per pair | 863ms |
| Total wall time | 29.0s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "calcite",
  "k_rows": 2,
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