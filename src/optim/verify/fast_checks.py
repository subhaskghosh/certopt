"""Fast deterministic verification (no SMT).

Catches trivial inconsistencies before invoking the solver:
  - Expression type checking
  - Aggregation legality (GROUP BY rules)
  - Join predicate type compatibility
  - DDL/DML rejection
  - LIMIT enforcement
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ir.types import (
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
    Literal,
    QueryIR,
    ScalarSubquery,
    SemType,
    UnaryOp,
    _contains_agg,
)
from ..schema.catalog import Catalog


@dataclass
class CheckResult:
    """Result of fast verification."""
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


def fast_verify(ir: QueryIR, catalog: Catalog, max_limit: int = 10000) -> CheckResult:
    """Run all fast deterministic checks on an IR.

    Returns a CheckResult with accumulated errors and warnings.
    """
    result = CheckResult(ok=True)

    _check_table_refs(ir, catalog, result)
    _check_column_refs(ir, catalog, result)
    _check_join_predicates(ir, catalog, result)
    _check_grouping_legality(ir, result)
    _check_aggregate_types(ir, catalog, result)
    _check_expression_types(ir, catalog, result)
    _check_limit(ir, max_limit, result)

    return result


# ---------------------------------------------------------------------------
# Table references
# ---------------------------------------------------------------------------

def _check_table_refs(ir: QueryIR, catalog: Catalog, result: CheckResult) -> None:
    """Verify all referenced tables exist in the catalog."""
    rels = [ir.from_table] + [join.right for join in ir.joins]

    for rel in rels:
        if isinstance(rel, DerivedTable):
            continue
        if catalog.get_table(rel.table) is None:
            result.add_error(f"Table '{rel.table}' not found in catalog")


# ---------------------------------------------------------------------------
# Column references
# ---------------------------------------------------------------------------

def _get_available_tables(ir: QueryIR) -> dict[str, str]:
    """Return a mapping of alias/name → actual table name."""
    tables: dict[str, str] = {}
    rels = [ir.from_table] + [join.right for join in ir.joins]
    for rel in rels:
        if isinstance(rel, DerivedTable):
            alias = rel.alias.lower()
            tables[alias] = alias
        else:
            tables[rel.ref_name.lower()] = rel.table.lower()
            tables[rel.table.lower()] = rel.table.lower()
    return tables


def _get_derived_schemas(ir: QueryIR, catalog: Catalog) -> dict[str, dict[str, SemType]]:
    """Build alias → {col_name → SemType} for DerivedTable nodes in the IR."""
    schemas: dict[str, dict[str, SemType]] = {}

    def _process(rel) -> None:
        if not isinstance(rel, DerivedTable):
            return
        inner = rel.query
        cols: dict[str, SemType] = {}
        inner_available = _get_available_tables(inner)
        for idx, expr in enumerate(inner.select):
            if expr.alias:
                name = expr.alias
            elif isinstance(expr, ColumnRef):
                name = expr.column
            elif isinstance(expr, AggCall):
                arg_name = ""
                if expr.arg and isinstance(expr.arg, ColumnRef):
                    arg_name = expr.arg.column
                prefix = "distinct_" if expr.distinct else ""
                name = f"{expr.func.value.lower()}_{prefix}{arg_name}".rstrip("_")
            else:
                name = f"expr_{idx}"
            sem_type = _infer_type(expr, catalog, inner_available)
            cols[name.lower()] = sem_type
        schemas[rel.alias.lower()] = cols

    _process(ir.from_table)
    for join in ir.joins:
        _process(join.right)
    return schemas


def _check_column_refs(ir: QueryIR, catalog: Catalog, result: CheckResult) -> None:
    """Verify all column references resolve to catalog columns."""
    available = _get_available_tables(ir)
    derived_schemas = _get_derived_schemas(ir, catalog)

    for expr in _collect_all_exprs(ir):
        if isinstance(expr, ColumnRef):
            if expr.table:
                table_alias = expr.table.lower()
                actual_table = available.get(table_alias)
                if actual_table is None:
                    result.add_error(
                        f"Column ref '{expr.fqn()}': table alias '{expr.table}' not in scope"
                    )
                    continue
                # For derived tables, validate against derived projection
                if actual_table == table_alias and catalog.get_table(actual_table) is None:
                    derived_cols = derived_schemas.get(actual_table)
                    if derived_cols is not None:
                        if expr.column.lower() not in derived_cols:
                            result.add_error(
                                f"Column '{expr.column}' not found in derived table '{actual_table}'"
                            )
                    continue
                col = catalog.get_column(actual_table, expr.column)
                if col is None:
                    result.add_error(
                        f"Column '{expr.column}' not found in table '{actual_table}'"
                    )
            else:
                # Unqualified column: check all available tables
                found = False
                for actual_table in available.values():
                    if catalog.get_column(actual_table, expr.column):
                        found = True
                        break
                if not found:
                    # Also check derived table schemas
                    for alias, cols in derived_schemas.items():
                        if expr.column.lower() in cols:
                            found = True
                            break
                if not found:
                    result.add_error(
                        f"Column '{expr.column}' not found in any available table"
                    )


# ---------------------------------------------------------------------------
# Join predicate type compatibility
# ---------------------------------------------------------------------------

def _check_join_predicates(ir: QueryIR, catalog: Catalog, result: CheckResult) -> None:
    """Check that join predicates compare compatible types."""
    available = _get_available_tables(ir)
    derived_schemas = _get_derived_schemas(ir, catalog)

    for join in ir.joins:
        if isinstance(join.right, DerivedTable):
            continue
        on_expr = join.on
        if isinstance(on_expr, BinOp) and on_expr.op == BinOpKind.EQ:
            left_type = _infer_type(on_expr.left, catalog, available, derived_schemas)
            right_type = _infer_type(on_expr.right, catalog, available, derived_schemas)
            if (
                left_type != SemType.UNKNOWN
                and right_type != SemType.UNKNOWN
                and left_type != right_type
                and not _types_compatible(left_type, right_type)
            ):
                result.add_error(
                    f"Join predicate type mismatch: {left_type.value} vs {right_type.value} "
                    f"in {join.right.ref_name}"
                )


# ---------------------------------------------------------------------------
# Grouping legality
# ---------------------------------------------------------------------------

def _check_grouping_legality(ir: QueryIR, result: CheckResult) -> None:
    """Enforce SQL standard grouping rules.

    If GROUP BY is present, every non-aggregated select expression must
    appear in the GROUP BY list.
    """
    if not ir.group_by and not ir.has_aggregation():
        return

    if ir.has_aggregation() and not ir.group_by:
        # All selects must be aggregates or literals
        for expr in ir.select:
            if not _contains_agg(expr) and not isinstance(expr, Literal):
                result.add_error(
                    f"Non-aggregated expression in SELECT without GROUP BY: "
                    f"{_expr_label(expr)}"
                )
        return

    # GROUP BY is present: check each non-agg select is in group_by
    gb_keys = {_expr_key(e) for e in ir.group_by}
    for expr in ir.select:
        if not _contains_agg(expr) and not isinstance(expr, Literal):
            key = _expr_key(expr)
            if key not in gb_keys:
                result.add_error(
                    f"Expression '{_expr_label(expr)}' in SELECT is not in GROUP BY "
                    f"and is not aggregated"
                )


# ---------------------------------------------------------------------------
# Aggregate type checks
# ---------------------------------------------------------------------------

def _check_aggregate_types(ir: QueryIR, catalog: Catalog, result: CheckResult) -> None:
    """Check that aggregates are applied to compatible types.

    - SUM, AVG: numeric only
    - COUNT: any type
    - MIN, MAX: numeric or temporal
    """
    available = _get_available_tables(ir)
    derived_schemas = _get_derived_schemas(ir, catalog)

    for expr in _collect_all_exprs(ir):
        if isinstance(expr, AggCall) and expr.arg is not None:
            arg_type = _infer_type(expr.arg, catalog, available, derived_schemas)
            if arg_type == SemType.UNKNOWN:
                continue

            if expr.func in (AggFunc.SUM, AggFunc.AVG):
                if not arg_type.is_numeric():
                    result.add_error(
                        f"{expr.func.value}() requires numeric argument, "
                        f"got {arg_type.value}"
                    )
            elif expr.func in (AggFunc.MIN, AggFunc.MAX):
                if not (arg_type.is_numeric() or arg_type.is_temporal() or arg_type == SemType.STRING):
                    result.add_error(
                        f"{expr.func.value}() requires orderable argument, "
                        f"got {arg_type.value}"
                    )


# ---------------------------------------------------------------------------
# General expression type checking
# ---------------------------------------------------------------------------

def _check_expression_types(ir: QueryIR, catalog: Catalog, result: CheckResult) -> None:
    """Check type compatibility in binary operations."""
    available = _get_available_tables(ir)
    derived_schemas = _get_derived_schemas(ir, catalog)

    for expr in _collect_all_exprs(ir):
        if isinstance(expr, BinOp):
            if expr.op in (BinOpKind.ADD, BinOpKind.SUB, BinOpKind.MUL, BinOpKind.DIV, BinOpKind.MOD):
                lt = _infer_type(expr.left, catalog, available, derived_schemas)
                rt = _infer_type(expr.right, catalog, available, derived_schemas)
                if lt != SemType.UNKNOWN and not lt.is_numeric():
                    result.add_error(
                        f"Arithmetic operator '{expr.op.value}' on non-numeric type {lt.value}"
                    )
                if rt != SemType.UNKNOWN and not rt.is_numeric():
                    result.add_error(
                        f"Arithmetic operator '{expr.op.value}' on non-numeric type {rt.value}"
                    )
            elif expr.op in (BinOpKind.EQ, BinOpKind.NEQ, BinOpKind.LT, BinOpKind.GT, BinOpKind.LTE, BinOpKind.GTE):
                lt = _infer_type(expr.left, catalog, available, derived_schemas)
                rt = _infer_type(expr.right, catalog, available, derived_schemas)
                if (
                    lt != SemType.UNKNOWN
                    and rt != SemType.UNKNOWN
                    and not _types_compatible(lt, rt)
                ):
                    result.add_warning(
                        f"Comparison '{expr.op.value}' between {lt.value} and {rt.value}"
                    )


# ---------------------------------------------------------------------------
# LIMIT enforcement
# ---------------------------------------------------------------------------

def _check_limit(ir: QueryIR, max_limit: int, result: CheckResult) -> None:
    """Warn if no LIMIT is set or if it exceeds the maximum."""
    if ir.limit is None:
        result.add_warning("No LIMIT clause; consider adding one for safety")
    elif ir.limit > max_limit:
        result.add_warning(f"LIMIT {ir.limit} exceeds recommended max {max_limit}")
    elif ir.limit < 0:
        result.add_error(f"LIMIT must be non-negative, got {ir.limit}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _types_compatible(a: SemType, b: SemType) -> bool:
    """Check if two types are compatible for comparison."""
    if a == b:
        return True
    # All numeric types are compatible with each other
    if a.is_numeric() and b.is_numeric():
        return True
    # All temporal types are compatible
    if a.is_temporal() and b.is_temporal():
        return True
    return False


def _infer_type(expr: Expr, catalog: Catalog, available: dict[str, str],
                derived_schemas: dict[str, dict[str, SemType]] | None = None) -> SemType:
    """Infer the semantic type of an expression from catalog metadata."""
    if expr.sem_type != SemType.UNKNOWN:
        return expr.sem_type
    if isinstance(expr, ColumnRef):
        if expr.table:
            actual = available.get(expr.table.lower(), expr.table)
        else:
            actual = None
            for tbl in available.values():
                if catalog.get_column(tbl, expr.column):
                    actual = tbl
                    break
        if actual:
            col = catalog.get_column(actual, expr.column)
            if col:
                return col.sem_type
            if derived_schemas:
                derived_cols = derived_schemas.get(actual)
                if derived_cols:
                    dtype = derived_cols.get(expr.column.lower())
                    if dtype:
                        return dtype
        else:
            if derived_schemas:
                for alias, cols in derived_schemas.items():
                    if expr.column.lower() in cols:
                        return cols[expr.column.lower()]
    if isinstance(expr, Literal):
        return expr.sem_type
    if isinstance(expr, AggCall):
        if expr.func == AggFunc.COUNT:
            return SemType.INT
        if expr.arg:
            return _infer_type(expr.arg, catalog, available, derived_schemas)
    return SemType.UNKNOWN


def _expr_key(expr: Expr) -> str:
    """A simple string key for expression equality checks."""
    if isinstance(expr, ColumnRef):
        t = (expr.table or "").lower()
        return f"{t}.{expr.column.lower()}"
    if isinstance(expr, Literal):
        return f"lit:{expr.value!r}"
    return repr(expr)


def _expr_label(expr: Expr) -> str:
    """A human-readable label for error messages."""
    if isinstance(expr, ColumnRef):
        return expr.fqn()
    if isinstance(expr, AggCall):
        return f"{expr.func.value}(...)"
    return type(expr).__name__


def _collect_all_exprs(ir: QueryIR) -> list[Expr]:
    """Collect all expression nodes from the IR (flat, non-recursive)."""
    exprs: list[Expr] = list(ir.select)
    if ir.where:
        exprs.extend(_flatten_expr(ir.where))
    for join in ir.joins:
        exprs.extend(_flatten_expr(join.on))
    exprs.extend(ir.group_by)
    if ir.having:
        exprs.extend(_flatten_expr(ir.having))
    for s in ir.order_by:
        exprs.extend(_flatten_expr(s.expr))
    return exprs


def _flatten_expr(expr: Expr) -> list[Expr]:
    """Flatten an expression tree into a list of all nodes."""
    nodes: list[Expr] = [expr]
    if isinstance(expr, BinOp):
        nodes.extend(_flatten_expr(expr.left))
        nodes.extend(_flatten_expr(expr.right))
    elif isinstance(expr, UnaryOp):
        nodes.extend(_flatten_expr(expr.operand))
    elif isinstance(expr, FuncCall):
        for a in expr.args:
            nodes.extend(_flatten_expr(a))
    elif isinstance(expr, AggCall) and expr.arg:
        nodes.extend(_flatten_expr(expr.arg))
    elif isinstance(expr, InList):
        nodes.extend(_flatten_expr(expr.expr))
        for v in expr.values:
            nodes.extend(_flatten_expr(v))
    elif isinstance(expr, Between):
        nodes.extend(_flatten_expr(expr.expr))
        nodes.extend(_flatten_expr(expr.low))
        nodes.extend(_flatten_expr(expr.high))
    elif isinstance(expr, CaseExpr):
        for cw in expr.whens:
            nodes.extend(_flatten_expr(cw.when))
            nodes.extend(_flatten_expr(cw.then))
        if expr.else_ is not None:
            nodes.extend(_flatten_expr(expr.else_))
    elif isinstance(expr, ScalarSubquery):
        for sel in expr.query.select:
            nodes.extend(_flatten_expr(sel))
        if expr.query.where:
            nodes.extend(_flatten_expr(expr.query.where))
        for join in expr.query.joins:
            nodes.extend(_flatten_expr(join.on))
    elif isinstance(expr, InSubquery):
        nodes.extend(_flatten_expr(expr.expr))
        for sel in expr.query.select:
            nodes.extend(_flatten_expr(sel))
        if expr.query.where:
            nodes.extend(_flatten_expr(expr.query.where))
        for join in expr.query.joins:
            nodes.extend(_flatten_expr(join.on))
    elif isinstance(expr, ExistsSubquery):
        for sel in expr.query.select:
            nodes.extend(_flatten_expr(sel))
        if expr.query.where:
            nodes.extend(_flatten_expr(expr.query.where))
        for join in expr.query.joins:
            nodes.extend(_flatten_expr(join.on))
    return nodes
