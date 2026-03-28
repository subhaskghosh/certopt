# VeriEQL Benchmark: leetcode

## Overall

| Metric | Value |
|---|---|
| Total pairs | 23994 |
| Parsed | 23797 (99.2%) |
| Our EQU | 15581 |
| Our NEQ (witness-validated) | 3581 |
| Our UNKNOWN | 4633 |
| Our TMO | 2 |
| Our PARSE_FAIL | 197 |
| Both proved EQU | 12042 |
| Both proved NEQ | 2072 |
| Our NEQ vs VeriEQL EQU | 241 (false rej: 1.6%) |
| Speedup vs VeriEQL | 1.2× |
| Total time | 31235.62s |

## Cross-Tabulation (Our vs VeriEQL)

|  | VQ EQU | VQ NEQ | VQ NSE | VQ ERR | VQ OTHER | Total |
|---|---|---|---|---|---|---|
| **Our EQU** | 12042 | 716 | 1298 | 1525 | 0 | **15581** |
| **Our NEQ** | 241 | 2072 | 240 | 1028 | 0 | **3581** |
| **Our UNKNOWN** | 2614 | 794 | 380 | 845 | 0 | **4633** |
| **Our TMO** | 1 | 0 | 0 | 1 | 0 | **2** |
| **Our PARSE_FAIL** | 7 | 4 | 5 | 181 | 0 | **197** |
| **Total** | **14905** | **3586** | **1923** | **3580** | **0** | **23994** |

## Novelty Coverage (VeriEQL NSE/ERR → We Decided)

| Category | Our EQU | Our NEQ | Our UNKNOWN | Total |
|---|---|---|---|---|
| VeriEQL NSE | 1298 | 240 | 380 | 1923 |
| VeriEQL ERR | 1525 | 1028 | 845 | 3580 |
| **Total decided** | — | — | — | **4091** |

## Timing

| Metric | Value |
|---|---|
| Mean time per pair | 1301ms |
| Median time per pair | 184ms |
| P95 time per pair | 10107ms |
| Max time per pair | 41658ms |
| Total wall time | 31235.6s |

## Configuration

```json
{
  "benchmark": "verieql",
  "suite": "leetcode",
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