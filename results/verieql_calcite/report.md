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
| Both proved EQU | 247 |
| Both proved NEQ | 0 |
| Our NEQ vs VeriEQL EQU | 0 (false rej: 0.0%) |
| Speedup vs VeriEQL | 254.8× |
| Total time | 157.14s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 247 | 2 | 35 | 62 | 0 | **346** |
| **Our NEQ** | 0 | 0 | 2 | 0 | 0 | **2** |
| **Our UNKNOWN** | 14 | 2 | 8 | 25 | 0 | **49** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Total** | **261** | **4** | **45** | **87** | **0** | **397** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 35 | 2 | 8 | 45 |
| VeriEQL ERR | 62 | 0 | 25 | 87 |
| **Total decided** | — | — | — | **99** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 395ms |
| Median time per pair | 136ms |
| P95 time per pair | 1191ms |
| Max time per pair | 13634ms |
| Total wall time | 157.1s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "calcite",
  "k_rows": 3,
  "timeout_ms": 30000,
  "validate": true,
  "max_pairs": null,
  "dialect": "sqlite",
  "pair_indices": null,
  "random_sample": false,
  "seed": 42,
  "at_most_k": true
}
```