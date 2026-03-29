# VeriEQL Benchmark: leetcode

## Overall

| Metric | Value |
|---|---|
| Total pairs | 1000 |
| Parsed | 992 (99.2%) |
| Our EQU | 663 |
| Our NEQ (witness-validated) | 143 |
| Our UNKNOWN | 186 |
| Our TMO | 0 |
| Our PARSE_FAIL | 8 |
| Both proved EQU | 503 |
| Both proved NEQ | 79 |
| Our NEQ vs VeriEQL EQU | 8 (false rej: 1.3%) |
| Speedup vs VeriEQL | 259.3× |
| Total time | 888.78s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 503 | 42 | 64 | 54 | 0 | **663** |
| **Our NEQ** | 8 | 79 | 10 | 46 | 0 | **143** |
| **Our UNKNOWN** | 86 | 33 | 19 | 48 | 0 | **186** |
| **Our TMO** | 0 | 0 | 0 | 0 | 0 | **0** |
| **Our PARSE_FAIL** | 0 | 0 | 0 | 8 | 0 | **8** |
| **Total** | **597** | **154** | **93** | **156** | **0** | **1000** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 64 | 10 | 19 | 93 |
| VeriEQL ERR | 54 | 46 | 48 | 156 |
| **Total decided** | — | — | — | **174** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 888ms |
| Median time per pair | 51ms |
| P95 time per pair | 502ms |
| Max time per pair | 45788ms |
| Total wall time | 888.8s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "leetcode",
  "k_rows": 2,
  "timeout_ms": 30000,
  "validate": true,
  "max_pairs": 1000,
  "dialect": "sqlite",
  "pair_indices": null,
  "random_sample": true,
  "seed": 42,
  "at_most_k": true,
  "ignore_constraints": false
}
```