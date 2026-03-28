"""Typed Intermediate Representation for NL→SQL.

The IR captures query intent as a structured, typed, dialect-neutral tree.
SQL is rendered *from* the IR (via a compiler), never reasoned about directly.
Every node carries semantic type, nullability, and provenance information.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Optional, Union

from pydantic import BaseModel, Discriminator, Field, Tag


# ---------------------------------------------------------------------------
# Semantic types
# ---------------------------------------------------------------------------

class SemType(str, Enum):
    """Semantic type for expressions."""
    INT = "INT"
    FLOAT = "FLOAT"
    DECIMAL = "DECIMAL"
    BOOL = "BOOL"
    STRING = "STRING"
    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"
    UNKNOWN = "UNKNOWN"

    def is_numeric(self) -> bool:
        return self in (SemType.INT, SemType.FLOAT, SemType.DECIMAL)

    def is_temporal(self) -> bool:
        return self in (SemType.DATE, SemType.TIMESTAMP)


class Nullability(str, Enum):
    NOT_NULL = "NOT_NULL"
    NULLABLE = "NULLABLE"
    UNKNOWN = "UNKNOWN"


class JoinType(str, Enum):
    INNER = "INNER"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    FULL = "FULL"
    CROSS = "CROSS"


class SetOpKind(str, Enum):
    """SQL set operation type."""
    UNION = "UNION"
    UNION_ALL = "UNION_ALL"
    INTERSECT = "INTERSECT"
    EXCEPT = "EXCEPT"


class AggFunc(str, Enum):
    COUNT = "COUNT"
    SUM = "SUM"
    AVG = "AVG"
    MIN = "MIN"
    MAX = "MAX"


class BinOpKind(str, Enum):
    EQ = "="
    NEQ = "!="
    LT = "<"
    GT = ">"
    LTE = "<="
    GTE = ">="
    ADD = "+"
    SUB = "-"
    MUL = "*"
    DIV = "/"
    MOD = "%"
    AND = "AND"
    OR = "OR"
    LIKE = "LIKE"
    IN = "IN"
    IS = "IS"
    IS_NOT_DISTINCT_FROM = "IS_NOT_DISTINCT_FROM"
    IS_DISTINCT_FROM = "IS_DISTINCT_FROM"


class UnaryOpKind(str, Enum):
    NOT = "NOT"
    NEG = "-"
    IS_NULL = "IS_NULL"
    IS_NOT_NULL = "IS_NOT_NULL"


class SortDir(str, Enum):
    ASC = "ASC"
    DESC = "DESC"


class OrderIntent(str, Enum):
    """Whether ordering is semantically required (top-k) or cosmetic."""
    ESSENTIAL = "ESSENTIAL"
    COSMETIC = "COSMETIC"


class IntentRole(str, Enum):
    """Role of an expression in the query intent."""
    METRIC = "METRIC"
    DIMENSION = "DIMENSION"
    FILTER = "FILTER"
    ORDERING = "ORDERING"
    OTHER = "OTHER"


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

class Provenance(BaseModel):
    """Tracks where in the NL utterance this IR node originates."""
    source_span: Optional[tuple[int, int]] = None
    nl_fragment: Optional[str] = None
    intent_role: IntentRole = IntentRole.OTHER


# ---------------------------------------------------------------------------
# Expression nodes
# ---------------------------------------------------------------------------

class Expr(BaseModel):
    """Base class for all expression nodes."""
    expr_type: str = "expr"
    sem_type: SemType = SemType.UNKNOWN
    nullability: Nullability = Nullability.UNKNOWN
    provenance: Optional[Provenance] = None
    alias: Optional[str] = None


class ColumnRef(Expr):
    """Reference to a table column: table.column."""
    expr_type: str = "column_ref"
    table: Optional[str] = None
    column: str

    def fqn(self) -> str:
        if self.table:
            return f"{self.table}.{self.column}"
        return self.column


class Literal(Expr):
    """A literal value."""
    expr_type: str = "literal"
    value: int | float | str | bool | None
    nullability: Nullability = Nullability.NOT_NULL

    def model_post_init(self, __context: object) -> None:
        if self.value is None:
            self.nullability = Nullability.NULLABLE
            if self.sem_type == SemType.UNKNOWN:
                self.sem_type = SemType.UNKNOWN
        elif self.sem_type != SemType.UNKNOWN:
            # FIX.28a: Preserve explicitly-set sem_type (e.g., DATE literals
            # stored as strings but typed as SemType.DATE).
            pass
        elif isinstance(self.value, bool):
            self.sem_type = SemType.BOOL
        elif isinstance(self.value, int):
            self.sem_type = SemType.INT
        elif isinstance(self.value, float):
            self.sem_type = SemType.FLOAT
        elif isinstance(self.value, str):
            self.sem_type = SemType.STRING


class BinOp(Expr):
    """Binary operation: left op right."""
    expr_type: str = "bin_op"
    op: BinOpKind
    left: ExprUnion
    right: ExprUnion


class UnaryOp(Expr):
    """Unary operation."""
    expr_type: str = "unary_op"
    op: UnaryOpKind
    operand: ExprUnion


class FuncCall(Expr):
    """Scalar function call: LOWER(x), DATE_TRUNC('month', x), COALESCE(a, b), CAST(x AS T)."""
    expr_type: str = "func_call"
    func_name: str
    args: list[ExprUnion]


class AggCall(Expr):
    """Aggregate function call: SUM(x), COUNT(*), COUNT(DISTINCT x)."""
    expr_type: str = "agg_call"
    func: AggFunc
    arg: Optional[ExprUnion] = None  # None means COUNT(*)
    distinct: bool = False


class Star(Expr):
    """Represents * in COUNT(*) or SELECT *."""
    expr_type: str = "star"


class InList(Expr):
    """expr IN (v1, v2, ...)."""
    expr_type: str = "in_list"
    expr: ExprUnion
    values: list[ExprUnion]


class Between(Expr):
    """expr BETWEEN low AND high."""
    expr_type: str = "between"
    expr: ExprUnion
    low: ExprUnion
    high: ExprUnion


class ScalarSubquery(Expr):
    """Scalar subquery: (SELECT ... ) used in expression position.

    The inner query must return exactly one row and one column.
    Used for patterns like: WHERE col = (SELECT MAX(x) FROM t2)
    """
    expr_type: str = "scalar_subquery"
    query: "QueryIR"


class InSubquery(Expr):
    """expr IN (SELECT ...) — membership test against a subquery result.

    Used for patterns like: WHERE col IN (SELECT x FROM t2 WHERE ...)
    """
    expr_type: str = "in_subquery"
    expr: ExprUnion
    query: "QueryIR"


class ExistsSubquery(Expr):
    """EXISTS (SELECT ...) — boolean test for subquery non-emptiness.

    Used for patterns like: WHERE EXISTS (SELECT 1 FROM t2 WHERE t2.id = t1.id)
    """
    expr_type: str = "exists_subquery"
    query: "QueryIR"


class CaseWhen(BaseModel):
    """A single WHEN condition THEN result pair."""
    when: ExprUnion
    then: ExprUnion


class CaseExpr(Expr):
    """CASE WHEN cond1 THEN val1 WHEN cond2 THEN val2 ELSE val3 END."""
    expr_type: str = "case_expr"
    whens: list[CaseWhen]
    else_: Optional[ExprUnion] = None


class WindowFrameBoundKind(str, Enum):
    UNBOUNDED_PRECEDING = "UNBOUNDED_PRECEDING"
    PRECEDING = "PRECEDING"
    CURRENT_ROW = "CURRENT_ROW"
    FOLLOWING = "FOLLOWING"
    UNBOUNDED_FOLLOWING = "UNBOUNDED_FOLLOWING"


class WindowFrameBound(BaseModel):
    kind: WindowFrameBoundKind
    offset: Optional[int] = None


class WindowFrame(BaseModel):
    unit: str = "ROWS"  # "ROWS" or "RANGE"
    start: WindowFrameBound
    end: Optional[WindowFrameBound] = None


class WindowFunc(Expr):
    """Window function: func(args) OVER (PARTITION BY ... ORDER BY ... frame)."""
    expr_type: str = "window_func"
    func_name: str
    args: list["ExprUnion"] = Field(default_factory=list)
    partition_by: list["ExprUnion"] = Field(default_factory=list)
    order_by: list["SortSpec"] = Field(default_factory=list)
    frame: Optional[WindowFrame] = None
    distinct: bool = False


# ---------------------------------------------------------------------------
# Discriminated union for polymorphic Expr deserialization
# ---------------------------------------------------------------------------

def _expr_discriminator(v: dict | Expr) -> str:
    if isinstance(v, dict):
        return v.get("expr_type", "expr")
    return getattr(v, "expr_type", "expr")


ExprUnion = Annotated[
    Union[
        Annotated[ColumnRef, Tag("column_ref")],
        Annotated[Literal, Tag("literal")],
        Annotated[BinOp, Tag("bin_op")],
        Annotated[UnaryOp, Tag("unary_op")],
        Annotated[FuncCall, Tag("func_call")],
        Annotated[AggCall, Tag("agg_call")],
        Annotated[Star, Tag("star")],
        Annotated[InList, Tag("in_list")],
        Annotated[Between, Tag("between")],
        Annotated[ScalarSubquery, Tag("scalar_subquery")],
        Annotated[InSubquery, Tag("in_subquery")],
        Annotated[ExistsSubquery, Tag("exists_subquery")],
        Annotated[CaseExpr, Tag("case_expr")],
        Annotated[WindowFunc, Tag("window_func")],
        Annotated[Expr, Tag("expr")],
    ],
    Discriminator(_expr_discriminator),
]

# NOTE: model_rebuild() calls are deferred to after QueryIR is defined
# (because ExprUnion now includes ScalarSubquery/InSubquery which reference QueryIR)


# ---------------------------------------------------------------------------
# Relation / Join nodes
# ---------------------------------------------------------------------------

class RelRef(BaseModel):
    """Reference to a base table."""
    table: str
    alias: Optional[str] = None

    @property
    def ref_name(self) -> str:
        return self.alias or self.table


class DerivedTable(BaseModel):
    """Derived table: (SELECT ...) AS alias in FROM/JOIN position.

    Wraps a nested QueryIR with a required alias.
    """
    query: "QueryIR"
    alias: str
    column_aliases: list[str] = Field(default_factory=list)

    @property
    def ref_name(self) -> str:
        return self.alias


# Union type for relation references (base table or derived table)
RelationUnion = Union[RelRef, DerivedTable]


class JoinClause(BaseModel):
    """A single JOIN clause."""
    join_type: JoinType = JoinType.INNER
    right: RelationUnion
    on: ExprUnion


# ---------------------------------------------------------------------------
# Grain (what keys define result rows)
# ---------------------------------------------------------------------------

class Grain(BaseModel):
    """The logical grain of the result set."""
    keys: list[str] = Field(default_factory=list)
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Sort specification
# ---------------------------------------------------------------------------

class SortSpec(BaseModel):
    expr: ExprUnion
    direction: SortDir = SortDir.ASC


# ---------------------------------------------------------------------------
# Top-level QueryIR
# ---------------------------------------------------------------------------

class QueryIR(BaseModel):
    """Complete intermediate representation of a SQL query.

    This is the single semantic backbone: verification, rendering,
    and witness synthesis all operate on this structure.
    """
    select: list[ExprUnion]
    from_table: RelationUnion
    joins: list[JoinClause] = Field(default_factory=list)
    where: Optional[ExprUnion] = None
    group_by: list[ExprUnion] = Field(default_factory=list)
    having: Optional[ExprUnion] = None
    order_by: list[SortSpec] = Field(default_factory=list)
    limit: Optional[int] = None
    distinct: bool = False

    # Set operations: self UNION/INTERSECT/EXCEPT set_right
    set_op: Optional[SetOpKind] = None
    set_right: Optional["QueryIR"] = None

    # Semantic annotations
    grain: Optional[Grain] = None
    order_intent: OrderIntent = OrderIntent.COSMETIC

    # Metadata
    provenance: Optional[Provenance] = None
    confidence: Optional[float] = None
    rationale: Optional[str] = None

    def has_aggregation(self) -> bool:
        """Check if any select expression contains an aggregate."""
        return any(_contains_agg(expr) for expr in self.select)

    def projected_columns(self) -> list[str]:
        """Return the alias or inferred name for each select expression."""
        names: list[str] = []
        for expr in self.select:
            if expr.alias:
                names.append(expr.alias)
            elif isinstance(expr, ColumnRef):
                names.append(expr.column)
            elif isinstance(expr, AggCall):
                arg_name = ""
                if expr.arg and isinstance(expr.arg, ColumnRef):
                    arg_name = expr.arg.column
                prefix = "distinct_" if expr.distinct else ""
                names.append(f"{expr.func.value.lower()}_{prefix}{arg_name}".rstrip("_"))
            else:
                names.append(f"expr_{len(names)}")
        return names


# Rebuild ALL models now that QueryIR is defined (resolves forward refs)
BinOp.model_rebuild()
UnaryOp.model_rebuild()
FuncCall.model_rebuild()
AggCall.model_rebuild()
InList.model_rebuild()
Between.model_rebuild()
ScalarSubquery.model_rebuild()
InSubquery.model_rebuild()
ExistsSubquery.model_rebuild()
CaseWhen.model_rebuild()
CaseExpr.model_rebuild()
WindowFunc.model_rebuild()
DerivedTable.model_rebuild()
JoinClause.model_rebuild()
SortSpec.model_rebuild()


def _contains_agg(expr: Expr) -> bool:
    """Recursively check if an expression contains an AggCall."""
    if isinstance(expr, AggCall):
        return True
    if isinstance(expr, BinOp):
        return _contains_agg(expr.left) or _contains_agg(expr.right)
    if isinstance(expr, UnaryOp):
        return _contains_agg(expr.operand)
    if isinstance(expr, FuncCall):
        return any(_contains_agg(a) for a in expr.args)
    if isinstance(expr, InSubquery):
        return _contains_agg(expr.expr)
    if isinstance(expr, ExistsSubquery):
        return False
    if isinstance(expr, CaseExpr):
        for cw in expr.whens:
            if _contains_agg(cw.when) or _contains_agg(cw.then):
                return True
        if expr.else_ is not None and _contains_agg(expr.else_):
            return True
    return False
