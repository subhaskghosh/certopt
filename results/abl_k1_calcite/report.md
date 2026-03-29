# VeriEQL Benchmark: calcite

## Overall

| Metric | Value |
|---|---|
| Total pairs | 397 |
| Parsed | 397 (100.0%) |
| Our EQU | 346 |
| Our NEQ (witness-validated) | 0 |
| Our UNKNOWN | 51 |
| Our TMO | 0 |
| Our PARSE_FAIL | 0 |
| Both proved EQU | 244 |
| Both proved NEQ | 0 |
| Our NEQ vs VeriEQL EQU | 0 (false rej: 0.0%) |
| Speedup vs VeriEQL | 10945.0× |
| Total time | 8.36s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 244 | 2 | 35 | 65 | 0 | **346** |
| **Our NEQ** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our UNKNOWN** | 17 | 2 | 10 | 22 | 0 | **51** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Total** | **261** | **4** | **45** | **87** | **0** | **397** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 35 | 0 | 10 | 45 |
| VeriEQL ERR | 65 | 0 | 22 | 87 |
| **Total decided** | — | — | — | **100** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 21ms |
| Median time per pair | 8ms |
| P95 time per pair | 85ms |
| Max time per pair | 688ms |
| Total wall time | 8.4s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "calcite",
  "k_rows": 1,
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