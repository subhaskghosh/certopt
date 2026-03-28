"""Tests for E.1 — window frame exact encoding (ROWS BETWEEN).

Tests verify that aggregate window functions with frame clauses produce
exact Z3 encodings, enabling correct SAT/UNSAT results for running
totals, bounded intervals, and value functions (FIRST_VALUE/LAST_VALUE).
"""

from optim.cegis.witness_synthesis import (
    BoundedScope,
    synthesize_witness,
)
from optim.ir.types import (
    AggCall,
    AggFunc,
    BinOp,
    BinOpKind,
    ColumnRef,
    Literal,
    QueryIR,
    RelRef,
    SemType,
    SortDir,
    SortSpec,
    WindowFrame,
    WindowFrameBound,
    WindowFrameBoundKind,
    WindowFunc,
)
from optim.schema.catalog import Catalog, ColumnInfo, TableInfo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _catalog() -> Catalog:
    return Catalog(
        tables={
            "t": TableInfo(
                name="t",
                columns=[
                    ColumnInfo(name="id", sem_type=SemType.INT, nullable=False, is_primary_key=True),
                    ColumnInfo(name="val", sem_type=SemType.INT, nullable=False),
                    ColumnInfo(name="grp", sem_type=SemType.INT, nullable=False),
                ],
                primary_keys=["id"],
            ),
        },
    )


def _scope() -> BoundedScope:
    return BoundedScope(k_rows=3, int_bounds=(0, 10), solver_timeout_ms=15_000)


def _col(name: str) -> ColumnRef:
    return ColumnRef(table="t", column=name, sem_type=SemType.INT)


def _running_sum_frame() -> WindowFrame:
    """ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW."""
    return WindowFrame(
        unit="ROWS",
        start=WindowFrameBound(kind=WindowFrameBoundKind.UNBOUNDED_PRECEDING),
        end=WindowFrameBound(kind=WindowFrameBoundKind.CURRENT_ROW),
    )


def _full_partition_frame() -> WindowFrame:
    """ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING."""
    return WindowFrame(
        unit="ROWS",
        start=WindowFrameBound(kind=WindowFrameBoundKind.UNBOUNDED_PRECEDING),
        end=WindowFrameBound(kind=WindowFrameBoundKind.UNBOUNDED_FOLLOWING),
    )


# ---------------------------------------------------------------------------
# W1: Running aggregates (UNBOUNDED PRECEDING ... CURRENT ROW)
# ---------------------------------------------------------------------------

def test_running_sum_self_equivalence():
    """SUM(val) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    vs itself — UNSAT."""
    catalog = _catalog()
    wf = WindowFunc(
        func_name="SUM",
        args=[_col("val")],
        order_by=[SortSpec(expr=_col("id"), direction=SortDir.ASC)],
        frame=_running_sum_frame(),
    )
    q1 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_running_sum_vs_full_partition_sum_sat():
    """SUM(val) OVER (ORDER BY id ROWS UNBOUNDED PRECEDING..CURRENT ROW)
    vs SUM(val) OVER (PARTITION BY 1) [full partition] — SAT.

    Running sum ≠ total sum unless there's only one row."""
    catalog = _catalog()
    wf_running = WindowFunc(
        func_name="SUM",
        args=[_col("val")],
        order_by=[SortSpec(expr=_col("id"), direction=SortDir.ASC)],
        frame=_running_sum_frame(),
    )
    wf_full = WindowFunc(
        func_name="SUM",
        args=[_col("val")],
        # No frame → full partition
    )
    q1 = QueryIR(select=[wf_running], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf_full], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "sat"


def test_running_count_self_equivalence():
    """COUNT(*) OVER (ORDER BY id ROWS UNBOUNDED PRECEDING..CURRENT ROW) — UNSAT."""
    catalog = _catalog()
    wf = WindowFunc(
        func_name="COUNT",
        args=[],
        order_by=[SortSpec(expr=_col("id"), direction=SortDir.ASC)],
        frame=_running_sum_frame(),
    )
    q1 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_running_min_self_equivalence():
    """MIN(val) OVER (ORDER BY id ROWS UNBOUNDED PRECEDING..CURRENT ROW) — UNSAT."""
    catalog = _catalog()
    wf = WindowFunc(
        func_name="MIN",
        args=[_col("val")],
        order_by=[SortSpec(expr=_col("id"), direction=SortDir.ASC)],
        frame=_running_sum_frame(),
    )
    q1 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_running_max_vs_running_min_sat():
    """Running MAX vs Running MIN — SAT when values differ."""
    catalog = _catalog()
    frame = _running_sum_frame()
    order = [SortSpec(expr=_col("id"), direction=SortDir.ASC)]
    wf_max = WindowFunc(func_name="MAX", args=[_col("val")], order_by=order, frame=frame)
    wf_min = WindowFunc(func_name="MIN", args=[_col("val")], order_by=order, frame=frame)
    q1 = QueryIR(select=[wf_max], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf_min], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "sat"


# ---------------------------------------------------------------------------
# W1: Explicit full-partition frame = no frame
# ---------------------------------------------------------------------------

def test_explicit_full_frame_equals_no_frame():
    """SUM(val) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
    vs SUM(val) OVER () — should be UNSAT (both are full partition)."""
    catalog = _catalog()
    wf_explicit = WindowFunc(
        func_name="SUM",
        args=[_col("val")],
        order_by=[SortSpec(expr=_col("id"), direction=SortDir.ASC)],
        frame=_full_partition_frame(),
    )
    wf_no_frame = WindowFunc(
        func_name="SUM",
        args=[_col("val")],
        # No frame and no ORDER BY → full partition
    )
    q1 = QueryIR(select=[wf_explicit], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf_no_frame], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


# ---------------------------------------------------------------------------
# W2: Bounded intervals (N PRECEDING ... M FOLLOWING)
# ---------------------------------------------------------------------------

def test_bounded_frame_self_equivalence():
    """SUM(val) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING) — UNSAT."""
    catalog = _catalog()
    wf = WindowFunc(
        func_name="SUM",
        args=[_col("val")],
        order_by=[SortSpec(expr=_col("id"), direction=SortDir.ASC)],
        frame=WindowFrame(
            unit="ROWS",
            start=WindowFrameBound(kind=WindowFrameBoundKind.PRECEDING, offset=1),
            end=WindowFrameBound(kind=WindowFrameBoundKind.FOLLOWING, offset=1),
        ),
    )
    q1 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_bounded_frame_vs_running_sum_sat():
    """1 PRECEDING..1 FOLLOWING vs UNBOUNDED PRECEDING..CURRENT ROW — SAT."""
    catalog = _catalog()
    order = [SortSpec(expr=_col("id"), direction=SortDir.ASC)]
    wf_bounded = WindowFunc(
        func_name="SUM",
        args=[_col("val")],
        order_by=order,
        frame=WindowFrame(
            unit="ROWS",
            start=WindowFrameBound(kind=WindowFrameBoundKind.PRECEDING, offset=1),
            end=WindowFrameBound(kind=WindowFrameBoundKind.FOLLOWING, offset=1),
        ),
    )
    wf_running = WindowFunc(
        func_name="SUM",
        args=[_col("val")],
        order_by=order,
        frame=_running_sum_frame(),
    )
    q1 = QueryIR(select=[wf_bounded], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf_running], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "sat"


# ---------------------------------------------------------------------------
# W2: Partitioned frame
# ---------------------------------------------------------------------------

def test_partitioned_running_sum_self_equiv():
    """SUM(val) OVER (PARTITION BY grp ORDER BY id ROWS UNBOUNDED..CURRENT) — UNSAT."""
    catalog = _catalog()
    wf = WindowFunc(
        func_name="SUM",
        args=[_col("val")],
        partition_by=[_col("grp")],
        order_by=[SortSpec(expr=_col("id"), direction=SortDir.ASC)],
        frame=_running_sum_frame(),
    )
    q1 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


# ---------------------------------------------------------------------------
# W3: FIRST_VALUE / LAST_VALUE
# ---------------------------------------------------------------------------

def test_first_value_self_equivalence():
    """FIRST_VALUE(val) OVER (ORDER BY id) — UNSAT."""
    catalog = _catalog()
    wf = WindowFunc(
        func_name="FIRST_VALUE",
        args=[_col("val")],
        order_by=[SortSpec(expr=_col("id"), direction=SortDir.ASC)],
    )
    q1 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_first_value_vs_last_value_sat():
    """FIRST_VALUE(val) vs LAST_VALUE(val) OVER same spec — SAT when values differ."""
    catalog = _catalog()
    order = [SortSpec(expr=_col("id"), direction=SortDir.ASC)]
    wf_first = WindowFunc(func_name="FIRST_VALUE", args=[_col("val")], order_by=order)
    wf_last = WindowFunc(func_name="LAST_VALUE", args=[_col("val")], order_by=order)
    q1 = QueryIR(select=[wf_first], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf_last], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "sat"


def test_first_value_with_frame():
    """FIRST_VALUE(val) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING)
    — self-equivalence UNSAT."""
    catalog = _catalog()
    wf = WindowFunc(
        func_name="FIRST_VALUE",
        args=[_col("val")],
        order_by=[SortSpec(expr=_col("id"), direction=SortDir.ASC)],
        frame=WindowFrame(
            unit="ROWS",
            start=WindowFrameBound(kind=WindowFrameBoundKind.PRECEDING, offset=1),
            end=WindowFrameBound(kind=WindowFrameBoundKind.FOLLOWING, offset=1),
        ),
    )
    q1 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"


def test_nth_value_self_equivalence():
    """NTH_VALUE(val, 2) OVER (ORDER BY id) — UNSAT."""
    catalog = _catalog()
    wf = WindowFunc(
        func_name="NTH_VALUE",
        args=[_col("val"), Literal(value=2)],
        order_by=[SortSpec(expr=_col("id"), direction=SortDir.ASC)],
    )
    q1 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    q2 = QueryIR(select=[wf], from_table=RelRef(table="t"))
    result = synthesize_witness(q1, q2, catalog, _scope())
    assert result.status == "unsat"
