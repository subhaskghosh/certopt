# Spurious Witness Root Cause Analysis

## Methodology

Each spurious witness is classified by the **encoding precision gap**
it exploits in the CertOpt Z3 encoding, not by SQL surface syntax.
Root causes are verified against the actual source code of both CertOpt
(`src/optim/cegis/witness_synthesis.py`) and VeriEQL (`data/VeriEQL/`).

### Encoding Gap Families

| Gap | Description | CertOpt Source | VeriEQL Comparison |
|-----|-------------|----------------|-------------------|
| FRESH_FALLBACK | Unmodeled function/expression → unconstrained Z3 variable | L3686-3830 | Also uses uninterpreted funcs; raises NSE for OVER |
| NUMERIC_TYPE_COERCION | All numerics as RealSort; CAST=identity; rational division | L3540-3555, L3887-3901 | Similar — all values as Z3 IntSort |
| BOUNDED_K_INCOMPLETENESS | HAVING threshold exceeds k=3 row bound | L229-272 | Same bounded approach |
| DT_RESULT_TRUNCATION | Derived table results capped at bounded rows | L2494-2505 | Same — bounded tuples |
| ROW_CHOICE_NONDETERMINISM | Scalar subquery picks first row; LIMIT tie-breaks | L3774-3797, L5995-6109 | Similar abstractions |
| UNINTERP_FUNC | Unknown functions → Z3 uninterpreted functions | L3686-3704 | FRound, FSymbolicFunc |

**Total spurious pairs analyzed:** 4597

## Summary by Primary Root Cause

| Root Cause | Count | % | Description |
|-----------|------:|--:|-------------|
| NUMERIC_TYPE_COERCION | 1662 | 36.2% | RealSort arithmetic / CAST-identity / rational division |
| ROW_CHOICE_NONDETERMINISM | 1319 | 28.7% | Scalar subquery first-row / LIMIT tie-break |
| FRESH_FALLBACK | 1278 | 27.8% | Unmodeled expression → fresh Z3 variable |
| BOUNDED_K_INCOMPLETENESS | 188 | 4.1% | HAVING threshold exceeds bounded k |
| DT_RESULT_TRUNCATION | 150 | 3.3% | Derived table result capped at k rows |

## Encoding Gap Co-occurrence

Many spurious witnesses exploit multiple encoding gaps simultaneously.
This table shows the most common gap combinations.

| Gap Combination | Count | % |
|----------------|------:|--:|
| DT_RESULT_TRUNCATION+ROW_CHOICE_NONDETERMINISM | 972 | 21.1% |
| NUMERIC_TYPE_COERCION | 882 | 19.2% |
| DT_RESULT_TRUNCATION+NUMERIC_TYPE_COERCION+ROW_CHOICE_NONDETERMINISM | 687 | 14.9% |
| DT_RESULT_TRUNCATION+FRESH_FALLBACK+NUMERIC_TYPE_COERCION+ROW_CHOICE_NONDETERMINISM | 395 | 8.6% |
| DT_RESULT_TRUNCATION+FRESH_FALLBACK+ROW_CHOICE_NONDETERMINISM | 381 | 8.3% |
| ROW_CHOICE_NONDETERMINISM | 213 | 4.6% |
| BOUNDED_K_INCOMPLETENESS | 188 | 4.1% |
| DT_RESULT_TRUNCATION | 150 | 3.3% |
| FRESH_FALLBACK+NUMERIC_TYPE_COERCION | 149 | 3.2% |
| FRESH_FALLBACK | 137 | 3.0% |
| BOUNDED_K_INCOMPLETENESS+DT_RESULT_TRUNCATION+ROW_CHOICE_NONDETERMINISM | 132 | 2.9% |
| FRESH_FALLBACK+ROW_CHOICE_NONDETERMINISM | 121 | 2.6% |
| FRESH_FALLBACK+NUMERIC_TYPE_COERCION+ROW_CHOICE_NONDETERMINISM | 93 | 2.0% |
| NUMERIC_TYPE_COERCION+ROW_CHOICE_NONDETERMINISM | 93 | 2.0% |
| BOUNDED_K_INCOMPLETENESS+ROW_CHOICE_NONDETERMINISM | 2 | 0.0% |
| BOUNDED_K_INCOMPLETENESS+DT_RESULT_TRUNCATION+FRESH_FALLBACK+ROW_CHOICE_NONDETERMINISM | 2 | 0.0% |

## Breakdown by Suite

### Leetcode (4542 pairs)

| Root Cause | Count | % |
|-----------|------:|--:|
| NUMERIC_TYPE_COERCION | 1660 | 36.5% |
| ROW_CHOICE_NONDETERMINISM | 1297 | 28.6% |
| FRESH_FALLBACK | 1257 | 27.7% |
| BOUNDED_K_INCOMPLETENESS | 179 | 3.9% |
| DT_RESULT_TRUNCATION | 149 | 3.3% |

### Calcite (40 pairs)

| Root Cause | Count | % |
|-----------|------:|--:|
| FRESH_FALLBACK | 21 | 52.5% |
| ROW_CHOICE_NONDETERMINISM | 17 | 42.5% |
| NUMERIC_TYPE_COERCION | 2 | 5.0% |

### Literature (15 pairs)

| Root Cause | Count | % |
|-----------|------:|--:|
| BOUNDED_K_INCOMPLETENESS | 9 | 60.0% |
| ROW_CHOICE_NONDETERMINISM | 5 | 33.3% |
| DT_RESULT_TRUNCATION | 1 | 6.7% |

## Cross-reference with VeriEQL Verdicts

Shows how our encoding gaps correlate with VeriEQL's verdicts.
VeriEQL=EQU means VeriEQL proved equivalence (our spurious SAT is a false alarm).
VeriEQL=NSE means VeriEQL couldn't handle the SQL syntax.

### VeriEQL = EQU (2616 pairs)

| Root Cause | Count |
|-----------|------:|
| NUMERIC_TYPE_COERCION | 1301 |
| ROW_CHOICE_NONDETERMINISM | 895 |
| FRESH_FALLBACK | 340 |
| BOUNDED_K_INCOMPLETENESS | 72 |
| DT_RESULT_TRUNCATION | 8 |

### VeriEQL = ERR (866 pairs)

| Root Cause | Count |
|-----------|------:|
| FRESH_FALLBACK | 392 |
| ROW_CHOICE_NONDETERMINISM | 273 |
| DT_RESULT_TRUNCATION | 141 |
| NUMERIC_TYPE_COERCION | 46 |
| BOUNDED_K_INCOMPLETENESS | 14 |

### VeriEQL = NEQ (735 pairs)

| Root Cause | Count |
|-----------|------:|
| FRESH_FALLBACK | 303 |
| NUMERIC_TYPE_COERCION | 196 |
| ROW_CHOICE_NONDETERMINISM | 135 |
| BOUNDED_K_INCOMPLETENESS | 100 |
| DT_RESULT_TRUNCATION | 1 |

### VeriEQL = NSE (380 pairs)

| Root Cause | Count |
|-----------|------:|
| FRESH_FALLBACK | 243 |
| NUMERIC_TYPE_COERCION | 119 |
| ROW_CHOICE_NONDETERMINISM | 16 |
| BOUNDED_K_INCOMPLETENESS | 2 |

## Witness Evidence

- Pairs with non-empty witness data: 2572
  - Of which contain `__fresh__` markers: 970
- Pairs with empty/no witness data: 2025

**Fresh markers** (`__fresh_lo__`, `__fresh_hi__`) in witness values are
direct evidence that the Z3 model used an unconstrained variable.
Their presence confirms the FRESH_FALLBACK encoding gap.

## Example Pairs by Encoding Gap

### FRESH_FALLBACK

**leetcode #622**
- Q1: `SELECT PLAYER_ID,MIN(EVENT_DATE) AS FIRST_LOGIN FROM ACTIVITY GROUP BY PLAYER_ID`
- Q2: `SELECT PLAYER_ID, MIN(DATE(EVENT_DATE)) AS FIRST_LOGIN FROM ACTIVITY GROUP BY PLAYER_ID`
- Gap: DATE() not in _KNOWN_SQL_FUNCS → fresh/uninterpreted var (witness_synthesis.py:3686-3718)

**leetcode #1038**
- Q1: `SELECT PLAYER_ID, DEVICE_ID FROM ACTIVITY WHERE (PLAYER_ID, EVENT_DATE) IN ( SELECT PLAYER_ID, MIN(EVENT_DATE) FROM ACTI`
- Q2: `WITH TEMP(PID, DATE) AS ( SELECT PLAYER_ID, MIN(EVENT_DATE) FROM ACTIVITY GROUP BY PLAYER_ID ) SELECT DISTINCT T.PID AS `
- Gap: expression fallback → fresh var (witness_synthesis.py:3827-3830)
- Contributing gaps: FRESH_FALLBACK, ROW_CHOICE_NONDETERMINISM

### NUMERIC_TYPE_COERCION

**leetcode #1362**
- Q1: `SELECT ROUND((SUM(CASE WHEN (TEMP.MIN_DATE + 1) = A.EVENT_DATE THEN 1 ELSE 0 END) / COUNT(DISTINCT TEMP.PLAYER_ID)), 2) `
- Q2: `SELECT ROUND(1-AVG(ACTIVITY.PLAYER_ID IS NULL), 2) AS FRACTION FROM ACTIVITY RIGHT OUTER JOIN ( SELECT PLAYER_ID, MIN(EV`
- Gap: ROUND(rational_division) — Z3 rational vs SQL float division (witness_synthesis.py:3894, 3512-3538)
- Contributing gaps: NUMERIC_TYPE_COERCION, ROW_CHOICE_NONDETERMINISM, DT_RESULT_TRUNCATION

**leetcode #1436**
- Q1: `SELECT ROUND((SUM(CASE WHEN (TEMP.MIN_DATE + 1) = A.EVENT_DATE THEN 1 ELSE 0 END) / COUNT(DISTINCT TEMP.PLAYER_ID)), 2) `
- Q2: `WITH CTE_0 AS ( SELECT PLAYER_ID, MIN(EVENT_DATE) AS FIRST_LOGIN FROM ACTIVITY GROUP BY PLAYER_ID ) ,CTE_1 AS ( SELECT C`
- Gap: ROUND(rational_division) — Z3 rational vs SQL float division (witness_synthesis.py:3894, 3512-3538)
- Contributing gaps: NUMERIC_TYPE_COERCION, ROW_CHOICE_NONDETERMINISM, DT_RESULT_TRUNCATION

### ROW_CHOICE_NONDETERMINISM

**leetcode #2850**
- Q1: `SELECT CLASS FROM COURSES GROUP BY CLASS HAVING COUNT(*)>=5`
- Q2: `(SELECT CLASS FROM COURSES GROUP BY CLASS HAVING COUNT(DISTINCT STUDENT) >= 5)`
- Gap: scalar subquery first-row abstraction (witness_synthesis.py:3774-3797)
- Contributing gaps: BOUNDED_K_INCOMPLETENESS, ROW_CHOICE_NONDETERMINISM

**leetcode #2851**
- Q1: `SELECT CLASS FROM COURSES GROUP BY CLASS HAVING COUNT(*)>=5`
- Q2: `SELECT A.CLASS FROM (SELECT DISTINCT * FROM COURSES) A GROUP BY CLASS HAVING COUNT(STUDENT)>=5`
- Gap: scalar subquery first-row abstraction (witness_synthesis.py:3774-3797)
- Contributing gaps: BOUNDED_K_INCOMPLETENESS, ROW_CHOICE_NONDETERMINISM, DT_RESULT_TRUNCATION

### BOUNDED_K_INCOMPLETENESS

**leetcode #2852**
- Q1: `SELECT CLASS FROM COURSES GROUP BY CLASS HAVING COUNT(*)>=5`
- Q2: `SELECT CLASS FROM COURSES GROUP BY CLASS HAVING COUNT(DISTINCT STUDENT) >=5`
- Gap: HAVING COUNT threshold (>= 5) requires 5 rows, exceeds k=3

**leetcode #2853**
- Q1: `SELECT CLASS FROM COURSES GROUP BY CLASS HAVING COUNT(*)>=5`
- Q2: `SELECT CLASS FROM COURSES GROUP BY CLASS HAVING COUNT(DISTINCT(STUDENT)) >=5`
- Gap: HAVING COUNT threshold (>= 5) requires 5 rows, exceeds k=3

### DT_RESULT_TRUNCATION

**leetcode #4287**
- Q1: `SELECT X, Y, Z, IF(X+Y>Z AND X+Z>Y AND Y+Z>X, 'YES', 'NO') AS TRIANGLE FROM TRIANGLE`
- Q2: `SELECT * , CASE WHEN X+Y>Z AND Y+Z>X AND Z+X>Y THEN YES ELSE NO END AS TRIANGLE FROM TRIANGLE`
- Gap: CASE expression vs alternative formulation — compositional encoding gap

**leetcode #4288**
- Q1: `SELECT X, Y, Z, IF(X+Y>Z AND X+Z>Y AND Y+Z>X, 'YES', 'NO') AS TRIANGLE FROM TRIANGLE`
- Q2: `SELECT *, (CASE WHEN (X + Y > Z) AND (X + Z > Y) AND (Y + Z > X) THEN YES ELSE NO END) AS TRIANGLE FROM TRIANGLE`
- Gap: CASE expression vs alternative formulation — compositional encoding gap

## Key Findings

1. **Top 3 encoding gaps** (NUMERIC_TYPE_COERCION, ROW_CHOICE_NONDETERMINISM, FRESH_FALLBACK) account for **92.6%** of all spurious witnesses.

2. **FRESH_FALLBACK** is the primary mechanism: when the Z3 encoding
   encounters a function/expression it cannot model precisely, it creates
   an unconstrained variable. The solver can then pick arbitrary values
   for these variables to create a "witness" that doesn't hold under real
   SQL execution.

3. **NUMERIC_TYPE_COERCION** is the second major gap: Z3 uses exact
   rational arithmetic (RealSort) while SQL engines use typed arithmetic.
   `ROUND(a/b, 2)` in Z3 rounds the exact rational `a/b`, which may
   differ from rounding the float result of `a/b` in SQL.

4. **Both tools share similar limitations:** VeriEQL also treats ROUND as
   uninterpreted (`FRound(FUninterpretedFunction)`), raises `NotSupportedError`
   for window functions, and uses bounded tuple counts. The key difference
   is that CertOpt validates witnesses via execution (DuckDB/SQLite) and
   downgrades to UNKNOWN, maintaining soundness.

5. **BOUNDED_K_INCOMPLETENESS** is a genuine theoretical limitation:
   queries with `HAVING COUNT(*) >= 5` require k≥5 rows per group,
   but our default k=3 cannot represent such databases. This causes
   the solver to find SAT on truncated intermediate results.
