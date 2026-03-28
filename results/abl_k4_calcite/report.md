# VeriEQL Benchmark: calcite

## Overall

| Metric | Value |
|---|---|
| Total pairs | 397 |
| Parsed | 397 (100.0%) |
| Our EQU | 351 |
| Our NEQ (witness-validated) | 3 |
| Our UNKNOWN | 43 |
| Our TMO | 0 |
| Our PARSE_FAIL | 0 |
| Both proved EQU | 246 |
| Both proved NEQ | 0 |
| Our NEQ vs VeriEQL EQU | 0 (false rej: 0.0%) |
| Speedup vs VeriEQL | 52.1× |
| Total time | 769.12s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 246 | 2 | 36 | 67 | 0 | **351** |
| **Our NEQ** | 0 | 0 | 3 | 0 | 0 | **3** |
| **Our UNKNOWN** | 15 | 2 | 6 | 20 | 0 | **43** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Total** | **261** | **4** | **45** | **87** | **0** | **397** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 36 | 3 | 6 | 45 |
| VeriEQL ERR | 67 | 0 | 20 | 87 |
| **Total decided** | — | — | — | **106** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 1937ms |
| Median time per pair | 287ms |
| P95 time per pair | 6252ms |
| Max time per pair | 91113ms |
| Total wall time | 769.1s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "calcite",
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