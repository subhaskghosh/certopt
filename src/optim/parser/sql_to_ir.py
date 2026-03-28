"""Parse SQL strings into QueryIR using sqlglot.

Handles the V1+V2 SQL fragment:
  SELECT / FROM / JOIN / WHERE / GROUP BY / HAVING / ORDER BY / LIMIT
  + aggregates (COUNT/SUM/AVG/MIN/MAX) + DISTINCT
  + derived tables: FROM (SELECT ...) AS alias
  + uncorrelated scalar subqueries: WHERE col = (SELECT MAX(x) FROM t2)
  + uncorrelated IN subqueries: WHERE col IN (SELECT x FROM t2)

Unsupported constructs (correlated subqueries, CTEs, window functions, UNION)
return None with a reason string.
"""
# pyright: reportAttributeAccessIssue=false, reportArgumentType=false

from __future__ import annotations

import datetime
import logging
import re
from typing import Optional

import sqlglot
from sqlglot import expressions as sge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# E.5 — Date normalization helpers
# ---------------------------------------------------------------------------


def _date_to_epoch_day(date_str: str) -> Optional[int]:
    """Convert a DATE literal string ('YYYY-MM-DD') to epoch-day integer.

    Returns the number of days since 1970-01-01, or None if *date_str*
    is not a valid ISO-format date.
    """
    try:
        d = datetime.date.fromisoformat(date_str)
        return (d - datetime.date(1970, 1, 1)).days
    except (ValueError, TypeError):
        return None

from ..ir.types import (
    AggCall,
    AggFunc,
    Between,
    BinOp,
    BinOpKind,
    ColumnRef,
    DerivedTable,
    ExistsSubquery,
    ExprUnion,
    FuncCall,
    InList,
    InSubquery,
    JoinClause,
    JoinType,
    Literal,
    QueryIR,
    RelRef,
    ScalarSubquery,
    SetOpKind,
    SortDir,
    SortSpec,
    Star,
    UnaryOp,
    UnaryOpKind,
    CaseExpr,
    CaseWhen,
    WindowFunc,
)

# ---------------------------------------------------------------------------
# NATURAL JOIN desugaring
# ---------------------------------------------------------------------------


def _desugar_natural_joins(
    stmt: sge.Expression, catalog
) -> sge.Expression:
    """Replace NATURAL JOIN with JOIN ... USING(common_columns).

    sqlglot represents NATURAL joins with ``join.method == 'NATURAL'``.
    We find common column names between the left-side tables and the right
    table using the *catalog*, then rewrite the join with a USING clause.

    If *catalog* is None or the tables cannot be resolved, NATURAL joins are
    left untouched (they will fail later with "JOIN without ON clause").
    """
    if catalog is None:
        return stmt

    joins = list(stmt.find_all(sge.Join))
    has_natural = any(
        (j.args.get("method") or "").upper() == "NATURAL" for j in joins
    )
    if not has_natural:
        return stmt

    stmt = stmt.copy()

    for join_node in list(stmt.find_all(sge.Join)):
        method = (join_node.args.get("method") or "").upper()
        if method != "NATURAL":
            continue

        # Find the right table name
        right = join_node.this
        if isinstance(right, sge.Table):
            right_table_name = right.name
        else:
            continue

        right_info = catalog.get_table(right_table_name)
        if right_info is None:
            continue
        right_cols = {c.name.lower() for c in right_info.columns}

        # Collect left-side column names from FROM and preceding JOINs
        # Walk up to find the enclosing Select
        parent_select = join_node.find_ancestor(sge.Select)
        if parent_select is None:
            continue

        left_cols: set[str] = set()
        from_clause = parent_select.args.get("from")
        if from_clause:
            from_table = from_clause.this
            if isinstance(from_table, sge.Table):
                from_info = catalog.get_table(from_table.name)
                if from_info:
                    left_cols.update(c.name.lower() for c in from_info.columns)

        # Also include columns from preceding joins (for multi-join scenarios)
        for prev_join in parent_select.args.get("joins") or []:
            if prev_join is join_node:
                break
            prev_table = prev_join.this
            if isinstance(prev_table, sge.Table):
                prev_info = catalog.get_table(prev_table.name)
                if prev_info:
                    left_cols.update(c.name.lower() for c in prev_info.columns)

        common = sorted(right_cols & left_cols)
        if not common:
            continue

        # Remove the NATURAL method and add USING
        join_node.args.pop("method", None)
        using_cols = [sge.to_identifier(c) for c in common]
        join_node.set("using", using_cols)

    return stmt


# ---------------------------------------------------------------------------
# FIX.36c / FIX.36d — SQL preprocessing helpers
# ---------------------------------------------------------------------------


def _preprocess_bare_union(sql: str) -> str:
    """FIX.36c: Rewrite bare UNION in FROM clauses to proper subqueries.

    Textbook SQL like '(R UNION ALL S) X' is not valid in most dialects.
    Rewrite to '(SELECT * FROM R UNION ALL SELECT * FROM S) X'.
    """
    def _replace(m: re.Match) -> str:
        t1 = m.group(1)
        union_kw = m.group(2)  # 'UNION ALL' or 'UNION'
        t2 = m.group(3)
        return f"(SELECT * FROM {t1} {union_kw} SELECT * FROM {t2})"

    return re.sub(
        r'\(\s*(\w+)\s+(UNION\s+ALL|UNION)\s+(\w+)\s*\)',
        _replace,
        sql,
        flags=re.IGNORECASE,
    )


def _preprocess_comma_join_on(sql: str) -> str:
    """FIX.36d: Rewrite 'FROM A, B ... ON' to 'FROM A JOIN B ... ON'.

    Nonstandard SQL like 'FROM A, (subquery) AS X ON ...' should be
    'FROM A JOIN (subquery) AS X ON ...'.
    """
    result = sql
    # Subquery with AS alias followed by ON
    result = re.sub(
        r',(\s*\([^)]*(?:\([^)]*\))*[^)]*\)\s+AS\s+\w+\s+ON\b)',
        r' JOIN\1',
        result,
        flags=re.IGNORECASE,
    )
    # Table with AS alias followed by ON
    result = re.sub(
        r',(\s+\w+\s+AS\s+\w+\s+ON\b)',
        r' JOIN\1',
        result,
        flags=re.IGNORECASE,
    )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sql_to_ir(
    sql: str, dialect: str = "sqlite", catalog=None,
) -> tuple[Optional[QueryIR], Optional[str]]:
    """Parse a SQL string into a QueryIR.

    Args:
        sql: The SQL string to parse.
        dialect: The SQL dialect for parsing.
        catalog: Optional schema catalog used to resolve NATURAL JOINs.

    Returns:
        (ir, error) — ir is the parsed QueryIR or None if parsing fails.
        error is a string describing why parsing failed, or None on success.
    """
    # FIX.36c: Rewrite bare UNION in FROM clauses
    sql = _preprocess_bare_union(sql)
    # FIX.36d: Rewrite comma-join with ON clause to proper JOIN syntax
    sql = _preprocess_comma_join_on(sql)

    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as e:
        # Fallback: try alternate dialect if primary fails
        alt = "mysql" if dialect != "mysql" else "sqlite"
        try:
            parsed = sqlglot.parse(sql, read=alt)
        except Exception:
            return None, f"Parse error: {e}"

    if not parsed or parsed[0] is None:
        return None, "Empty parse result"

    stmt = parsed[0]

    # Desugar NATURAL JOINs into JOIN ... USING before conversion
    stmt = _desugar_natural_joins(stmt, catalog)

    # FIX.28a: Inline CTEs at the top level before dispatching.
    # When a CTE wraps a UNION/INTERSECT/EXCEPT, sqlglot attaches
    # the WITH clause on the set-op node, not on the inner Select.
    # _convert_set_op never called _inline_ctes, leaving CTE table
    # references unresolved.
    if isinstance(stmt, (sge.Union, sge.Intersect, sge.Except)):
        with_node = stmt.args.get("with")
        if with_node:
            stmt = _inline_ctes_setop(stmt, with_node)
        return _convert_set_op(stmt)

    # Handle top-level VALUES clause (FIX.5)
    if isinstance(stmt, sge.Values):
        return _convert_values(stmt)

    if not isinstance(stmt, sge.Select):
        # Try to find the Select within the statement
        selects = list(stmt.find_all(sge.Select))
        if not selects:
            return None, f"Not a SELECT statement: {type(stmt).__name__}"
        stmt = selects[0]

    return _convert_select(stmt)


# ---------------------------------------------------------------------------
# Set operation conversion
# ---------------------------------------------------------------------------

_SET_OP_MAP: dict[type[sge.Expression], SetOpKind] = {
    sge.Intersect: SetOpKind.INTERSECT,
    sge.Except: SetOpKind.EXCEPT,
}


def _convert_set_op(
    node: sge.Expression,
) -> tuple[Optional[QueryIR], Optional[str]]:
    """Convert a sqlglot UNION/INTERSECT/EXCEPT node to QueryIR with set_op fields."""
    left_node = node.this
    right_node = node.expression

    # Unwrap Subquery wrappers (sqlglot sometimes wraps set-op operands)
    if isinstance(left_node, sge.Subquery):
        left_node = left_node.this
    if isinstance(right_node, sge.Subquery):
        right_node = right_node.this

    # Determine set operation kind
    if isinstance(node, sge.Union):
        is_distinct = node.args.get("distinct")
        op_kind = SetOpKind.UNION if is_distinct else SetOpKind.UNION_ALL
    else:
        op_kind = _SET_OP_MAP.get(type(node))
        if op_kind is None:
            return None, f"Unsupported set operation: {type(node).__name__}"

    # Recursively convert left (may itself be a set operation)
    if isinstance(left_node, (sge.Union, sge.Intersect, sge.Except)):
        left_ir, left_err = _convert_set_op(left_node)
    elif isinstance(left_node, sge.Select):
        left_ir, left_err = _convert_select(left_node)
    else:
        return None, f"Unexpected left operand in set operation: {type(left_node).__name__}"
    if left_ir is None:
        return None, left_err

    # Convert right side
    if isinstance(right_node, (sge.Union, sge.Intersect, sge.Except)):
        right_ir, right_err = _convert_set_op(right_node)
    elif isinstance(right_node, sge.Select):
        right_ir, right_err = _convert_select(right_node)
    else:
        return None, f"Unexpected right operand in set operation: {type(right_node).__name__}"
    if right_ir is None:
        return None, right_err

    # Attach set operation at the END of the left IR's set-op chain.
    # For chained set ops like (A UNION B) UNION C, left_ir is already
    # A_ir with set_op/set_right pointing to B_ir.  We must walk to
    # the rightmost leaf (B_ir) and append C there.
    tail = left_ir
    while tail.set_right is not None:
        tail = tail.set_right
    tail.set_op = op_kind
    tail.set_right = right_ir
    return left_ir, None


# ---------------------------------------------------------------------------
# Unsupported-construct guard
# ---------------------------------------------------------------------------

_UNSUPPORTED: list[tuple[type[sge.Expression], str]] = [
]


def _check_unsupported(stmt: sge.Select) -> Optional[str]:
    """Return a reason string if *stmt* contains unsupported constructs."""
    for cls, label in _UNSUPPORTED:
        if stmt.find(cls):
            return f"Unsupported: {label}"
    return None


# ---------------------------------------------------------------------------
# CTE inlining
# ---------------------------------------------------------------------------


def _inline_ctes(stmt: sge.Select) -> sge.Select:
    """Inline CTEs by replacing CTE references with subqueries.

    Transforms: WITH cte AS (SELECT ...) SELECT ... FROM cte
    Into: SELECT ... FROM (SELECT ...) AS cte

    Handles inter-CTE references: CTE2 may reference CTE1, so we inline
    earlier CTEs into later CTE bodies before building the final map.
    """
    with_node = stmt.args.get("with")
    if not with_node:
        return stmt

    # Build CTE map with inter-CTE resolution
    cte_map: dict[str, sge.Expression] = {}
    for cte in with_node.expressions:
        if not isinstance(cte, sge.CTE):
            continue
        cte_body = cte.this.copy()
        # Replace references to earlier CTEs within this CTE body
        for table_node in list(cte_body.find_all(sge.Table)):
            if table_node.name in cte_map:
                earlier = cte_map[table_node.name].copy()
                subquery = sge.Subquery(this=earlier, alias=sge.to_identifier(table_node.name))
                table_node.replace(subquery)
        cte_map[cte.alias] = cte_body

    # Remove the WITH clause from the statement
    stmt_copy = stmt.copy()
    stmt_copy.args.pop("with", None)

    # Replace table references that match CTE names with subqueries.
    # FIX.37c: Preserve the outer alias when available (e.g., CTE T2 →
    # alias should be T2, not CTE) so that ON/WHERE refs resolve correctly.
    for table_node in list(stmt_copy.find_all(sge.Table)):
        table_name = table_node.name
        if table_name in cte_map:
            cte_select = cte_map[table_name].copy()
            effective_alias = table_node.alias or table_name
            subquery = sge.Subquery(this=cte_select, alias=sge.to_identifier(effective_alias))
            table_node.replace(subquery)

    return stmt_copy


def _inline_ctes_setop(stmt: sge.Expression, with_node: sge.With) -> sge.Expression:
    """Inline CTEs in a top-level set-op node (Union/Intersect/Except).

    FIX.28a: When WITH wraps a UNION, sqlglot places the WITH on the
    Union node.  We build the CTE map and replace Table refs throughout
    the entire set-op tree, then strip the WITH clause.

    Handles inter-CTE references: CTE2 may reference CTE1, so we inline
    earlier CTEs into later CTE bodies before building the final map.
    """
    # Build CTE map with inter-CTE resolution: inline earlier CTEs
    # into later CTE bodies so that CTE2 referencing CTE1 works.
    cte_map: dict[str, sge.Expression] = {}
    for cte in with_node.expressions:
        if not isinstance(cte, sge.CTE):
            continue
        cte_body = cte.this.copy()
        # Replace references to earlier CTEs within this CTE body
        for table_node in list(cte_body.find_all(sge.Table)):
            if table_node.name in cte_map:
                earlier = cte_map[table_node.name].copy()
                subquery = sge.Subquery(this=earlier, alias=sge.to_identifier(table_node.name))
                table_node.replace(subquery)
        cte_map[cte.alias] = cte_body

    if not cte_map:
        return stmt

    stmt_copy = stmt.copy()
    stmt_copy.args.pop("with", None)

    for table_node in list(stmt_copy.find_all(sge.Table)):
        table_name = table_node.name
        if table_name in cte_map:
            cte_select = cte_map[table_name].copy()
            effective_alias = table_node.alias or table_name
            subquery = sge.Subquery(this=cte_select, alias=sge.to_identifier(effective_alias))
            table_node.replace(subquery)

    return stmt_copy


# ---------------------------------------------------------------------------
# VALUES lowering (FIX.5)
# ---------------------------------------------------------------------------


def _convert_values(
    node: sge.Values,
) -> tuple[Optional[QueryIR], Optional[str]]:
    """Lower VALUES (r1), (r2), ... into a UNION ALL chain of constant SELECTs.

    Each row becomes: SELECT lit1 AS c0, lit2 AS c1, ... FROM __values_dual__
    Rows are chained via set_right / set_op = UNION_ALL.
    """
    rows = node.expressions
    if not rows:
        return None, "Empty VALUES clause"

    head_ir: Optional[QueryIR] = None
    prev_ir: Optional[QueryIR] = None

    for row_node in rows:
        # Each row is a Tuple node with .expressions
        if isinstance(row_node, sge.Tuple):
            vals = row_node.expressions
        else:
            vals = [row_node]

        select_exprs: list[ExprUnion] = []
        for idx, v in enumerate(vals):
            expr = _convert_expr(v)
            if expr is None:
                return None, f"Cannot convert VALUES element: {v}"
            expr.alias = f"c{idx}"
            select_exprs.append(expr)

        row_ir = QueryIR(
            select=select_exprs,
            from_table=RelRef(table="__values_dual__"),
        )

        if head_ir is None:
            head_ir = row_ir
        else:
            assert prev_ir is not None
            prev_ir.set_op = SetOpKind.UNION_ALL
            prev_ir.set_right = row_ir
        prev_ir = row_ir

    return head_ir, None


# ---------------------------------------------------------------------------
# Top-level SELECT conversion
# ---------------------------------------------------------------------------


def _convert_select(
    stmt: sge.Select,
    outer_aliases: frozenset[str] | None = None,
) -> tuple[Optional[QueryIR], Optional[str]]:
    """Convert a sqlglot Select to QueryIR."""
    unsupported = _check_unsupported(stmt)
    if unsupported:
        return None, unsupported

    # Handle CTEs: rewrite as derived tables
    with_node = stmt.find(sge.With)
    if with_node:
        # Inline CTEs as derived tables by removing the WITH clause
        # and substituting CTE references in the main query
        stmt = _inline_ctes(stmt)

    # FROM — use stmt.args.get to avoid leaking inner subquery clauses
    from_clause = stmt.args.get("from")
    if from_clause is None:
        # SELECT expr without FROM (e.g., SELECT ROUND(...)) — use dual table
        from_table: RelRef | DerivedTable = RelRef(table="__values_dual__")
    else:
        from_table, from_err = _convert_from(from_clause, outer_aliases=outer_aliases)
        if from_table is None:
            return None, from_err

    # Collect table aliases visible at this scope (for correlation detection)
    scope_aliases: set[str] = set()
    if isinstance(from_table, RelRef):
        scope_aliases.add(from_table.alias or from_table.table)
    elif isinstance(from_table, DerivedTable):
        scope_aliases.add(from_table.alias)

    # JOINs — use stmt.args.get("joins") to avoid leaking inner subquery joins
    joins: list[JoinClause] = []
    current_aliases = frozenset(scope_aliases) | (outer_aliases or frozenset())
    for join_node in (stmt.args.get("joins") or []):
        jc, jerr = _convert_join(join_node, outer_aliases=current_aliases)
        if jc is None:
            return None, jerr
        joins.append(jc)
        if isinstance(jc.right, RelRef):
            scope_aliases.add(jc.right.alias or jc.right.table)
        elif isinstance(jc.right, DerivedTable):
            scope_aliases.add(jc.right.alias)

    # SELECT expressions
    select_exprs: list[ExprUnion] = []
    for expr in stmt.expressions:
        alias: Optional[str] = None
        actual_expr = expr
        if isinstance(expr, sge.Alias):
            alias = expr.alias
            actual_expr = expr.this

        converted = _convert_expr(actual_expr)
        if converted is None:
            return None, f"Cannot convert SELECT expression: {expr}"
        if alias:
            converted.alias = alias
        select_exprs.append(converted)

    if not select_exprs:
        select_exprs = [Star()]

    # WHERE — FIX.15b: use args.get to avoid leaking inner subquery WHERE
    where_expr: Optional[ExprUnion] = None
    where_clause = stmt.args.get("where")
    if where_clause:
        where_expr = _convert_expr(where_clause.this)
        if where_expr is None:
            return None, f"Cannot convert WHERE expression: {where_clause.this}"

    # GROUP BY — intercept ROLLUP/GROUPING SETS and lower to UNION ALL
    # FIX.15b: use args.get to avoid leaking inner subquery GROUP BY
    group_by: list[ExprUnion] = []
    group_node = stmt.args.get("group")
    if group_node:
        rollup_nodes = group_node.args.get("rollup")
        gs_nodes = group_node.args.get("grouping_sets")
        if rollup_nodes:
            return _lower_rollup(stmt, rollup_nodes)
        if gs_nodes:
            return _lower_grouping_sets(stmt, gs_nodes)
        # Reject CUBE (not lowered yet)
        if group_node.args.get("cube"):
            return None, "Unsupported GROUP BY construct: CUBE"
        for gb_expr in group_node.expressions:
            # Resolve positional GROUP BY (e.g., GROUP BY 1, 2)
            if isinstance(gb_expr, sge.Literal) and gb_expr.is_number:
                try:
                    pos = int(gb_expr.this)
                    if 1 <= pos <= len(select_exprs):
                        group_by.append(select_exprs[pos - 1])
                        continue
                except (ValueError, TypeError):
                    pass
            converted = _convert_expr(gb_expr)
            if converted:
                group_by.append(converted)

    # HAVING — FIX.15b: use args.get to avoid leaking inner subquery HAVING
    # FIX.23: resolve SELECT alias references in HAVING (MySQL extension)
    having_expr: Optional[ExprUnion] = None
    having_node = stmt.args.get("having")
    if having_node:
        # Build alias map from SELECT expressions
        alias_map: dict[str, ExprUnion] = {}
        for se in select_exprs:
            if se.alias:
                alias_map[se.alias.lower()] = se
        having_expr = _convert_expr(having_node.this)
        if having_expr is None:
            return None, f"Cannot convert HAVING expression: {having_node.this}"
        if alias_map:
            having_expr = _resolve_aliases(having_expr, alias_map)

    # ORDER BY — FIX.15b: use args.get to avoid leaking inner subquery ORDER BY
    order_by: list[SortSpec] = []
    order_node = stmt.args.get("order")
    if order_node:
        for sort_expr in order_node.expressions:
            converted_sort = _convert_sort(sort_expr)
            if converted_sort:
                order_by.append(converted_sort)

    # LIMIT — FIX.15b: use args.get to avoid leaking inner subquery LIMIT
    # FIX.16a: handle FETCH NEXT N ROWS ONLY (sqlglot Fetch node in limit slot)
    limit_val: Optional[int] = None
    limit_node = stmt.args.get("limit")
    if limit_node:
        if isinstance(limit_node, sge.Fetch):
            # FETCH NEXT N ROWS ONLY → count is in args["count"]
            count_node = limit_node.args.get("count")
            if count_node is not None:
                try:
                    limit_val = int(count_node.this)
                except (ValueError, TypeError, AttributeError):
                    logger.debug("Failed to parse FETCH count: %r", count_node)
        else:
            limit_expr = limit_node.expression
            if limit_expr is not None:
                try:
                    limit_val = int(limit_expr.this)
                except (ValueError, TypeError, AttributeError):
                    logger.debug("Failed to parse LIMIT value: %r", limit_expr)

    # DISTINCT
    is_distinct = stmt.args.get("distinct") is not None

    # FIX.24b: MySQL extension — HAVING without GROUP BY acts as WHERE.
    # When there's no GROUP BY and the HAVING clause contains no aggregates,
    # move HAVING to WHERE (ANDed with existing WHERE if any).
    if having_expr is not None and not group_by:
        def _has_agg(e):
            if isinstance(e, AggCall):
                return True
            if isinstance(e, BinOp):
                return _has_agg(e.left) or _has_agg(e.right)
            if isinstance(e, UnaryOp):
                return _has_agg(e.operand)
            if isinstance(e, FuncCall):
                return any(_has_agg(a) for a in e.args)
            return False
        if not _has_agg(having_expr):
            if where_expr:
                where_expr = BinOp(op=BinOpKind.AND, left=where_expr, right=having_expr)
            else:
                where_expr = having_expr
            having_expr = None

    ir = QueryIR(
        select=select_exprs,
        from_table=from_table,
        joins=joins,
        where=where_expr,
        group_by=group_by,
        having=having_expr,
        order_by=order_by,
        limit=limit_val,
        distinct=is_distinct,
    )
    return ir, None


# ---------------------------------------------------------------------------
# ROLLUP / GROUPING SETS lowering
# ---------------------------------------------------------------------------


def _make_groupby_branch(
    stmt: sge.Select,
    keep_cols: list[sge.Expression],
    null_col_names: set[str],
) -> tuple[Optional[QueryIR], Optional[str]]:
    """Build one UNION ALL branch for ROLLUP/GROUPING SETS lowering.

    *keep_cols* are the GROUP BY columns for this branch.
    *null_col_names* are column names to replace with NULL in SELECT.
    """
    import copy
    branch = copy.deepcopy(stmt)

    # Replace GROUP BY with just the keep_cols
    group_node = branch.find(sge.Group)
    if group_node is not None:
        # Remove rollup/grouping_sets args
        group_node.args.pop("rollup", None)
        group_node.args.pop("grouping_sets", None)
        group_node.args.pop("cube", None)
        if keep_cols:
            group_node.set("expressions", list(keep_cols))
        else:
            # Empty GROUP BY → total aggregation, remove the Group node
            group_node.pop()

    # Replace nullified columns in SELECT with NULL AS <name>
    if null_col_names:
        new_exprs = []
        for expr in branch.expressions:
            col_name = None
            if isinstance(expr, sge.Column):
                col_name = expr.name
            elif isinstance(expr, sge.Alias) and isinstance(expr.this, sge.Column):
                col_name = expr.this.name

            if col_name and col_name.upper() in {n.upper() for n in null_col_names}:
                alias_name = expr.alias if isinstance(expr, sge.Alias) else col_name
                null_expr = sge.Alias(
                    this=sge.Null(),
                    alias=sge.to_identifier(alias_name),
                )
                new_exprs.append(null_expr)
            else:
                new_exprs.append(expr)
        branch.set("expressions", new_exprs)

    return _convert_select(branch)


def _extract_col_names(cols: list[sge.Expression]) -> list[str]:
    """Extract column names from a list of sqlglot column expressions."""
    names = []
    for c in cols:
        if isinstance(c, sge.Column):
            names.append(c.name)
        elif isinstance(c, sge.Paren) and isinstance(c.this, sge.Column):
            names.append(c.this.name)
        else:
            names.append(str(c))
    return names


def _lower_rollup(
    stmt: sge.Select,
    rollup_nodes: list,
) -> tuple[Optional[QueryIR], Optional[str]]:
    """Lower GROUP BY ROLLUP(c1, c2, ..., cn) to UNION ALL of n+1 grouped queries."""
    # Collect all rollup columns
    all_cols: list[sge.Expression] = []
    for rollup in rollup_nodes:
        if hasattr(rollup, "expressions"):
            all_cols.extend(rollup.expressions)

    if not all_cols:
        return None, "Empty ROLLUP"

    col_names = _extract_col_names(all_cols)

    # Generate n+1 branches: level 0 keeps all, level i drops last i columns
    irs: list[QueryIR] = []
    for level in range(len(all_cols) + 1):
        keep = all_cols[: len(all_cols) - level] if level < len(all_cols) else []
        null_names = set(col_names[len(all_cols) - level :]) if level > 0 else set()
        branch_ir, err = _make_groupby_branch(stmt, keep, null_names)
        if branch_ir is None:
            return None, f"ROLLUP branch {level} failed: {err}"
        irs.append(branch_ir)

    # Chain with UNION ALL
    result = irs[0]
    tail = result
    for branch in irs[1:]:
        tail.set_op = SetOpKind.UNION_ALL
        tail.set_right = branch
        tail = branch

    return result, None


def _lower_grouping_sets(
    stmt: sge.Select,
    gs_nodes: list,
) -> tuple[Optional[QueryIR], Optional[str]]:
    """Lower GROUP BY GROUPING SETS(...) to UNION ALL of grouped queries."""
    # Collect all column names that appear across any grouping set
    all_col_names: set[str] = set()
    grouping_sets: list[list[sge.Expression]] = []

    for gs in gs_nodes:
        if not hasattr(gs, "expressions"):
            continue
        for set_node in gs.expressions:
            cols: list[sge.Expression] = []
            if isinstance(set_node, sge.Tuple):
                cols = list(set_node.expressions)
            elif isinstance(set_node, sge.Paren):
                cols = [set_node.this]
            elif isinstance(set_node, sge.Column):
                cols = [set_node]
            grouping_sets.append(cols)
            all_col_names.update(_extract_col_names(cols))

    if not grouping_sets:
        return None, "Empty GROUPING SETS"

    # Generate one branch per grouping set
    irs: list[QueryIR] = []
    for gs_cols in grouping_sets:
        gs_col_names = set(_extract_col_names(gs_cols))
        null_names = all_col_names - gs_col_names
        branch_ir, err = _make_groupby_branch(stmt, gs_cols, null_names)
        if branch_ir is None:
            return None, f"GROUPING SETS branch failed: {err}"
        irs.append(branch_ir)

    # Chain with UNION ALL
    result = irs[0]
    tail = result
    for branch in irs[1:]:
        tail.set_op = SetOpKind.UNION_ALL
        tail.set_right = branch
        tail = branch

    return result, None


# ---------------------------------------------------------------------------
# FROM clause
# ---------------------------------------------------------------------------


def _convert_from(
    from_clause: sge.From,
    outer_aliases: frozenset[str] | None = None,
) -> tuple[Optional[RelRef | DerivedTable], Optional[str]]:
    """Extract the table reference from a FROM clause."""
    tbl = from_clause.this
    return _extract_rel_ref(tbl, outer_aliases=outer_aliases)


def _extract_rel_ref(
    node: sge.Expression,
    outer_aliases: frozenset[str] | None = None,
) -> tuple[Optional[RelRef | DerivedTable], Optional[str]]:
    """Extract a RelRef or DerivedTable from a table node (possibly aliased)."""
    alias: Optional[str] = None

    if isinstance(node, sge.Alias):
        alias = node.alias
        node = node.this

    # LATERAL joins
    if isinstance(node, sge.Lateral):
        lat_alias = alias or node.alias
        if not lat_alias:
            lat_alias = "_lateral"
        lat_inner = node.this
        if isinstance(lat_inner, sge.Subquery):
            lat_inner = lat_inner.this
        while isinstance(lat_inner, sge.Subquery):
            lat_inner = lat_inner.this
        if isinstance(lat_inner, sge.Select):
            inner_ir, err = _convert_select(lat_inner, outer_aliases=outer_aliases)
            if inner_ir is None:
                return None, f"Cannot convert LATERAL subquery: {err}"
            return DerivedTable(query=inner_ir, alias=lat_alias), None
        return None, f"LATERAL must contain a SELECT, got {type(lat_inner).__name__}"

    if isinstance(node, sge.Subquery):
        # Derived table: (SELECT ...) AS alias
        sub_alias = alias or node.alias
        if not sub_alias:
            sub_alias = "_subquery"
        # Extract positional column aliases from AS t(A, B) syntax
        col_aliases: list[str] = []
        ta_node = node.args.get("alias")
        if isinstance(ta_node, sge.TableAlias) and ta_node.columns:
            col_aliases = [col.name for col in ta_node.columns]
        inner = node.this
        # FIX.28a: Unwrap nested Subquery wrappers (double-parens).
        # FIX.37b: Preserve joins attached to intermediate Subquery
        # nodes.  sqlglot represents ((SELECT * FROM T1) AS A LEFT JOIN
        # (SELECT * FROM T2) AS B ON ...) as Subquery(this=Subquery(
        # this=Select, alias=A, joins=[...])).  The inner Subquery
        # carries the joins; unwrapping blindly would lose them.
        while isinstance(inner, sge.Subquery):
            if inner.args.get("joins"):
                break  # this Subquery carries joins — handle below
            inner = inner.this
        # Handle parenthesized join trees: (T1 CROSS JOIN T2) or
        # ((SELECT ..) AS A JOIN (SELECT ..) AS B ON ..)
        # sqlglot parses this as Subquery/Table with inner joins
        if isinstance(inner, (sge.Table, sge.Subquery)) and inner.args.get("joins"):
            inner_table = inner.copy()
            inner_joins = list(inner_table.args.pop("joins", []))
            synth = sge.Select(expressions=[sge.Star()]).from_(inner_table)
            for ij in inner_joins:
                synth.append("joins", ij)
            inner_ir, err = _convert_select(synth, outer_aliases=outer_aliases)
            if inner_ir is None:
                return None, f"Cannot convert parenthesized join tree: {err}"
            return DerivedTable(query=inner_ir, alias=sub_alias, column_aliases=col_aliases), None
        # Handle set operations (UNION/INTERSECT/EXCEPT) inside derived tables
        if isinstance(inner, (sge.Union, sge.Intersect, sge.Except)):
            inner_ir, err = _convert_set_op(inner)
        elif isinstance(inner, sge.Values):
            inner_ir, err = _convert_values(inner)
        elif isinstance(inner, sge.Select):
            inner_ir, err = _convert_select(inner, outer_aliases=outer_aliases)
        else:
            return None, f"Derived table must contain a SELECT or set operation, got {type(inner).__name__}"
        if inner_ir is None:
            return None, f"Cannot convert derived table: {err}"
        return DerivedTable(query=inner_ir, alias=sub_alias, column_aliases=col_aliases), None

    if isinstance(node, sge.Table):
        table_name = node.name
        tbl_alias = alias or (node.alias if node.alias else None)
        return RelRef(table=table_name, alias=tbl_alias), None

    # VALUES directly in FROM position (not wrapped in Subquery) — FIX.5
    if isinstance(node, sge.Values):
        inner_ir, err = _convert_values(node)
        if inner_ir is None:
            return None, f"Cannot convert VALUES: {err}"
        val_alias = alias or "_values"
        # FIX.13f: Extract column aliases from VALUES ... AS t(A, B)
        col_aliases: list[str] = []
        ta_node = node.args.get("alias")
        if isinstance(ta_node, sge.TableAlias):
            if ta_node.this:
                val_alias = alias or str(ta_node.this)
            if ta_node.columns:
                col_aliases = [col.name for col in ta_node.columns]
        return DerivedTable(query=inner_ir, alias=val_alias, column_aliases=col_aliases), None

    return None, f"Cannot extract table reference from: {type(node).__name__}"


# ---------------------------------------------------------------------------
# JOIN clause
# ---------------------------------------------------------------------------

_SGE_JOIN_TYPE_MAP: dict[str, JoinType] = {
    "": JoinType.INNER,
    "JOIN": JoinType.INNER,
    "INNER": JoinType.INNER,
    "LEFT": JoinType.LEFT,
    "RIGHT": JoinType.RIGHT,
    "FULL": JoinType.FULL,
    "CROSS": JoinType.CROSS,
}


def _convert_join(
    join_node: sge.Join,
    outer_aliases: frozenset[str] | None = None,
) -> tuple[Optional[JoinClause], Optional[str]]:
    """Convert a sqlglot Join node to a JoinClause."""
    # Determine join type from the node's attributes
    join_type = JoinType.INNER
    if join_node.side:
        key = join_node.side.upper()
        join_type = _SGE_JOIN_TYPE_MAP.get(key, JoinType.INNER)
    elif join_node.kind:
        key = join_node.kind.upper()
        join_type = _SGE_JOIN_TYPE_MAP.get(key, JoinType.INNER)

    # Right table
    right_node = join_node.this
    right_ref, rerr = _extract_rel_ref(right_node, outer_aliases=outer_aliases)
    if right_ref is None:
        return None, rerr

    # ON condition
    on_node = join_node.args.get("on")
    using_node = join_node.args.get("using")
    if on_node is not None:
        converted = _convert_expr(on_node)
        if converted is None:
            return None, f"Cannot convert JOIN ON expression: {on_node}"
        on_expr: ExprUnion = converted
    elif using_node is not None:
        # JOIN ... USING(col1, col2, ...) → ON left.col1 = right.col1 AND ...
        if isinstance(right_ref, RelRef):
            right_alias = right_ref.alias or right_ref.table
        elif isinstance(right_ref, DerivedTable):
            right_alias = right_ref.alias
        else:
            right_alias = None

        eq_conditions: list[ExprUnion] = []
        for col_node in using_node:
            col_name = col_node.name if hasattr(col_node, "name") else str(col_node)
            eq = BinOp(
                op=BinOpKind.EQ,
                left=ColumnRef(table=None, column=col_name),
                right=ColumnRef(table=right_alias, column=col_name)
                if right_alias
                else ColumnRef(table=None, column=col_name),
            )
            eq_conditions.append(eq)

        if not eq_conditions:
            return None, "Empty USING clause"

        on_expr = eq_conditions[0]
        for cond in eq_conditions[1:]:
            on_expr = BinOp(op=BinOpKind.AND, left=on_expr, right=cond)
    elif join_type in (JoinType.CROSS, JoinType.INNER):
        # CROSS JOIN or implicit join (comma-separated FROM) has no ON;
        # use TRUE literal — the actual join condition is in WHERE.
        on_expr = Literal(value=True)
    else:
        return None, "JOIN without ON clause"

    return JoinClause(join_type=join_type, right=right_ref, on=on_expr), None


# ---------------------------------------------------------------------------
# Expression conversion (sqlglot → IR)
# ---------------------------------------------------------------------------

# Reverse of render_sql._BINOP_MAP
_SGE_BINOP_MAP: dict[type[sge.Expression], BinOpKind] = {
    sge.EQ: BinOpKind.EQ,
    sge.NEQ: BinOpKind.NEQ,
    sge.LT: BinOpKind.LT,
    sge.GT: BinOpKind.GT,
    sge.LTE: BinOpKind.LTE,
    sge.GTE: BinOpKind.GTE,
    sge.Add: BinOpKind.ADD,
    sge.Sub: BinOpKind.SUB,
    sge.Mul: BinOpKind.MUL,
    sge.Div: BinOpKind.DIV,
    sge.Mod: BinOpKind.MOD,
    sge.And: BinOpKind.AND,
    sge.Or: BinOpKind.OR,
    sge.NullSafeEQ: BinOpKind.IS_NOT_DISTINCT_FROM,
    sge.NullSafeNEQ: BinOpKind.IS_DISTINCT_FROM,
}

# Reverse of render_sql._AGG_FUNC_MAP
_SGE_AGG_MAP: dict[type[sge.Expression], AggFunc] = {
    sge.Count: AggFunc.COUNT,
    sge.Sum: AggFunc.SUM,
    sge.Avg: AggFunc.AVG,
    sge.Min: AggFunc.MIN,
    sge.Max: AggFunc.MAX,
}


def _convert_expr(node: sge.Expression) -> Optional[ExprUnion]:
    """Convert a sqlglot expression node to an IR ExprUnion.

    Returns None for constructs we cannot represent.
    """
    if node is None:
        return None

    # --- Parentheses: unwrap ---
    if isinstance(node, sge.Paren):
        return _convert_expr(node.this)

    # --- Alias: unwrap and attach alias ---
    if isinstance(node, sge.Alias):
        inner = _convert_expr(node.this)
        if inner is not None:
            inner.alias = node.alias
        return inner

    # --- Star ---
    if isinstance(node, sge.Star):
        return Star()

    # --- Column reference ---
    if isinstance(node, sge.Column):
        table: Optional[str] = None
        if node.table:
            table = node.table
        return ColumnRef(table=table, column=node.name)

    # --- Literals ---
    if isinstance(node, sge.Literal):
        if node.is_number:
            text = node.this
            try:
                return Literal(value=int(text))
            except ValueError:
                try:
                    return Literal(value=float(text))
                except ValueError:
                    return Literal(value=text)
        # String literal
        return Literal(value=node.this)

    if isinstance(node, sge.Boolean):
        return Literal(value=node.this)

    if isinstance(node, sge.Null):
        return Literal(value=None)

    # --- Binary operators ---
    binop_kind = _SGE_BINOP_MAP.get(type(node))
    if binop_kind is not None:
        # FIX.18a: Fix IS TRUE/FALSE precedence.
        # sqlglot mis-parses "X = Y IS TRUE" as EQ(X, IS(Y, TRUE)) instead of
        # IS(EQ(X, Y), TRUE).  In standard SQL, IS TRUE/FALSE has lower
        # precedence than comparison operators (=, <>, <, >, <=, >=).
        # Detect and restructure: BinOp(left, IS(x, Boolean/Null)) →
        # IS(BinOp(left, x), Boolean/Null).
        rhs_node = node.expression
        if (
            binop_kind in (BinOpKind.EQ, BinOpKind.NEQ, BinOpKind.LT, BinOpKind.GT,
                           BinOpKind.LTE, BinOpKind.GTE)
            and isinstance(rhs_node, sge.Is)
            and isinstance(rhs_node.expression, (sge.Boolean, sge.Null))
        ):
            # Restructure: IS(BinOp(left, rhs_node.this), rhs_node.expression)
            inner_left = _convert_expr(node.this)
            inner_right = _convert_expr(rhs_node.this)
            is_rhs = rhs_node.expression
            if inner_left is not None and inner_right is not None:
                cmp_expr = BinOp(op=binop_kind, left=inner_left, right=inner_right)
                if isinstance(is_rhs, sge.Null):
                    return UnaryOp(op=UnaryOpKind.IS_NULL, operand=cmp_expr)
                else:
                    is_rhs_ir = _convert_expr(is_rhs)
                    if is_rhs_ir is not None:
                        return BinOp(op=BinOpKind.IS, left=cmp_expr, right=is_rhs_ir)
            return None

        # FIX.28a: Tuple equality: (a, b) = (c, d) → a = c AND b = d
        if (binop_kind == BinOpKind.EQ
                and isinstance(node.this, sge.Tuple)
                and isinstance(node.expression, sge.Tuple)):
            left_elems = node.this.expressions
            right_elems = node.expression.expressions
            if len(left_elems) == len(right_elems) and len(left_elems) > 0:
                pairs = []
                for le, re in zip(left_elems, right_elems):
                    lc = _convert_expr(le)
                    rc = _convert_expr(re)
                    if lc is None or rc is None:
                        return None
                    pairs.append(BinOp(op=BinOpKind.EQ, left=lc, right=rc))
                result = pairs[0]
                for p in pairs[1:]:
                    result = BinOp(op=BinOpKind.AND, left=result, right=p)
                return result

        left = _convert_expr(node.this)
        right = _convert_expr(node.expression)
        if left is None or right is None:
            return None
        return BinOp(op=binop_kind, left=left, right=right)

    # --- LIKE ---
    if isinstance(node, sge.Like):
        left = _convert_expr(node.this)
        right = _convert_expr(node.expression)
        if left is None or right is None:
            return None
        return BinOp(op=BinOpKind.LIKE, left=left, right=right)

    # --- String concatenation (||) ---
    if isinstance(node, sge.DPipe):
        left = _convert_expr(node.this)
        right = _convert_expr(node.expression)
        if left is None or right is None:
            return None
        return FuncCall(func_name="CONCAT", args=[left, right])

    # --- IS (null checks) ---
    if isinstance(node, sge.Is):
        operand = _convert_expr(node.this)
        rhs = node.expression
        if operand is not None and isinstance(rhs, sge.Null):
            return UnaryOp(op=UnaryOpKind.IS_NULL, operand=operand)
        # IS TRUE / IS FALSE etc. — fall through to generic handling
        if operand is not None:
            rhs_ir = _convert_expr(rhs)
            if rhs_ir is not None:
                return BinOp(op=BinOpKind.IS, left=operand, right=rhs_ir)
        return None

    # --- NOT ---
    if isinstance(node, sge.Not):
        inner = node.this
        # NOT(IS(x, NULL)) → IS_NOT_NULL
        if isinstance(inner, sge.Is) and isinstance(inner.expression, sge.Null):
            operand = _convert_expr(inner.this)
            if operand is not None:
                return UnaryOp(op=UnaryOpKind.IS_NOT_NULL, operand=operand)
        converted = _convert_expr(inner)
        if converted is None:
            return None
        return UnaryOp(op=UnaryOpKind.NOT, operand=converted)

    # --- NEG ---
    if isinstance(node, sge.Neg):
        operand = _convert_expr(node.this)
        if operand is None:
            return None
        return UnaryOp(op=UnaryOpKind.NEG, operand=operand)

    # --- IN ---
    if isinstance(node, sge.In):
        # Handle tuple IN: (a, b) IN (SELECT x, y FROM ...)
        if isinstance(node.this, sge.Tuple):
            sub_node = node.args.get("query")
            if sub_node is not None:
                return _convert_tuple_in_subquery(node.this, sub_node)
            return None
        expr = _convert_expr(node.this)
        if expr is None:
            return None
        # IN (SELECT ...) → InSubquery
        sub_node = node.args.get("query")
        if sub_node is not None:
            inner_ir, err = _convert_subquery(sub_node)
            if inner_ir is None:
                return None
            return InSubquery(expr=expr, query=inner_ir)
        # IN (v1, v2, ...) → InList
        values: list[ExprUnion] = []
        for v in node.expressions:
            cv = _convert_expr(v)
            if cv is None:
                return None
            values.append(cv)
        return InList(expr=expr, values=values)

    # --- EXISTS (SELECT ...) ---
    if isinstance(node, sge.Exists):
        sub_node = node.this
        inner_ir, err = _convert_subquery(sub_node)
        if inner_ir is None:
            return None
        return ExistsSubquery(query=inner_ir)

    # --- BETWEEN ---
    if isinstance(node, sge.Between):
        expr = _convert_expr(node.this)
        low = _convert_expr(node.args.get("low"))
        high = _convert_expr(node.args.get("high"))
        if expr is None or low is None or high is None:
            return None
        return Between(expr=expr, low=low, high=high)

    # --- CASE expression ---
    if isinstance(node, sge.Case):
        whens = []
        for ifs_node in node.args.get("ifs", []):
            if isinstance(ifs_node, sge.If):
                cond = _convert_expr(ifs_node.this)
                val = _convert_expr(ifs_node.args.get("true"))
                if cond is not None and val is not None:
                    whens.append(CaseWhen(when=cond, then=val))
        else_val = None
        else_node = node.args.get("default")
        if else_node is not None:
            else_val = _convert_expr(else_node)
        if not whens:
            return None
        return CaseExpr(whens=whens, else_=else_val)

    # --- Window functions ---
    if isinstance(node, sge.Window):
        inner = node.this
        func_name = ""
        args: list[ExprUnion] = []
        distinct = False

        # Identify the inner function and extract its name/args
        if isinstance(inner, sge.RowNumber):
            func_name = "ROW_NUMBER"
        elif isinstance(inner, sge.Rank):
            func_name = "RANK"
        elif isinstance(inner, sge.DenseRank):
            func_name = "DENSE_RANK"
        elif isinstance(inner, sge.Lag):
            func_name = "LAG"
        elif isinstance(inner, sge.Lead):
            func_name = "LEAD"
        elif isinstance(inner, sge.FirstValue):
            func_name = "FIRST_VALUE"
        elif isinstance(inner, sge.LastValue):
            func_name = "LAST_VALUE"
        elif isinstance(inner, sge.NthValue):
            func_name = "NTH_VALUE"
        elif isinstance(inner, sge.Count):
            func_name = "COUNT"
        elif isinstance(inner, sge.Sum):
            func_name = "SUM"
        elif isinstance(inner, sge.Avg):
            func_name = "AVG"
        elif isinstance(inner, sge.Min):
            func_name = "MIN"
        elif isinstance(inner, sge.Max):
            func_name = "MAX"
        elif isinstance(inner, sge.Anonymous):
            func_name = inner.this
        elif isinstance(inner, sge.Func):
            func_name = inner.sql_name() if hasattr(inner, "sql_name") else type(inner).__name__.upper()
        else:
            func_name = type(inner).__name__.upper()

        # Extract arguments from the inner function
        if hasattr(inner, "this") and inner.this is not None:
            inner_arg = inner.this
            if isinstance(inner_arg, sge.Distinct):
                distinct = True
                exprs = inner_arg.expressions
                if exprs:
                    ca = _convert_expr(exprs[0])
                    if ca is not None:
                        args.append(ca)
            elif not isinstance(inner_arg, str):
                ca = _convert_expr(inner_arg)
                if ca is not None:
                    args.append(ca)

        # partition_by
        partition_by: list[ExprUnion] = []
        if node.args.get("partition_by"):
            for p in node.args["partition_by"]:
                cp = _convert_expr(p)
                if cp is not None:
                    partition_by.append(cp)

        # order_by
        order_by_specs: list[SortSpec] = []
        order_node = node.args.get("order")
        if order_node is not None:
            if isinstance(order_node, sge.Order):
                for o in order_node.expressions:
                    ss = _convert_sort(o)
                    if ss is not None:
                        order_by_specs.append(ss)

        return WindowFunc(
            func_name=func_name,
            args=args,
            partition_by=partition_by,
            order_by=order_by_specs,
            distinct=distinct,
        )

    # --- FILTER(WHERE) → CASE lowering (FIX.2) ---
    if isinstance(node, sge.Filter):
        inner_agg = node.this
        where_cond = node.expression
        # Extract the WHERE condition from the filter
        if isinstance(where_cond, sge.Where):
            where_cond = where_cond.this
        cond_ir = _convert_expr(where_cond)
        if cond_ir is None:
            return None
        # Determine the aggregate function
        agg_func_f = _SGE_AGG_MAP.get(type(inner_agg))
        if agg_func_f is None:
            return None
        # Extract distinct and argument from the aggregate
        agg_inner = inner_agg.this
        distinct = False
        if isinstance(agg_inner, sge.Star):
            # COUNT(*) FILTER(WHERE p) → COUNT(CASE WHEN p THEN 1 ELSE NULL END)
            case_expr = CaseExpr(
                whens=[CaseWhen(when=cond_ir, then=Literal(value=1))],
                else_=Literal(value=None),
            )
            return AggCall(func=agg_func_f, arg=case_expr, distinct=False)
        if isinstance(agg_inner, sge.Distinct):
            distinct = True
            exprs = agg_inner.expressions
            arg_ir = _convert_expr(exprs[0]) if exprs else None
        else:
            arg_ir = _convert_expr(agg_inner)
        if arg_ir is None:
            return None
        # AGG(x) FILTER(WHERE p) → AGG(CASE WHEN p THEN x ELSE NULL END)
        case_expr = CaseExpr(
            whens=[CaseWhen(when=cond_ir, then=arg_ir)],
            else_=Literal(value=None),
        )
        return AggCall(func=agg_func_f, arg=case_expr, distinct=distinct)

    # --- Aggregates ---
    agg_func = _SGE_AGG_MAP.get(type(node))
    if agg_func is not None:
        return _convert_agg(node, agg_func)

    # --- CAST ---
    if isinstance(node, sge.Cast):
        inner = _convert_expr(node.this)
        if inner is None:
            return None
        type_node = node.args.get("to")
        type_str = type_node.sql() if type_node else "UNKNOWN"
        # FIX.28a: Keep CAST(string AS DATE) as a string literal with DATE type.
        # Previously converted to epoch-day integers, but date columns are
        # encoded as symbol-table indices, creating incompatible numeric spaces.
        if type_str.upper() in ("DATE", "TIMESTAMP") and isinstance(inner, Literal) and isinstance(inner.value, str):
            from ..ir.types import SemType
            return Literal(value=inner.value, sem_type=SemType.DATE)
        return FuncCall(func_name="CAST", args=[inner, Literal(value=type_str)])

    # --- COALESCE ---
    if isinstance(node, sge.Coalesce):
        coal_args: list[ExprUnion] = []
        first = _convert_expr(node.this)
        if first is not None:
            coal_args.append(first)
        for a in node.expressions:
            ca = _convert_expr(a)
            if ca is not None:
                coal_args.append(ca)
        if not coal_args:
            return None
        return FuncCall(func_name="COALESCE", args=coal_args)

    # --- Named scalar functions (LOWER, UPPER, etc.) ---
    if isinstance(node, (sge.Lower, sge.Upper)):
        func_name = type(node).__name__.upper()
        inner = _convert_expr(node.this)
        if inner is None:
            return None
        return FuncCall(func_name=func_name, args=[inner])

    if isinstance(node, sge.DateTrunc):
        unit = _convert_expr(node.args.get("unit"))
        expr = _convert_expr(node.this)
        if unit is None or expr is None:
            return None
        return FuncCall(func_name="DATE_TRUNC", args=[unit, expr])

    if isinstance(node, sge.Extract):
        part_node = node.this
        part_str = part_node.this if isinstance(part_node, sge.Var) else str(part_node)
        expr = _convert_expr(node.expression)
        if expr is None:
            return None
        return FuncCall(func_name="EXTRACT", args=[Literal(value=part_str), expr])

    # --- FIX.28a: DateStrToDate — DATE 'YYYY-MM-DD' literal ---
    # Keep as string with SemType.DATE so it enters the symbol table
    # and is compared on the same numeric scale as date columns.
    if isinstance(node, sge.DateStrToDate):
        inner = node.this
        if isinstance(inner, sge.Literal) and not inner.is_number:
            from ..ir.types import SemType
            return Literal(value=inner.this, sem_type=SemType.DATE)
        return _convert_expr(inner)

    # --- INTERVAL literal ---
    if isinstance(node, sge.Interval):
        # INTERVAL '1' DAY → integer value
        # Under integer encoding, DAY intervals are just integers
        val_node = node.this
        if val_node is not None:
            inner = _convert_expr(val_node)
            if inner is not None:
                return inner
        return Literal(value=1)  # fallback

    # --- DATE_ADD ---
    if isinstance(node, sge.DateAdd):
        date_expr = _convert_expr(node.this)
        interval_expr = _convert_expr(node.expression)
        if date_expr is None or interval_expr is None:
            return None
        return BinOp(op=BinOpKind.ADD, left=date_expr, right=interval_expr)

    # --- DATE_SUB ---
    if isinstance(node, sge.DateSub):
        date_expr = _convert_expr(node.this)
        interval_expr = _convert_expr(node.expression)
        if date_expr is None or interval_expr is None:
            return None
        return BinOp(op=BinOpKind.SUB, left=date_expr, right=interval_expr)

    # --- DATEDIFF ---
    if isinstance(node, sge.DateDiff):
        # DATEDIFF(a, b) = a - b (in days)
        a = _convert_expr(node.this)
        b = _convert_expr(node.expression)
        if a is None or b is None:
            return None
        return BinOp(op=BinOpKind.SUB, left=a, right=b)

    # --- TIMESTAMPDIFF ---
    if isinstance(node, sge.TimestampDiff):
        # TIMESTAMPDIFF(unit, start, end) = end - start
        # sqlglot puts: this=end, expression=start, unit=unit
        end = _convert_expr(node.this)
        start = _convert_expr(node.expression)
        if end is None or start is None:
            return None
        return BinOp(op=BinOpKind.SUB, left=end, right=start)

    # --- Date formatting functions (STRFTIME, TimeToStr, etc.) ---
    # TsOrDsToTimestamp is sqlglot's internal cast wrapper — passthrough to inner
    if isinstance(node, sge.TsOrDsToTimestamp):
        return _convert_expr(node.this)

    if isinstance(node, sge.TimeToStr):
        args = []
        # format arg comes first for STRFTIME(format, date)
        fmt = node.args.get("format")
        if fmt is not None:
            ca = _convert_expr(fmt)
            if ca is not None:
                args.append(ca)
        # 'this' is the date expression
        inner = node.args.get("this")
        if inner is not None:
            ca = _convert_expr(inner)
            if ca is not None:
                args.append(ca)
        if not args:
            return None
        return FuncCall(func_name="STRFTIME", args=args)

    # --- SUBSTR / SUBSTRING ---
    if isinstance(node, sge.Substring):
        substr_args: list[ExprUnion] = []
        inner = _convert_expr(node.this)
        if inner is None:
            return None
        substr_args.append(inner)
        for key in ("start", "length"):
            child = node.args.get(key)
            if child is not None:
                ca = _convert_expr(child)
                if ca is not None:
                    substr_args.append(ca)
        return FuncCall(func_name="SUBSTR", args=substr_args)

    # --- REPLACE ---
    if isinstance(node, sge.Replace):
        inner = _convert_expr(node.this)
        expr = _convert_expr(node.expression)
        if inner is None or expr is None:
            return None
        repl_args: list[ExprUnion] = [inner, expr]
        repl = node.args.get("replacement")
        if repl is not None:
            ra = _convert_expr(repl)
            if ra is not None:
                repl_args.append(ra)
        return FuncCall(func_name="REPLACE", args=repl_args)

    # --- ROUND ---
    if isinstance(node, sge.Round):
        inner = _convert_expr(node.this)
        if inner is None:
            return None
        round_args: list[ExprUnion] = [inner]
        decimals = node.args.get("decimals")
        if decimals is not None:
            da = _convert_expr(decimals)
            if da is not None:
                round_args.append(da)
        return FuncCall(func_name="ROUND", args=round_args)

    # --- INSTR (sqlglot: StrPosition) ---
    if isinstance(node, sge.StrPosition):
        inner = _convert_expr(node.this)
        if inner is None:
            return None
        instr_args: list[ExprUnion] = [inner]
        substr = node.args.get("substr")
        if substr is not None:
            sa = _convert_expr(substr)
            if sa is not None:
                instr_args.append(sa)
        pos = node.args.get("position")
        if pos is not None:
            pa = _convert_expr(pos)
            if pa is not None:
                instr_args.append(pa)
        return FuncCall(func_name="INSTR", args=instr_args)

    # --- IIF (sqlglot: If) ---
    if isinstance(node, sge.If):
        cond = _convert_expr(node.this)
        if cond is None:
            return None
        iif_args: list[ExprUnion] = [cond]
        for key in ("true", "false"):
            child = node.args.get(key)
            if child is not None:
                ca = _convert_expr(child)
                if ca is not None:
                    iif_args.append(ca)
        return FuncCall(func_name="IIF", args=iif_args)

    # --- GROUP_CONCAT ---
    if isinstance(node, sge.GroupConcat):
        gc_args: list[ExprUnion] = []
        inner = _convert_expr(node.this)
        if inner is None:
            return None
        gc_args.append(inner)
        sep = node.args.get("separator")
        if sep is not None:
            sa = _convert_expr(sep)
            if sa is not None:
                gc_args.append(sa)
        return FuncCall(func_name="GROUP_CONCAT", args=gc_args)

    # --- Anonymous / generic function call ---
    if isinstance(node, sge.Anonymous):
        func_name = node.this
        # FIX.24a: MySQL ISNULL(col) → IS NULL check (1-arg form)
        if func_name.upper() == "ISNULL" and len(node.expressions) == 1:
            operand = _convert_expr(node.expressions[0])
            if operand is not None:
                return UnaryOp(op=UnaryOpKind.IS_NULL, operand=operand)
            return None
        args = []
        for a in node.expressions:
            ca = _convert_expr(a)
            if ca is None:
                return None
            args.append(ca)
        return FuncCall(func_name=func_name, args=args)

    # --- Catch-all for other Function subclasses ---
    if isinstance(node, sge.Func):
        func_name = node.sql_name() if hasattr(node, "sql_name") else type(node).__name__.upper()
        args = []
        for key in ("this", "expression"):
            child = node.args.get(key)
            if child is not None:
                ca = _convert_expr(child)
                if ca is not None:
                    args.append(ca)
        for child in node.expressions:
            ca = _convert_expr(child)
            if ca is not None:
                args.append(ca)
        return FuncCall(func_name=func_name, args=args)

    # --- Scalar subquery: (SELECT ...) ---
    if isinstance(node, sge.Subquery):
        # Unwrap nested Subquery wrappers (triple parens)
        unwrapped = node
        while isinstance(unwrapped.this, sge.Subquery):
            unwrapped = unwrapped.this
        inner_ir, err = _convert_subquery(unwrapped)
        if inner_ir is None:
            return None
        return ScalarSubquery(query=inner_ir)

    # Unsupported expression type — return None
    return None


# ---------------------------------------------------------------------------
# Tuple IN subquery lowering
# ---------------------------------------------------------------------------


def _convert_tuple_in_subquery(
    tuple_node: sge.Tuple,
    sub_node: sge.Expression,
) -> Optional[ExprUnion]:
    """Lower (a,b) IN (SELECT x,y FROM ...) to EXISTS(SELECT 1 FROM ... WHERE a=x AND b=y).

    FIX.19b: Assign a unique alias to the inner FROM table and qualify
    inner SELECT columns so that the correlated equality predicates
    (outer.col = inner.col) are distinguishable in the binding.
    Without this, self-referencing queries like
    ``(EMPNO, DEPTNO) IN (SELECT EMPNO, DEPTNO FROM EMP WHERE ...)``
    would produce ``EMPNO = EMPNO`` (always TRUE) instead of
    ``outer.EMPNO = inner.EMPNO`` (correlated equality).
    """
    inner = sub_node.this if isinstance(sub_node, sge.Subquery) else sub_node
    while isinstance(inner, sge.Subquery):
        inner = inner.this
    if not isinstance(inner, sge.Select):
        return None

    tuple_exprs = [_convert_expr(e) for e in tuple_node.expressions]
    if any(e is None for e in tuple_exprs):
        return None

    inner_select_exprs = inner.expressions
    if len(tuple_exprs) != len(inner_select_exprs):
        return None

    inner_ir, err = _convert_select(inner)
    if inner_ir is None:
        return None

    # FIX.19b: Alias the inner FROM table to avoid binding collisions
    # with the outer query's table references.
    inner_alias = f"_tin_{id(tuple_node)}"
    inner_from = inner_ir.from_table
    if isinstance(inner_from, RelRef) and inner_from.alias is None:
        inner_from = RelRef(table=inner_from.table, alias=inner_alias)
        inner_table_name = inner_from.table.lower()
    elif isinstance(inner_from, RelRef) and inner_from.alias is not None:
        inner_alias = inner_from.alias.lower()
        inner_table_name = inner_from.table.lower()
    else:
        inner_table_name = None

    # Build qualified inner select columns for the equality predicates
    # FIX.30a: Use _qualify_unqualified_refs recursively so that nested
    # refs inside AggCall (e.g., MIN(EVENT_DATE)) are also qualified.
    # Previously only top-level ColumnRefs were qualified, leaving
    # aggregate arguments unqualified and causing ambiguous resolution
    # in correlated EXISTS HAVING evaluation.
    inner_select_qualified: list[ExprUnion] = []
    for iexpr in inner_ir.select:
        if inner_table_name:
            inner_select_qualified.append(
                _qualify_unqualified_refs(iexpr, inner_alias)
            )
        else:
            inner_select_qualified.append(iexpr)

    eq_conditions: list[ExprUnion] = []
    for texpr, iexpr in zip(tuple_exprs, inner_select_qualified):
        eq_conditions.append(BinOp(op=BinOpKind.EQ, left=texpr, right=iexpr))

    # FIX.25d: When the inner query has GROUP BY and equality conditions
    # involve aggregate expressions (e.g., MIN(EVENT_DATE)), those conditions
    # must go into HAVING (post-group), not WHERE (pre-group).
    # Conditions referencing only plain columns go into WHERE.
    def _contains_agg(expr) -> bool:
        if isinstance(expr, AggCall):
            return True
        if isinstance(expr, BinOp):
            return _contains_agg(expr.left) or _contains_agg(expr.right)
        if hasattr(expr, 'operand'):
            return _contains_agg(expr.operand)
        if isinstance(expr, FuncCall):
            return any(_contains_agg(a) for a in expr.args)
        return False

    where_conds: list[ExprUnion] = []
    having_conds: list[ExprUnion] = []

    if inner_ir.group_by:
        for cond in eq_conditions:
            if _contains_agg(cond):
                having_conds.append(cond)
            else:
                where_conds.append(cond)
    else:
        where_conds = eq_conditions

    # Qualify inner WHERE column refs to use the inner alias
    inner_where = inner_ir.where
    if inner_where and inner_table_name:
        inner_where = _qualify_unqualified_refs(inner_where, inner_alias)

    # FIX.30a: Also qualify inner GROUP BY and HAVING column refs.
    inner_group_by = list(inner_ir.group_by)
    if inner_table_name:
        inner_group_by = [_qualify_unqualified_refs(g, inner_alias) for g in inner_group_by]

    inner_having = inner_ir.having
    if inner_having and inner_table_name:
        inner_having = _qualify_unqualified_refs(inner_having, inner_alias)

    # FIX.30a: When having_conds exist (aggregate conditions like
    # outer.EVENT_DATE = MIN(inner.EVENT_DATE)), wrap the inner
    # aggregated query as a derived table and use WHERE instead of
    # HAVING.  This avoids the correlated outer ref resolving to the
    # wrong binding when inner and outer share the same base table
    # (self-join).
    #
    # Before: EXISTS(SELECT 1 FROM T AS _tin GROUP BY k
    #                HAVING outer.col = MIN(_tin.col))
    # After:  EXISTS(SELECT 1 FROM (SELECT k, MIN(col) AS _agg_0
    #                FROM T GROUP BY k) AS _tin
    #                WHERE outer.col = _tin._agg_0)
    if having_conds and inner_ir.group_by:
        from ..ir.types import DerivedTable

        # Build the inner aggregated query's SELECT: group keys + aggregate exprs
        dt_select: list[ExprUnion] = []
        dt_col_aliases: list[str] = []
        # Add group keys
        for gi, g in enumerate(inner_group_by):
            dt_select.append(g)
            if isinstance(g, ColumnRef):
                dt_col_aliases.append(g.column.lower())
            else:
                dt_col_aliases.append(f"_gk_{gi}")

        # Extract aggregate expressions from having_conds and replace
        # them with references to DT columns
        agg_map: dict[int, str] = {}  # id(agg_expr) -> alias
        agg_idx = 0

        def _extract_aggs(expr):
            """Collect AggCall nodes from an expression, assign aliases."""
            nonlocal agg_idx
            if isinstance(expr, AggCall):
                eid = id(expr)
                if eid not in agg_map:
                    alias = f"_agg_{agg_idx}"
                    agg_map[eid] = alias
                    dt_select.append(expr)
                    dt_col_aliases.append(alias)
                    agg_idx += 1
                return
            for attr in ('left', 'right', 'operand', 'arg'):
                child = getattr(expr, attr, None)
                if child is not None:
                    _extract_aggs(child)
            if isinstance(expr, FuncCall):
                for a in expr.args:
                    _extract_aggs(a)

        for cond in having_conds:
            _extract_aggs(cond)
        if inner_having:
            _extract_aggs(inner_having)

        # Build inner aggregated query
        inner_agg_where = inner_where
        inner_agg_ir = QueryIR(
            select=dt_select,
            from_table=inner_from,
            joins=inner_ir.joins,
            where=inner_agg_where,
            group_by=inner_group_by,
            having=inner_having,
        )

        # Build DT wrapping the aggregated query
        dt_alias = inner_alias
        dt = DerivedTable(
            query=inner_agg_ir,
            alias=dt_alias,
            column_aliases=dt_col_aliases,
        )

        # Replace AggCalls in having_conds with ColumnRefs to DT columns
        def _replace_aggs(expr):
            if isinstance(expr, AggCall):
                alias = agg_map.get(id(expr))
                if alias:
                    return ColumnRef(table=dt_alias, column=alias)
                return expr
            if isinstance(expr, BinOp):
                new_left = _replace_aggs(expr.left)
                new_right = _replace_aggs(expr.right)
                return BinOp(op=expr.op, left=new_left, right=new_right,
                             sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias)
            if isinstance(expr, UnaryOp):
                return UnaryOp(op=expr.op, operand=_replace_aggs(expr.operand),
                               sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias)
            if isinstance(expr, FuncCall):
                return FuncCall(func_name=expr.func_name,
                                args=[_replace_aggs(a) for a in expr.args],
                                sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias)
            return expr

        # Build WHERE from non-aggregate conditions + replaced having_conds
        all_where_conds = list(where_conds)
        for cond in having_conds:
            all_where_conds.append(_replace_aggs(cond))

        combined_where: ExprUnion | None = None
        if all_where_conds:
            combined_where = all_where_conds[0]
            for cond in all_where_conds[1:]:
                combined_where = BinOp(op=BinOpKind.AND, left=combined_where, right=cond)

        exists_ir = QueryIR(
            select=[Literal(value=1)],
            from_table=dt,
            joins=[],
            where=combined_where,
            group_by=[],
            having=None,
        )

        return ExistsSubquery(query=exists_ir)

    # Non-aggregate path: simple WHERE-only EXISTS
    # Build WHERE from non-aggregate conditions
    combined_where: ExprUnion | None = None
    if where_conds:
        combined_where = where_conds[0]
        for cond in where_conds[1:]:
            combined_where = BinOp(op=BinOpKind.AND, left=combined_where, right=cond)

    if inner_where and combined_where:
        combined_where = BinOp(op=BinOpKind.AND, left=inner_where, right=combined_where)
    elif inner_where:
        combined_where = inner_where

    exists_ir = QueryIR(
        select=[Literal(value=1)],
        from_table=inner_from,
        joins=inner_ir.joins,
        where=combined_where,
        group_by=inner_group_by,
        having=inner_having,
    )

    return ExistsSubquery(query=exists_ir)


def _resolve_aliases(expr: ExprUnion, alias_map: dict[str, ExprUnion]) -> ExprUnion:
    """Replace ColumnRef(table=None, column=alias) with the aliased expression.

    Used to resolve SELECT alias references in HAVING clauses (MySQL extension).
    """
    if isinstance(expr, ColumnRef) and not expr.table:
        replacement = alias_map.get(expr.column.lower())
        if replacement is not None:
            return replacement
        return expr
    if isinstance(expr, BinOp):
        new_left = _resolve_aliases(expr.left, alias_map)
        new_right = _resolve_aliases(expr.right, alias_map)
        if new_left is expr.left and new_right is expr.right:
            return expr
        return BinOp(op=expr.op, left=new_left, right=new_right,
                     sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias)
    if isinstance(expr, UnaryOp):
        new_operand = _resolve_aliases(expr.operand, alias_map)
        if new_operand is expr.operand:
            return expr
        return UnaryOp(op=expr.op, operand=new_operand,
                       sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias)
    if isinstance(expr, FuncCall):
        new_args = [_resolve_aliases(a, alias_map) for a in expr.args]
        return FuncCall(func_name=expr.func_name, args=new_args,
                        sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias)
    return expr


def _qualify_unqualified_refs(expr: ExprUnion, table_alias: str) -> ExprUnion:
    """Qualify unqualified ColumnRefs with the given table alias."""
    if isinstance(expr, ColumnRef):
        if not expr.table:
            return ColumnRef(
                table=table_alias, column=expr.column,
                sem_type=expr.sem_type, nullability=expr.nullability,
                alias=expr.alias,
            )
        return expr
    if isinstance(expr, BinOp):
        return BinOp(
            op=expr.op,
            left=_qualify_unqualified_refs(expr.left, table_alias),
            right=_qualify_unqualified_refs(expr.right, table_alias),
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    if isinstance(expr, UnaryOp):
        return UnaryOp(
            op=expr.op,
            operand=_qualify_unqualified_refs(expr.operand, table_alias),
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    if isinstance(expr, FuncCall):
        return FuncCall(
            func_name=expr.func_name,
            args=[_qualify_unqualified_refs(a, table_alias) for a in expr.args],
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    if isinstance(expr, AggCall):
        return AggCall(
            func=expr.func,
            arg=_qualify_unqualified_refs(expr.arg, table_alias) if expr.arg else None,
            distinct=expr.distinct,
            sem_type=expr.sem_type, nullability=expr.nullability, alias=expr.alias,
        )
    if isinstance(expr, CaseExpr):
        return CaseExpr(
            whens=[
                CaseWhen(
                    when=_qualify_unqualified_refs(cw.when, table_alias),
                    then=_qualify_unqualified_refs(cw.then, table_alias),
                ) for cw in expr.whens
            ],
            else_=_qualify_unqualified_refs(expr.else_, table_alias) if expr.else_ else None,
            sem_type=expr.sem_type, alias=expr.alias,
        )
    if isinstance(expr, Between):
        return Between(
            expr=_qualify_unqualified_refs(expr.expr, table_alias),
            low=_qualify_unqualified_refs(expr.low, table_alias),
            high=_qualify_unqualified_refs(expr.high, table_alias),
            sem_type=expr.sem_type, alias=expr.alias,
        )
    if isinstance(expr, InList):
        return InList(
            expr=_qualify_unqualified_refs(expr.expr, table_alias),
            values=[_qualify_unqualified_refs(v, table_alias) for v in expr.values],
            sem_type=expr.sem_type, alias=expr.alias,
        )
    return expr


# ---------------------------------------------------------------------------
# Subquery helpers
# ---------------------------------------------------------------------------


def _convert_subquery(
    node: sge.Expression,
) -> tuple[Optional[QueryIR], Optional[str]]:
    """Convert a sqlglot Subquery node to a QueryIR.

    Returns (ir, error). Rejects correlated subqueries.
    """
    inner = node.this if isinstance(node, sge.Subquery) else node
    # Unwrap nested Subquery wrappers (triple parens)
    while isinstance(inner, sge.Subquery):
        inner = inner.this

    # Handle VALUES inside subquery (FIX.5)
    if isinstance(inner, sge.Values):
        return _convert_values(inner)

    # Handle set operations inside subquery
    if isinstance(inner, (sge.Union, sge.Intersect, sge.Except)):
        return _convert_set_op(inner)

    if not isinstance(inner, sge.Select):
        return None, "Subquery must contain a SELECT"

    # Collect table aliases defined in the inner scope
    inner_ir, err = _convert_select(inner)
    if inner_ir is None:
        return None, err

    # Correlation detection: correlated subqueries are now supported.
    # Outer references are resolved at evaluation time via outer bindings.

    return inner_ir, None


def _collect_scope_tables(ir: QueryIR) -> set[str]:
    """Collect all table names/aliases visible in the IR's FROM/JOIN scope."""
    tables: set[str] = set()
    if isinstance(ir.from_table, RelRef):
        tables.add(ir.from_table.alias or ir.from_table.table)
    elif isinstance(ir.from_table, DerivedTable):
        tables.add(ir.from_table.alias)
    for jc in ir.joins:
        if isinstance(jc.right, RelRef):
            tables.add(jc.right.alias or jc.right.table)
        elif isinstance(jc.right, DerivedTable):
            tables.add(jc.right.alias)
    return tables


def _collect_referenced_tables(stmt: sge.Select) -> set[str]:
    """Collect all table qualifiers used in column references within a SELECT."""
    tables: set[str] = set()
    for col in stmt.find_all(sge.Column):
        if col.table:
            tables.add(col.table)
    return tables


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------


def _convert_agg(
    node: sge.Expression, func: AggFunc
) -> Optional[AggCall]:
    """Convert a sqlglot aggregate node to an AggCall."""
    inner = node.this
    distinct = False
    arg: Optional[ExprUnion] = None

    if isinstance(inner, sge.Star):
        # COUNT(*)
        arg = None
    elif isinstance(inner, sge.Distinct):
        distinct = True
        exprs = inner.expressions
        if exprs:
            arg = _convert_expr(exprs[0])
        else:
            arg = None
    else:
        arg = _convert_expr(inner)

    return AggCall(func=func, arg=arg, distinct=distinct)


# ---------------------------------------------------------------------------
# ORDER BY helpers
# ---------------------------------------------------------------------------


def _convert_sort(node: sge.Expression) -> Optional[SortSpec]:
    """Convert a sqlglot ORDER BY element to a SortSpec."""
    if isinstance(node, sge.Ordered):
        expr = _convert_expr(node.this)
        if expr is None:
            return None
        direction = SortDir.DESC if node.args.get("desc") else SortDir.ASC
        return SortSpec(expr=expr, direction=direction)

    # Bare expression with no explicit ordering
    expr = _convert_expr(node)
    if expr is None:
        return None
    return SortSpec(expr=expr, direction=SortDir.ASC)
