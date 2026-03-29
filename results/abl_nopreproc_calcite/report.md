# VeriEQL Benchmark: calcite

## Overall

| Metric | Value |
|---|---|
| Total pairs | 397 |
| Parsed | 397 (100.0%) |
| Our EQU | 353 |
| Our NEQ (witness-validated) | 2 |
| Our UNKNOWN | 42 |
| Our TMO | 0 |
| Our PARSE_FAIL | 0 |
| Both proved EQU | 250 |
| Both proved NEQ | 0 |
| Our NEQ vs VeriEQL EQU | 0 (false rej: 0.0%) |
| Speedup vs VeriEQL | 639.4× |
| Total time | 143.1s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 250 | 2 | 36 | 65 | 0 | **353** |
| **Our NEQ** | 0 | 0 | 2 | 0 | 0 | **2** |
| **Our UNKNOWN** | 11 | 2 | 7 | 22 | 0 | **42** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Total** | **261** | **4** | **45** | **87** | **0** | **397** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 36 | 2 | 7 | 45 |
| VeriEQL ERR | 65 | 0 | 22 | 87 |
| **Total decided** | — | — | — | **103** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 360ms |
| Median time per pair | 120ms |
| P95 time per pair | 1281ms |
| Max time per pair | 11904ms |
| Total wall time | 143.1s |

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
  "at_most_k": true,
  "ignore_constraints": false
}
```