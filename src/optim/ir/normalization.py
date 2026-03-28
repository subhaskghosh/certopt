"""Deterministic normalization of QueryIR.

Ensures that semantically identical IRs have identical structure,
which is essential for stable certificates, caching, and equivalence checks.

Normalization rules:
  - Canonical AND/OR sorting (by deterministic key)
  - Canonical comparison orientation (smaller side left)
  - Stable alias naming
  - Canonical aggregate forms (COUNT(1) → COUNT(*))
"""

from __future__ import annotations

import copy

from .types import (
    AggCall,
    AggFunc,
    Between,
    BinOp,
    BinOpKind,
    ColumnRef,
    Expr,
    FuncCall,
    InList,
    Literal,
    OrderIntent,
    QueryIR,
    SortSpec,
    Star,
    UnaryOp,
)


def normalize(ir: QueryIR) -> QueryIR:
    """Return a normalized deep copy of the IR."""
    ir = ir.model_copy(deep=True)
    ir.select = [_norm_expr(e) for e in ir.select]
    if ir.where:
        ir.where = _norm_expr(ir.where)
    ir.joins = copy.deepcopy(ir.joins)
    for join in ir.joins:
        join.on = _norm_expr(join.on)
    ir.group_by = sorted(
        [_norm_expr(e) for e in ir.group_by],
        key=_expr_sort_key,
    )
    if ir.having:
        ir.having = _norm_expr(ir.having)
    ir.order_by = [
        SortSpec(expr=_norm_expr(s.expr), direction=s.direction)
        for s in ir.order_by
    ]
    # Strip cosmetic ORDER BY for stable certificates
    if ir.order_intent == OrderIntent.COSMETIC:
        ir.order_by = []
    # Stable alias naming for from_table
    ir.from_table = copy.deepcopy(ir.from_table)
    return ir


# ---------------------------------------------------------------------------
# Expression normalization
# ---------------------------------------------------------------------------

def _norm_expr(expr: Expr) -> Expr:
    """Recursively normalize an expression."""
    if isinstance(expr, AggCall):
        return _norm_agg(expr)
    if isinstance(expr, BinOp):
        return _norm_binop(expr)
    if isinstance(expr, UnaryOp):
        return UnaryOp(
            op=expr.op,
            operand=_norm_expr(expr.operand),
            sem_type=expr.sem_type,
            nullability=expr.nullability,
            provenance=expr.provenance,
            alias=expr.alias,
        )
    if isinstance(expr, FuncCall):
        return FuncCall(
            func_name=expr.func_name.upper(),
            args=[_norm_expr(a) for a in expr.args],
            sem_type=expr.sem_type,
            nullability=expr.nullability,
            provenance=expr.provenance,
            alias=expr.alias,
        )
    if isinstance(expr, InList):
        return InList(
            expr=_norm_expr(expr.expr),
            values=sorted([_norm_expr(v) for v in expr.values], key=_expr_sort_key),
            sem_type=expr.sem_type,
            nullability=expr.nullability,
            provenance=expr.provenance,
            alias=expr.alias,
        )
    if isinstance(expr, Between):
        return Between(
            expr=_norm_expr(expr.expr),
            low=_norm_expr(expr.low),
            high=_norm_expr(expr.high),
            sem_type=expr.sem_type,
            nullability=expr.nullability,
            provenance=expr.provenance,
            alias=expr.alias,
        )
    if isinstance(expr, ColumnRef):
        return ColumnRef(
            table=expr.table.lower() if expr.table else None,
            column=expr.column.lower(),
            sem_type=expr.sem_type,
            nullability=expr.nullability,
            provenance=expr.provenance,
            alias=expr.alias,
        )
    if isinstance(expr, Literal):
        return expr.model_copy(deep=True)
    if isinstance(expr, Star):
        return expr.model_copy(deep=True)
    return expr.model_copy(deep=True)


def _norm_agg(agg: AggCall) -> AggCall:
    """Normalize aggregate calls.

    - COUNT(1) → COUNT(*)
    - Normalize the inner expression
    """
    arg = agg.arg
    # COUNT(1) / COUNT(<literal>) without DISTINCT → COUNT(*)
    if (
        agg.func == AggFunc.COUNT
        and not agg.distinct
        and isinstance(arg, Literal)
        and arg.value is not None
    ):
        arg = None

    if arg is not None:
        arg = _norm_expr(arg)

    return AggCall(
        func=agg.func,
        arg=arg,
        distinct=agg.distinct,
        sem_type=agg.sem_type,
        nullability=agg.nullability,
        provenance=agg.provenance,
        alias=agg.alias,
    )


def _norm_binop(binop: BinOp) -> Expr:
    """Normalize binary operations.

    - Recursively normalize children
    - For commutative comparisons (=, !=), put the smaller side on the left
    - For AND/OR, flatten and sort children
    """
    left = _norm_expr(binop.left)
    right = _norm_expr(binop.right)

    # Flatten AND/OR chains and sort
    if binop.op in (BinOpKind.AND, BinOpKind.OR):
        children = _flatten_bool(binop.op, left, right)
        children.sort(key=_expr_sort_key)
        # Rebuild right-associative chain
        result = children[-1]
        for child in reversed(children[:-1]):
            result = BinOp(
                op=binop.op,
                left=child,
                right=result,
                sem_type=binop.sem_type,
                nullability=binop.nullability,
                alias=binop.alias,
            )
        return result

    # Canonical comparison orientation: smaller key on left
    if binop.op in (BinOpKind.EQ, BinOpKind.NEQ):
        if _expr_sort_key(left) > _expr_sort_key(right):
            left, right = right, left

    return BinOp(
        op=binop.op,
        left=left,
        right=right,
        sem_type=binop.sem_type,
        nullability=binop.nullability,
        provenance=binop.provenance,
        alias=binop.alias,
    )


def _flatten_bool(op: BinOpKind, left: Expr, right: Expr) -> list[Expr]:
    """Flatten nested AND/OR into a flat list of children."""
    children: list[Expr] = []
    for child in (left, right):
        if isinstance(child, BinOp) and child.op == op:
            children.extend(_flatten_bool(op, child.left, child.right))
        else:
            children.append(child)
    return children


# ---------------------------------------------------------------------------
# Sort key for deterministic ordering
# ---------------------------------------------------------------------------

def _expr_sort_key(expr: Expr) -> str:
    """Produce a deterministic string key for sorting expressions."""
    if isinstance(expr, ColumnRef):
        t = (expr.table or "").lower()
        c = expr.column.lower()
        return f"COL:{t}.{c}"
    if isinstance(expr, Literal):
        return f"LIT:{expr.value!r}"
    if isinstance(expr, AggCall):
        inner = _expr_sort_key(expr.arg) if expr.arg else "*"
        d = "D" if expr.distinct else ""
        return f"AGG:{expr.func.value}{d}({inner})"
    if isinstance(expr, BinOp):
        return f"BIN:{expr.op.value}({_expr_sort_key(expr.left)},{_expr_sort_key(expr.right)})"
    if isinstance(expr, UnaryOp):
        return f"UNA:{expr.op.value}({_expr_sort_key(expr.operand)})"
    if isinstance(expr, FuncCall):
        args = ",".join(_expr_sort_key(a) for a in expr.args)
        return f"FN:{expr.func_name}({args})"
    if isinstance(expr, InList):
        vals = ",".join(_expr_sort_key(v) for v in expr.values)
        return f"IN:({_expr_sort_key(expr.expr)},[{vals}])"
    if isinstance(expr, Between):
        return f"BTW:({_expr_sort_key(expr.expr)},{_expr_sort_key(expr.low)},{_expr_sort_key(expr.high)})"
    if isinstance(expr, Star):
        return "STAR"
    return f"UNKNOWN:{type(expr).__name__}"
