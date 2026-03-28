# VeriEQL Benchmark: calcite

## Overall

| Metric | Value |
|---|---|
| Total pairs | 397 |
| Parsed | 397 (100.0%) |
| Our EQU | 320 |
| Our NEQ (witness-validated) | 40 |
| Our UNKNOWN | 37 |
| Our TMO | 0 |
| Our PARSE_FAIL | 0 |
| Both proved EQU | 230 |
| Both proved NEQ | 2 |
| Our NEQ vs VeriEQL EQU | 18 (false rej: 6.9%) |
| Speedup vs VeriEQL | 166.0× |
| Total time | 241.26s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 230 | 1 | 28 | 61 | 0 | **320** |
| **Our NEQ** | 18 | 2 | 9 | 11 | 0 | **40** |
| **Our UNKNOWN** | 13 | 1 | 8 | 15 | 0 | **37** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Total** | **261** | **4** | **45** | **87** | **0** | **397** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 28 | 9 | 8 | 45 |
| VeriEQL ERR | 61 | 11 | 15 | 87 |
| **Total decided** | — | — | — | **109** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 607ms |
| Median time per pair | 160ms |
| P95 time per pair | 2436ms |
| Max time per pair | 25808ms |
| Total wall time | 241.3s |

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
  "ignore_constraints": true
}
```