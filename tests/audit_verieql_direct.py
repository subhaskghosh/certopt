"""Audit: Run VeriEQL directly on HAVING COUNT queries to confirm exactly-N behavior.

Mocks ujson (unavailable on this platform) to allow importing VeriEQL.
"""
import json
import sys
import types

# Mock ujson with standard json
ujson_mock = types.ModuleType('ujson')
ujson_mock.loads = json.loads
ujson_mock.dumps = json.dumps
sys.modules['ujson'] = ujson_mock

sys.path.insert(0, 'data/VeriEQL')

from environment import Environment


def run_test(N, sql1, sql2, label):
    """Run VeriEQL on a query pair at bound N."""
    try:
        env = Environment(semantics='bag', show_counterexample=True)
        env.create_database(
            attributes={'CID': 'INT'},
            bound_size=N,
            name='ORDERS',
        )
        env.save_checkpoints()

        result = env.analyze(sql1, sql2)
        if result is True:
            status = "EQUIVALENT (UNSAT)"
        elif result is False:
            status = "NON-EQUIVALENT (SAT)"
        else:
            status = f"OTHER ({result})"

        print(f"  {label} N={N}: {status}")
        if result is False and env.counterexample:
            # Show counterexample
            for line in env.counterexample.strip().split('\n'):
                print(f"    {line}")
        return result
    except Exception as e:
        print(f"  {label} N={N}: ERROR — {e}")
        import traceback
        traceback.print_exc()
        return None


def test_having_count():
    """Core non-monotonicity test: HAVING COUNT(*)>=3 vs >3."""
    print("=" * 70)
    print("TEST A: HAVING COUNT(*)>=3 vs COUNT(*)>3")
    print("  If exactly-N: UNSAT at N=2 (vacuous), SAT at N=3")
    print("  If at-most-N: SAT at N≥3 (solver can choose 3 rows)")
    print("=" * 70)

    sql1 = "SELECT CID FROM ORDERS GROUP BY CID HAVING COUNT(*) >= 3"
    sql2 = "SELECT CID FROM ORDERS GROUP BY CID HAVING COUNT(*) > 3"

    results = {}
    for N in [2, 3, 4]:
        results[N] = run_test(N, sql1, sql2, "HAVING>=3 vs >3")
    print()

    # Analysis
    if results[2] is True and results[3] is False:
        print("  ✓ CONFIRMED: VeriEQL uses EXACTLY-N for base tables")
        print("    N=2 → UNSAT (vacuous: COUNT never reaches 3)")
        print("    N=3 → SAT (COUNT=3: Q1 returns group, Q2 doesn't)")
        print("    UNSAT at N=2 does NOT transfer to N=3")
    elif results[2] is True and results[3] is True:
        print("  ? Unexpected: UNSAT at both N=2 and N=3")
        print("    This would suggest VeriEQL's GROUP BY encoding")
        print("    doesn't support this case, not an at-most-N issue")
    print()
    return results


def test_select_star():
    """Simple sanity check: SELECT * FROM T ≡ SELECT * FROM T."""
    print("=" * 70)
    print("TEST B: Sanity — SELECT CID FROM ORDERS ≡ SELECT CID FROM ORDERS")
    print("=" * 70)
    sql1 = "SELECT CID FROM ORDERS"
    sql2 = "SELECT CID FROM ORDERS"
    for N in [2, 3]:
        run_test(N, sql1, sql2, "Identity")
    print()


def test_simple_neq():
    """Simple NEQ: SELECT CID FROM ORDERS vs SELECT CID FROM ORDERS WHERE CID > 5."""
    print("=" * 70)
    print("TEST C: SELECT CID vs SELECT CID WHERE CID > 5")
    print("=" * 70)
    sql1 = "SELECT CID FROM ORDERS"
    sql2 = "SELECT CID FROM ORDERS WHERE CID > 5"
    for N in [1, 2]:
        run_test(N, sql1, sql2, "With/without WHERE")
    print()


def test_dbms_facts_inspection():
    """Directly inspect DBMS_facts to see Not(DELETED) constraints."""
    print("=" * 70)
    print("TEST D: Direct inspection of DBMS_facts")
    print("=" * 70)

    env = Environment(semantics='bag')
    env.create_database(
        attributes={'CID': 'INT', 'AMT': 'INT'},
        bound_size=3,
        name='ORDERS',
    )

    print(f"  Number of DBMS_facts: {len(env.DBMS_facts)}")
    print(f"  DBMS_facts contents:")
    for i, fact in enumerate(env.DBMS_facts):
        fact_str = str(fact)
        # Highlight DELETED constraints
        if 'DELETED' in fact_str:
            print(f"    [{i}] *** {fact_str}")
        else:
            print(f"    [{i}] {fact_str}")

    # Count DELETED constraints
    deleted_constraints = [f for f in env.DBMS_facts if 'DELETED' in str(f)]
    print(f"\n  DELETED-related constraints: {len(deleted_constraints)}")
    for dc in deleted_constraints:
        print(f"    {dc}")
    print()

    # Verify all are Not(DELETED(...))
    all_not_deleted = all('Not(DELETED' in str(dc) for dc in deleted_constraints)
    print(f"  All are Not(DELETED(...)): {all_not_deleted}")
    if all_not_deleted:
        print("  ✓ CONFIRMED: Every base tuple has Not(DELETED(t)) constraint")
        print("    → Base tuples are forced present (exactly-N)")
    print()


if __name__ == '__main__':
    print()
    print("=" * 70)
    print("VERIEQL DIRECT AUDIT: Running VeriEQL's own code")
    print("=" * 70)
    print()

    test_dbms_facts_inspection()
    test_select_star()
    test_simple_neq()
    test_having_count()

    print("=" * 70)
    print("AUDIT COMPLETE")
    print("=" * 70)
