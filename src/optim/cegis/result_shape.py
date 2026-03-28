"""Result-shape classifier for encoder dispatch.

Classifies queries by result shape to enable specialized encoding paths
in witness synthesis.
"""
from __future__ import annotations

from enum import Enum

from ..ir.types import (
    AggCall,
    BinOp,
    CaseExpr,
    Expr,
    FuncCall,
    QueryIR,
    UnaryOp,
    WindowFunc,
)


class ResultShape(str, Enum):
    SCALAR_AGG = "SCALAR_AGG"
    GROUPED_AGG = "GROUPED_AGG"
    BAG_ROWS = "BAG_ROWS"
    TOPK = "TOPK"
    WINDOWED = "WINDOWED"


def _has_window_functions(ir: QueryIR) -> bool:
    """Check if any SELECT expression contains a WindowFunc node."""
    for expr in ir.select:
        if _expr_contains_window(expr):
            return True
    return False


def _expr_contains_window(expr: Expr) -> bool:
    """Recursively check if an expression contains a WindowFunc."""
    if isinstance(expr, WindowFunc):
        return True
    if isinstance(expr, BinOp):
        return _expr_contains_window(expr.left) or _expr_contains_window(expr.right)
    if isinstance(expr, UnaryOp):
        return _expr_contains_window(expr.operand)
    if isinstance(expr, FuncCall):
        return any(_expr_contains_window(a) for a in expr.args)
    if isinstance(expr, AggCall):
        return _expr_contains_window(expr.arg) if expr.arg else False
    if isinstance(expr, CaseExpr):
        for cw in expr.whens:
            if _expr_contains_window(cw.when) or _expr_contains_window(cw.then):
                return True
        if expr.else_ is not None and _expr_contains_window(expr.else_):
            return True
    return False


def classify_result_shape(ir: QueryIR) -> ResultShape:
    """Classify a query by its result shape.

    Priority order:
    1. WINDOWED — has window functions in SELECT
    2. GROUPED_AGG — has aggregation + GROUP BY
    3. SCALAR_AGG — has aggregation, no GROUP BY
    4. TOPK — has ORDER BY + LIMIT
    5. BAG_ROWS — everything else
    """
    if _has_window_functions(ir):
        return ResultShape.WINDOWED
    if ir.has_aggregation() and ir.group_by:
        return ResultShape.GROUPED_AGG
    if ir.has_aggregation():
        return ResultShape.SCALAR_AGG
    if ir.order_by and ir.limit is not None:
        return ResultShape.TOPK
    return ResultShape.BAG_ROWS
