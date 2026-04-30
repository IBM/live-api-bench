# Query Support & Known Failures

This document classifies the SQL query patterns that currently fail translation, based on runs against `california_schools` and `superhero` BIRD benchmark datasets.

## Error Categories

### 1. Subqueries (13 instances) — High complexity

**Error:** `Can't support multiple SELECT statements in single query.`

The pipeline does not support any query containing a nested `SELECT`. Sub-patterns observed:

| Sub-pattern | Example | Sample IDs |
|---|---|---|
| Scalar subquery in WHERE | `WHERE col = (SELECT ... LIMIT 1)` | cs-8, sh-69, sh-120 |
| Correlated EXISTS | `WHERE EXISTS (SELECT 1 FROM ... WHERE outer.id = inner.id)` | sh-13, sh-21 |
| Subquery vs. aggregate | `WHERE col > (SELECT AVG(...)) * N` | sh-44, sh-128 |
| Derived table in FROM | `FROM (SELECT ... RANK() OVER ...) t WHERE t.rnk <= 5` | cs-41 |
| Subquery with aggregate in WHERE | `WHERE (col1-col2) > (SELECT AVG(col1-col2) FROM ...)` | cs-28 |
| Multi-select arithmetic | `SELECT (SELECT a FROM ...) - (SELECT b FROM ...)` | sh-73 |
| Subquery in outer WHERE with GROUP BY | `WHERE County = (SELECT County FROM ... GROUP BY ... LIMIT 1)` | cs-49, sh-26 |
| DISTINCT from subquery | `SELECT DISTINCT ... FROM (SELECT ...)` | cs-84 |

---

### 2. Arithmetic in WHERE clause (5 instances) — Medium complexity

**Error:** `Can't parse where clause nested with <class 'sqlglot.expressions.core.{Div|Add|Sub|Paren}'>`

The WHERE clause parser does not handle arithmetic operators applied directly to column references.

| Operator | Example | Sample IDs |
|---|---|---|
| Division | `WHERE CAST(col1 AS REAL) / col2 > 0.3` | cs-1, cs-12, cs-24, cs-62 |
| Addition | `WHERE col1 + col2 > 500` | cs-11 |
| Subtraction | `WHERE col1 - col2 > 30` | cs-23 |
| Parenthesized expression | `WHERE (col1 + col2 + col3) >= 1500` | cs-52 |

---

### 3. Window functions — RANK() OVER (3 instances) — High complexity

**Error:** `Didn't create the right number of getters.`

Queries using `RANK() OVER (ORDER BY ...)` or `RANK() OVER (PARTITION BY ... ORDER BY ...)` in the SELECT clause cannot be translated. No strategy exists to map window functions to API calls.

Sample IDs: sh-9, sh-11, cs-17. (cs-41 also uses a window function but is caught by the subquery check first.)

---

### 4. NEQ operator (`!=`) in WHERE (1 instance) — Low complexity

**Error:** `Unknown where operator: <class 'sqlglot.expressions.core.NEQ'>`

The WHERE operator map handles `=`, `<`, `>`, `LIKE`, etc. but not `!=`. Sample ID: sh-110.

---

### 5. Math functions in ORDER BY — ABS() (1 instance) — Low-Medium complexity

**Error:** `Column object of type <class 'sqlglot.expressions.math.Abs'> could not be processed.`

`ORDER BY ABS(col) DESC` is not recognized as a valid column expression. Sample ID: cs-82.

---

### 6. Multi-condition JOIN ON clause (1 instance) — Medium complexity

**Error:** Execution failure — malformed SQL generated for the query.

`JOIN t2 ON t1.col_a = t2.id AND t1.col_b = t2.id` — the JOIN parser only handles a single equality condition; multiple conditions in the ON clause produce malformed output SQL. Sample ID: sh-65.

---

### 7. IN clause with literal list in WHERE (1 instance) — Medium complexity

**Error:** `'NoneType' object has no attribute 'this'`

`WHERE col IN ('val1', 'val2', 'val3')` causes a NoneType crash in the WHERE parser. Sample ID: sh-81.

---

### 8. Missing column getter — sel style only (1 instance) — Medium complexity

**Error:** `'get_CharterNums'`

The sel-style builder tries to generate a `get_CharterNums` getter that is not present in the toolbox. Occurs when a column appears in SELECT alongside a window function. Sample ID: cs-17.

---

### 9. NoneType comparison during execution (1 instance) — Low complexity

**Error:** `'<' not supported between instances of 'NoneType' and 'str'`

A null value in a column being compared to a string literal (`WHERE CharterNum = '00D2'`) is not guarded against during result comparison. Sample ID: cs-63.

---

## Summary

| Category | Instances | Fix Complexity |
|---|---|---|
| Subqueries (multiple SELECT) | 13 | High |
| Arithmetic in WHERE | 5 | Medium |
| Window functions (RANK OVER) | 3 | High |
| NEQ operator (`!=`) | 1 | Low |
| ABS() in ORDER BY | 1 | Low–Medium |
| Multi-condition JOIN ON | 1 | Medium |
| IN clause with literals | 1 | Medium |
| Missing column getter (sel) | 1 | Medium |
| NoneType comparison | 1 | Low |

**Highest-impact fixes:** subquery support and arithmetic-in-WHERE together account for 18 of 27 error instances across the two datasets.
