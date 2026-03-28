# VeriEQL Benchmark: calcite

## Overall

| Metric | Value |
|---|---|
| Total pairs | 397 |
| Parsed | 397 (100.0%) |
| Our EQU | 340 |
| Our NEQ (witness-validated) | 3 |
| Our UNKNOWN | 54 |
| Our TMO | 0 |
| Our PARSE_FAIL | 0 |
| Both proved EQU | 239 |
| Both proved NEQ | 0 |
| Our NEQ vs VeriEQL EQU | 0 (false rej: 0.0%) |
| Speedup vs VeriEQL | 21.0× |
| Total time | 1904.1s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 239 | 2 | 34 | 65 | 0 | **340** |
| **Our NEQ** | 0 | 0 | 3 | 0 | 0 | **3** |
| **Our UNKNOWN** | 22 | 2 | 8 | 22 | 0 | **54** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Total** | **261** | **4** | **45** | **87** | **0** | **397** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 34 | 3 | 8 | 45 |
| VeriEQL ERR | 65 | 0 | 22 | 87 |
| **Total decided** | — | — | — | **102** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 4796ms |
| Median time per pair | 671ms |
| P95 time per pair | 31745ms |
| Max time per pair | 265514ms |
| Total wall time | 1904.1s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "calcite",
  "k_rows": 5,
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