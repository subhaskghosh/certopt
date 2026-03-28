"""Hash-consed Z3 expression cache for witness synthesis.

Memoizes _eval_value() and _eval_predicate_3vl() results by
(expression identity, binding signature) to avoid constructing
identical Z3 subexpressions across combos.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExprCache:
    """Cache for Z3 expression construction during witness synthesis.

    Keys are (id(expr_node), binding_key) tuples. The binding_key is
    derived from the binding dict to identify which symbolic rows are
    bound — two combos that bind the same aliases to the same symbolic
    rows will share cached expressions.
    """
    _value_cache: dict[tuple, object] = field(default_factory=dict)
    _pred_cache: dict[tuple, object] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0
    pred_hits: int = 0
    pred_misses: int = 0

    @staticmethod
    def binding_key(binding: dict) -> tuple:
        """Extract a hashable key from a binding dict.

        Uses id(SymbolicRow) as the row identity — two combos that
        reference the same SymbolicRow object for the same alias
        will produce the same key.
        """
        return tuple(sorted((k, id(v)) for k, v in binding.items()))

    def get_value(self, expr_id: int, bkey: tuple):
        """Look up a cached NullableVal. Returns None on miss."""
        result = self._value_cache.get((expr_id, bkey))
        if result is not None:
            self.hits += 1
        return result

    def put_value(self, expr_id: int, bkey: tuple, val):
        """Store a NullableVal in the cache."""
        self._value_cache[(expr_id, bkey)] = val
        self.misses += 1

    def get_pred(self, expr_id: int, bkey: tuple):
        """Look up a cached TriBool. Returns None on miss."""
        result = self._pred_cache.get((expr_id, bkey))
        if result is not None:
            self.pred_hits += 1
        return result

    def put_pred(self, expr_id: int, bkey: tuple, val):
        """Store a TriBool in the cache."""
        self._pred_cache[(expr_id, bkey)] = val
        self.pred_misses += 1

    def stats(self) -> dict:
        """Return cache statistics."""
        return {
            "value_hits": self.hits,
            "value_misses": self.misses,
            "value_hit_rate": self.hits / max(1, self.hits + self.misses),
            "pred_hits": self.pred_hits,
            "pred_misses": self.pred_misses,
            "pred_hit_rate": self.pred_hits / max(1, self.pred_hits + self.pred_misses),
            "total_entries": len(self._value_cache) + len(self._pred_cache),
        }

    def clear(self):
        """Reset the cache."""
        self._value_cache.clear()
        self._pred_cache.clear()
        self.hits = 0
        self.misses = 0
        self.pred_hits = 0
        self.pred_misses = 0
