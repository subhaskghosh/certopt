# BIRD Family Pruning Ablation

## Dataset
| Metric | Value |
|---|---|
| Queries | 500 |
| Total candidates | 3071 |
| Mean candidates/query | 6.1 |
| k_rows | 2 |
| timeout_ms | 5000 |

## Results

| Metric | No Pruning | With Pruning | Savings |
|---|---|---|---|
| Solver calls | 3071 | 2056 | 1015 (33.1%) |
| Candidates pruned | 0 | 1015 | — |
| SAT (rejected) | 1464 | 678 | — |
| UNSAT (equivalent) | 547 | 467 | — |
| Unknown/timeout | 1060 | 911 | — |
| Parse errors | 0 | 0 | — |
| Solver time | 788.9s | 587.1s | 201.8s |

## Conclusion

Family pruning reduced solver calls by **33.1%** (1015 calls saved),
pruning **1015** candidates without invoking the solver.
