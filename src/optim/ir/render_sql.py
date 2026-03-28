"""Render QueryIR → SQL via sqlglot AST.

Never builds SQL strings manually. The IR is compiled to a sqlglot AST,
then emitted in the requested dialect. A round-trip parse sanity check
ensures the rendered SQL is valid.
"""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import sqlglot
from sqlglot import expressions as sge

# SQL reserved words that must be quoted when used as identifiers
_SQL_RESERVED = frozenset({
    "abort", "action", "add", "after", "all", "alter", "always", "analyze",
    "and", "as", "asc", "attach", "autoincrement", "before", "begin",
    "between", "by", "cascade", "case", "cast", "check", "collate", "column",
    "commit", "conflict", "constraint", "create", "cross", "current",
    "current_date", "current_time", "current_timestamp", "database", "default",
    "deferrable", "deferred", "delete", "desc", "detach", "distinct", "do",
    "drop", "each", "else", "end", "escape", "except", "exclude", "exclusive",
    "exists", "explain", "fail", "filter", "first", "following", "for",
    "foreign", "from", "full", "generated", "glob", "group", "groups",
    "having", "if", "ignore", "immediate", "in", "index", "indexed",
    "initially", "inner", "insert", "instead", "intersect", "into", "is",
    "isnull", "join", "key", "last", "left", "like", "limit", "match",
    "materialized", "natural", "no", "not", "nothing", "notnull", "null",
    "nulls", "of", "offset", "on", "or", "order", "others", "outer", "over",
    "partition", "plan", "pragma", "preceding", "primary", "query", "raise",
    "range", "recursive", "references", "regexp", "reindex", "release",
    "rename", "replace", "restrict", "returning", "right", "rollback", "row",
    "rows", "savepoint", "select", "set", "sets", "table", "temp", "temporary",
    "then", "ties", "to", "transaction", "trigger", "unbounded", "union",
    "unique", "update", "using", "vacuum", "values", "view", "virtual",
    "when", "where", "window", "with", "without",
})


def quote_ident(name: str, dialect: str = "sqlite") -> str:
    """Quote a SQL identifier if it's a reserved word or contains special chars.

    Always safe to call — returns the name unchanged if quoting isn't needed.
    Uses double-quotes for SQLite/standard SQL.
    """
    if not name:
        return name
    # Already quoted
    if name.startswith('"') and name.endswith('"'):
        return name
    # Needs quoting: reserved word, contains special chars, or starts with digit
    name_lower = name.lower()
    needs_quote = (
        name_lower in _SQL_RESERVED
        or not name.replace("_", "").isalnum()
        or name[0].isdigit()
    )
    if needs_quote:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'
    return name


from .types import (
    AggCall,
    AggFunc,
    Between,
    BinOp,
    BinOpKind,
    CaseExpr,
    ColumnRef,
    DerivedTable,
    ExistsSubquery,
    Expr,
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
    WindowFunc,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render(ir: QueryIR, dialect: str = "sqlite") -> str:
    """Render an IR to a SQL string in the given dialect."""
    ast = _ir_to_ast(ir)
    sql = ast.sql(dialect=dialect, pretty=True)
    # Round-trip sanity: parse the rendered SQL to confirm it's valid
    roundtrip_check(sql, dialect)
    return sql


def _ir_to_ast(ir: QueryIR) -> sge.Expression:
    """Compile an IR into a sqlglot AST, handling set operations."""
    select = ir_to_sqlglot(ir)
    if ir.set_op and ir.set_right:
        right_ast = _ir_to_ast(ir.set_right)
        if ir.set_op == SetOpKind.UNION:
            return sge.Union(this=select, expression=right_ast, distinct=True)
        elif ir.set_op == SetOpKind.UNION_ALL:
            return sge.Union(this=select, expression=right_ast, distinct=False)
        elif ir.set_op == SetOpKind.INTERSECT:
            return sge.Intersect(this=select, expression=right_ast, distinct=True)
        elif ir.set_op == SetOpKind.EXCEPT:
            return sge.Except(this=select, expression=right_ast, distinct=True)
    return select


def ir_to_sqlglot(ir: QueryIR) -> sge.Select:
    """Compile an IR into a sqlglot Select AST (without set operations)."""
    select = sge.Select()

    # FROM (omit for __values_dual__ sentinel — FIX.5)
    if not (isinstance(ir.from_table, RelRef) and ir.from_table.table == "__values_dual__"):
        from_expr = _rel_ref(ir.from_table)
        select = select.from_(from_expr)

    # JOINs
    for join in ir.joins:
        select = _add_join(select, join)

    # SELECT expressions
    for expr in ir.select:
        sg_expr = _compile_expr(expr)
        if expr.alias:
            sg_expr = sge.Alias(this=sg_expr, alias=sge.to_identifier(expr.alias))
        select = select.select(sg_expr, append=True)

    # WHERE
    if ir.where:
        select = select.where(_compile_expr(ir.where))

    # GROUP BY
    if ir.group_by:
        for gb_expr in ir.group_by:
            select = select.group_by(_compile_expr(gb_expr), append=True)

    # HAVING
    if ir.having:
        select = select.having(_compile_expr(ir.having))

    # ORDER BY
    if ir.order_by:
        for sort in ir.order_by:
            select = _add_order(select, sort)

    # LIMIT
    if ir.limit is not None:
        select = select.limit(ir.limit)

    # DISTINCT
    if ir.distinct:
        select = select.distinct()

    return select


def roundtrip_check(sql: str, dialect: str) -> None:
    """Parse the rendered SQL to verify it's syntactically valid.

    Raises ValueError if the SQL cannot be parsed.
    """
    try:
        parsed = sqlglot.parse(sql, read=dialect)
        if not parsed or parsed[0] is None:
            raise ValueError(f"sqlglot returned empty parse for: {sql[:200]}")
    except sqlglot.errors.ParseError as e:
        raise ValueError(f"Rendered SQL failed round-trip parse: {e}") from e


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_JOIN_TYPE_MAP = {
    JoinType.INNER: "JOIN",
    JoinType.LEFT: "LEFT JOIN",
    JoinType.RIGHT: "RIGHT JOIN",
    JoinType.FULL: "FULL JOIN",
    JoinType.CROSS: "CROSS JOIN",
}

_AGG_FUNC_MAP = {
    AggFunc.COUNT: "COUNT",
    AggFunc.SUM: "SUM",
    AggFunc.AVG: "AVG",
    AggFunc.MIN: "MIN",
    AggFunc.MAX: "MAX",
}

_BINOP_MAP: dict[BinOpKind, type[sge.Expression]] = {
    BinOpKind.EQ: sge.EQ,
    BinOpKind.NEQ: sge.NEQ,
    BinOpKind.LT: sge.LT,
    BinOpKind.GT: sge.GT,
    BinOpKind.LTE: sge.LTE,
    BinOpKind.GTE: sge.GTE,
    BinOpKind.ADD: sge.Add,
    BinOpKind.SUB: sge.Sub,
    BinOpKind.MUL: sge.Mul,
    BinOpKind.DIV: sge.Div,
    BinOpKind.MOD: sge.Mod,
    BinOpKind.AND: sge.And,
    BinOpKind.OR: sge.Or,
    BinOpKind.IS_NOT_DISTINCT_FROM: sge.NullSafeEQ,
    BinOpKind.IS_DISTINCT_FROM: sge.NullSafeNEQ,
}


def _rel_ref(rel: RelRef | DerivedTable) -> sge.Expression:
    if isinstance(rel, DerivedTable):
        inner_ast = ir_to_sqlglot(rel.query)
        alias_node: sge.Expression
        if rel.column_aliases:
            alias_node = sge.TableAlias(
                this=sge.to_identifier(rel.alias),
                columns=[sge.to_identifier(c) for c in rel.column_aliases],
            )
        else:
            alias_node = sge.to_identifier(rel.alias)
        return sge.Subquery(
            this=inner_ast,
            alias=alias_node,
        )
    table = sge.to_table(rel.table)
    if rel.alias and rel.alias != rel.table:
        table = sge.Alias(this=table, alias=sge.to_identifier(rel.alias))
    return table


def _add_join(select: sge.Select, join: JoinClause) -> sge.Select:
    right = _rel_ref(join.right)

    join_kind = _JOIN_TYPE_MAP.get(join.join_type, "JOIN")
    side = ""
    kind = ""
    if "LEFT" in join_kind:
        side = "LEFT"
    elif "RIGHT" in join_kind:
        side = "RIGHT"
    elif "FULL" in join_kind:
        side = "FULL"
    elif "CROSS" in join_kind:
        kind = "CROSS"

    if kind == "CROSS":
        return select.join(right, join_type="CROSS")

    on_expr = _compile_expr(join.on)
    return select.join(
        right,
        on=on_expr,
        join_type=side or kind or "",
    )


def _add_order(select: sge.Select, sort: SortSpec) -> sge.Select:
    sg_expr = _compile_expr(sort.expr)
    if sort.direction == SortDir.DESC:
        sg_expr = sge.Ordered(this=sg_expr, desc=True)
    else:
        sg_expr = sge.Ordered(this=sg_expr, desc=False)
    return select.order_by(sg_expr, append=True)


def _compile_expr(expr: Expr) -> sge.Expression:
    """Compile an IR expression node to a sqlglot expression."""
    if isinstance(expr, ColumnRef):
        return _compile_column_ref(expr)

    if isinstance(expr, Literal):
        return _compile_literal(expr)

    if isinstance(expr, Star):
        return sge.Star()

    if isinstance(expr, AggCall):
        return _compile_agg(expr)

    if isinstance(expr, BinOp):
        return _compile_binop(expr)

    if isinstance(expr, UnaryOp):
        return _compile_unary(expr)

    if isinstance(expr, FuncCall):
        return _compile_func(expr)

    if isinstance(expr, InList):
        return _compile_in_list(expr)

    if isinstance(expr, Between):
        return _compile_between(expr)

    if isinstance(expr, ScalarSubquery):
        return sge.Subquery(this=ir_to_sqlglot(expr.query))

    if isinstance(expr, InSubquery):
        return sge.In(
            this=_compile_expr(expr.expr),
            query=sge.Subquery(this=ir_to_sqlglot(expr.query)),
        )

    if isinstance(expr, ExistsSubquery):
        return sge.Exists(this=sge.Subquery(this=ir_to_sqlglot(expr.query)))

    if isinstance(expr, CaseExpr):
        return _compile_case(expr)

    if isinstance(expr, WindowFunc):
        return _compile_window(expr)

    raise ValueError(f"Unsupported IR expression type: {type(expr).__name__}")


def _compile_column_ref(ref: ColumnRef) -> sge.Column:
    parts: dict[str, sge.Expression] = {
        "this": sge.to_identifier(ref.column),
    }
    if ref.table:
        parts["table"] = sge.to_identifier(ref.table)
    return sge.Column(**parts)


def _compile_literal(lit: Literal) -> sge.Expression:
    if lit.value is None:
        return sge.Null()
    if isinstance(lit.value, bool):
        return sge.Boolean(this=lit.value)
    if isinstance(lit.value, int):
        return sge.Literal.number(lit.value)
    if isinstance(lit.value, float):
        return sge.Literal.number(lit.value)
    if isinstance(lit.value, str):
        return sge.Literal.string(lit.value)
    raise ValueError(f"Unsupported literal type: {type(lit.value)}")


def _compile_agg(agg: AggCall) -> sge.Expression:
    func_name = _AGG_FUNC_MAP[agg.func]

    if agg.arg is None:
        # COUNT(*)
        inner = sge.Star()
    else:
        inner = _compile_expr(agg.arg)

    if agg.distinct:
        inner = sge.Distinct(expressions=[inner])

    func_cls = getattr(sge, func_name.capitalize(), None)
    if func_cls and func_cls is not None:
        # sqlglot has dedicated nodes: Count, Sum, Avg, Min, Max
        return func_cls(this=inner)

    return sge.Anonymous(this=func_name, expressions=[inner])


def _compile_binop(binop: BinOp) -> sge.Expression:
    left = _compile_expr(binop.left)
    right = _compile_expr(binop.right)

    # Explicit parens when OR is nested inside AND (sqlglot doesn't auto-add)
    if binop.op == BinOpKind.AND:
        if isinstance(binop.left, BinOp) and binop.left.op == BinOpKind.OR:
            left = sge.Paren(this=left)
        if isinstance(binop.right, BinOp) and binop.right.op == BinOpKind.OR:
            right = sge.Paren(this=right)

    if binop.op == BinOpKind.LIKE:
        return sge.Like(this=left, expression=right)

    if binop.op == BinOpKind.IS:
        return sge.Is(this=left, expression=right)

    cls = _BINOP_MAP.get(binop.op)
    if cls:
        return cls(this=left, expression=right)

    raise ValueError(f"Unsupported binary operator: {binop.op}")


def _compile_unary(unary: UnaryOp) -> sge.Expression:
    operand = _compile_expr(unary.operand)
    if unary.op == UnaryOpKind.NOT:
        return sge.Not(this=operand)
    if unary.op == UnaryOpKind.NEG:
        return sge.Neg(this=operand)
    if unary.op == UnaryOpKind.IS_NULL:
        return sge.Is(this=operand, expression=sge.Null())
    if unary.op == UnaryOpKind.IS_NOT_NULL:
        return sge.Not(this=sge.Is(this=operand, expression=sge.Null()))
    raise ValueError(f"Unsupported unary operator: {unary.op}")


def _compile_func(func: FuncCall) -> sge.Expression:
    args = [_compile_expr(a) for a in func.args]
    name = func.func_name.upper()

    if name == "COALESCE":
        return sge.Coalesce(this=args[0], expressions=args[1:])
    if name == "CAST" and len(args) == 2 and isinstance(func.args[1], Literal):
        type_str = str(func.args[1].value)
        return sge.Cast(this=args[0], to=sge.DataType.build(type_str))
    if name == "LOWER":
        return sge.Lower(this=args[0])
    if name == "UPPER":
        return sge.Upper(this=args[0])
    if name == "DATE_TRUNC" and len(args) == 2:
        return sge.DateTrunc(this=args[1], unit=args[0])
    if name == "EXTRACT" and len(args) == 2 and isinstance(func.args[0], Literal):
        return sge.Extract(this=sge.Var(this=str(func.args[0].value)), expression=args[1])
    if name == "CONCAT" and len(args) == 2:
        return sge.DPipe(this=args[0], expression=args[1])
    if name == "STRFTIME" and len(args) >= 1:
        # Render as Anonymous to avoid sqlglot's TimeToStr normalization
        return sge.Anonymous(this="STRFTIME", expressions=args)
    if name == "GROUP_CONCAT":
        kwargs: dict[str, sge.Expression] = {"this": args[0]}
        if len(args) >= 2:
            kwargs["separator"] = args[1]
        return sge.GroupConcat(**kwargs)
    if name == "SUBSTR" or name == "SUBSTRING":
        kwargs: dict[str, sge.Expression] = {"this": args[0]}
        if len(args) >= 2:
            kwargs["start"] = args[1]
        if len(args) >= 3:
            kwargs["length"] = args[2]
        return sge.Substring(**kwargs)
    if name == "REPLACE" and len(args) >= 2:
        kwargs: dict[str, sge.Expression] = {"this": args[0], "expression": args[1]}
        if len(args) >= 3:
            kwargs["replacement"] = args[2]
        return sge.Replace(**kwargs)
    if name == "ROUND":
        kwargs: dict[str, sge.Expression] = {"this": args[0]}
        if len(args) >= 2:
            kwargs["decimals"] = args[1]
        return sge.Round(**kwargs)
    if name == "INSTR" and len(args) >= 2:
        kwargs: dict[str, sge.Expression] = {"this": args[0], "substr": args[1]}
        if len(args) >= 3:
            kwargs["position"] = args[2]
        return sge.StrPosition(**kwargs)
    if name == "IIF" and len(args) >= 3:
        return sge.If(this=args[0], true=args[1], false=args[2])
    if name == "NULLIF" and len(args) == 2:
        return sge.Nullif(this=args[0], expression=args[1])

    return sge.Anonymous(this=name, expressions=args)


def _compile_case(case: CaseExpr) -> sge.Expression:
    ifs = []
    for cw in case.whens:
        ifs.append(sge.If(
            this=_compile_expr(cw.when),
            true=_compile_expr(cw.then),
        ))
    default = _compile_expr(case.else_) if case.else_ is not None else None
    return sge.Case(ifs=ifs, default=default)


def _compile_in_list(in_list: InList) -> sge.Expression:
    expr = _compile_expr(in_list.expr)
    values = [_compile_expr(v) for v in in_list.values]
    return sge.In(this=expr, expressions=values)


def _compile_between(between: Between) -> sge.Expression:
    return sge.Between(
        this=_compile_expr(between.expr),
        low=_compile_expr(between.low),
        high=_compile_expr(between.high),
    )


_WINDOW_FUNC_MAP: dict[str, type[sge.Expression]] = {
    "ROW_NUMBER": sge.RowNumber,
    "RANK": sge.Rank,
    "DENSE_RANK": sge.DenseRank,
    "LAG": sge.Lag,
    "LEAD": sge.Lead,
    "COUNT": sge.Count,
    "SUM": sge.Sum,
    "AVG": sge.Avg,
    "MIN": sge.Min,
    "MAX": sge.Max,
    "FIRST_VALUE": sge.FirstValue,
    "LAST_VALUE": sge.LastValue,
}


def _compile_window(wf: WindowFunc) -> sge.Expression:
    inner_args = [_compile_expr(a) for a in wf.args]

    func_cls = _WINDOW_FUNC_MAP.get(wf.func_name.upper(), sge.Anonymous)
    if func_cls is sge.Anonymous:
        inner = sge.Anonymous(this=wf.func_name, expressions=inner_args)
    elif inner_args:
        arg = inner_args[0]
        if wf.distinct:
            arg = sge.Distinct(expressions=[arg])
        inner = func_cls(this=arg)
    else:
        inner = func_cls()

    partition = [_compile_expr(p) for p in wf.partition_by] or None
    order = sge.Order(expressions=[
        sge.Ordered(this=_compile_expr(s.expr), desc=(s.direction == SortDir.DESC))
        for s in wf.order_by
    ]) if wf.order_by else None

    return sge.Window(this=inner, partition_by=partition, order=order)
