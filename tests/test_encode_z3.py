"""Tests for Z3 encoding primitives and 3-valued logic."""

import z3

from optim.ir.types import SemType
from optim.verify.encode_z3 import (
    BoundedScope,
    make_sorts,
    make_nullable_var,
    domain_constraints,
    sql_eq,
    sql_neq,
    sql_and,
    sql_or,
    sql_not,
    sql_is_null_check,
    truth_is_true,
    sort_for_type,
)


def _eval(expr, sorts):
    """Evaluate a Z3 expression to a concrete value."""
    s = z3.Solver()
    result = z3.Bool("__result")
    s.add(result == expr)
    assert s.check() == z3.sat
    m = s.model()
    return m.eval(expr)


def test_make_sorts():
    scope = BoundedScope()
    sorts = make_sorts(scope)
    assert sorts.truth_TRUE is not None
    assert sorts.truth_FALSE is not None
    assert sorts.truth_UNKNOWN is not None
    # They should be distinct
    s = z3.Solver()
    s.add(sorts.truth_TRUE == sorts.truth_FALSE)
    assert s.check() == z3.unsat


def test_nullable_var():
    scope = BoundedScope()
    sorts = make_sorts(scope)
    v = make_nullable_var("test", SemType.INT, sorts, nullable=True)
    assert v.is_null is not None
    assert v.val is not None

    v_nn = make_nullable_var("test2", SemType.INT, sorts, nullable=False)
    # is_null should be BoolVal(False) for non-nullable
    s = z3.Solver()
    s.add(v_nn.is_null)
    assert s.check() == z3.unsat


def test_domain_constraints_int():
    scope = BoundedScope(int_bounds=(-5, 5))
    sorts = make_sorts(scope)
    v = make_nullable_var("x", SemType.INT, sorts)
    constraints = domain_constraints(v, scope)

    s = z3.Solver()
    for c in constraints:
        s.add(c)
    # Not null and val = 3 should be SAT
    s.add(z3.Not(v.is_null))
    s.add(v.val == 3)
    assert s.check() == z3.sat

    # Not null and val = 100 should be UNSAT
    s2 = z3.Solver()
    for c in constraints:
        s2.add(c)
    s2.add(z3.Not(v.is_null))
    s2.add(v.val == 100)
    assert s2.check() == z3.unsat


def test_sql_eq_both_non_null():
    """Non-null values: eq returns TRUE or FALSE."""
    scope = BoundedScope()
    sorts = make_sorts(scope)
    a = make_nullable_var("a", SemType.INT, sorts, nullable=False)
    b = make_nullable_var("b", SemType.INT, sorts, nullable=False)

    result = sql_eq(a, b, sorts)

    s = z3.Solver()
    s.add(a.val == 5)
    s.add(b.val == 5)
    s.check()
    m = s.model()
    assert m.eval(result) == sorts.truth_TRUE

    s2 = z3.Solver()
    s2.add(a.val == 5)
    s2.add(b.val == 3)
    s2.check()
    m2 = s2.model()
    assert m2.eval(result) == sorts.truth_FALSE


def test_sql_eq_null_returns_unknown():
    """NULL = anything → UNKNOWN."""
    scope = BoundedScope()
    sorts = make_sorts(scope)
    a = make_nullable_var("a", SemType.INT, sorts)
    b = make_nullable_var("b", SemType.INT, sorts, nullable=False)

    result = sql_eq(a, b, sorts)

    s = z3.Solver()
    s.add(a.is_null)
    s.add(b.val == 5)
    s.check()
    m = s.model()
    assert m.eval(result) == sorts.truth_UNKNOWN


def test_sql_and_truth_table():
    """Test 3VL AND truth table."""
    scope = BoundedScope()
    sorts = make_sorts(scope)

    T, F, U = sorts.truth_TRUE, sorts.truth_FALSE, sorts.truth_UNKNOWN

    # TRUE AND TRUE = TRUE
    s = z3.Solver()
    r = sql_and(T, T, sorts)
    s.check()
    assert s.model().eval(r) == T

    # FALSE AND anything = FALSE
    s2 = z3.Solver()
    r2 = sql_and(F, U, sorts)
    s2.check()
    assert s2.model().eval(r2) == F

    # TRUE AND UNKNOWN = UNKNOWN
    s3 = z3.Solver()
    r3 = sql_and(T, U, sorts)
    s3.check()
    assert s3.model().eval(r3) == U


def test_sql_or_truth_table():
    """Test 3VL OR truth table."""
    scope = BoundedScope()
    sorts = make_sorts(scope)

    T, F, U = sorts.truth_TRUE, sorts.truth_FALSE, sorts.truth_UNKNOWN

    # TRUE OR anything = TRUE
    s = z3.Solver()
    assert s.model().eval(sql_or(T, F, sorts)) == T if s.check() == z3.sat else None
    s2 = z3.Solver()
    s2.check()
    assert s2.model().eval(sql_or(T, U, sorts)) == T

    # FALSE OR FALSE = FALSE
    s3 = z3.Solver()
    s3.check()
    assert s3.model().eval(sql_or(F, F, sorts)) == F

    # FALSE OR UNKNOWN = UNKNOWN
    s4 = z3.Solver()
    s4.check()
    assert s4.model().eval(sql_or(F, U, sorts)) == U


def test_sql_not_truth_table():
    """Test 3VL NOT."""
    scope = BoundedScope()
    sorts = make_sorts(scope)

    T, F, U = sorts.truth_TRUE, sorts.truth_FALSE, sorts.truth_UNKNOWN

    s = z3.Solver()
    s.check()
    m = s.model()
    assert m.eval(sql_not(T, sorts)) == F
    assert m.eval(sql_not(F, sorts)) == T
    assert m.eval(sql_not(U, sorts)) == U


def test_truth_is_true():
    """truth_is_true converts 3VL to Bool."""
    scope = BoundedScope()
    sorts = make_sorts(scope)

    s = z3.Solver()
    s.check()
    m = s.model()

    assert m.eval(truth_is_true(sorts.truth_TRUE, sorts)) is True or \
           z3.is_true(m.eval(truth_is_true(sorts.truth_TRUE, sorts)))
    assert z3.is_false(m.eval(truth_is_true(sorts.truth_FALSE, sorts)))
    assert z3.is_false(m.eval(truth_is_true(sorts.truth_UNKNOWN, sorts)))


def test_sort_for_type():
    scope = BoundedScope()
    sorts = make_sorts(scope)
    assert sort_for_type(SemType.INT, sorts) == sorts.int_sort
    assert sort_for_type(SemType.STRING, sorts) == sorts.string_sort
    assert sort_for_type(SemType.BOOL, sorts) == sorts.bool_sort
    assert sort_for_type(SemType.DATE, sorts) == sorts.date_sort
