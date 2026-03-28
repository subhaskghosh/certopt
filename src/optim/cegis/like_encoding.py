"""Exact LIKE encoding for Z3 string theory.

Provides two routes for encoding SQL LIKE predicates:
  Route A — specialized direct encodings for common patterns
  Route B — general regex compilation for arbitrary patterns

Both routes produce Z3 BoolRef constraints over StringSort values.
"""

from __future__ import annotations

import z3


def classify_like_pattern(pattern: str, escape: str | None = None) -> str:
    """Classify a LIKE pattern into a category for specialized encoding.

    Returns one of:
        "exact"    — no wildcards (e.g., 'abc')
        "prefix"   — trailing % only (e.g., 'abc%')
        "suffix"   — leading % only (e.g., '%xyz')
        "contains" — leading and trailing % only (e.g., '%mid%')
        "general"  — anything else
    """
    tokens = _parse_pattern_chars(pattern, escape)
    wildcards = [(i, t) for i, t in enumerate(tokens) if isinstance(t, str)]

    if not wildcards:
        return "exact"

    # Only % wildcards (no _)
    if all(t == "%" for _, t in wildcards):
        positions = [i for i, _ in wildcards]
        if len(positions) == 1:
            if positions[0] == len(tokens) - 1:
                return "prefix"
            if positions[0] == 0:
                return "suffix"
        if len(positions) == 2 and positions[0] == 0 and positions[1] == len(tokens) - 1:
            return "contains"

    return "general"


def _parse_pattern_chars(pattern: str, escape: str | None) -> list[str | tuple[str, str]]:
    """Parse LIKE pattern into a list of tokens, handling escape sequences.

    Returns:
        - '%' or '_' for wildcards
        - ('lit', c) tuples for literal characters (including escaped wildcards)
    """
    result: list = []
    i = 0
    while i < len(pattern):
        if escape and pattern[i] == escape and i + 1 < len(pattern):
            result.append(("lit", pattern[i + 1]))  # escaped — literal
            i += 2
        elif pattern[i] in ("%", "_"):
            result.append(pattern[i])  # wildcard
            i += 1
        else:
            result.append(("lit", pattern[i]))  # literal
            i += 1
    return result


def encode_like_specialized(
    s: z3.SeqRef,
    pattern: str,
    escape: str | None = None,
) -> z3.BoolRef:
    """Encode LIKE using specialized Z3 string operations for common patterns.

    Only handles: exact, prefix, suffix, contains.
    Raises ValueError for general patterns.
    """
    category = classify_like_pattern(pattern, escape)
    tokens = _parse_pattern_chars(pattern, escape)

    def _extract_literals(toks):
        return "".join(t[1] for t in toks if isinstance(t, tuple))

    if category == "exact":
        return s == z3.StringVal(_extract_literals(tokens))

    if category == "prefix":
        # Remove trailing %
        return z3.PrefixOf(z3.StringVal(_extract_literals(tokens[:-1])), s)

    if category == "suffix":
        # Remove leading %
        return z3.SuffixOf(z3.StringVal(_extract_literals(tokens[1:])), s)

    if category == "contains":
        # Remove leading and trailing %
        return z3.Contains(s, z3.StringVal(_extract_literals(tokens[1:-1])))

    raise ValueError(f"Pattern '{pattern}' is not a specialized category: {category}")


def like_to_regex(pattern: str, escape: str | None = None) -> z3.ReRef:
    """Compile SQL LIKE pattern to Z3 regular expression.

    Mapping:
        literal char c → Re(StringVal(c))
        %              → Star(allchar)
        _              → allchar
        escaped % or _ → literal
    """
    anychar = z3.Range(z3.StringVal("\x00"), z3.StringVal("\xff"))
    allchar = z3.Star(anychar)
    singlechar = anychar

    parts: list[z3.ReRef] = []
    i = 0
    while i < len(pattern):
        if escape and pattern[i] == escape and i + 1 < len(pattern):
            parts.append(z3.Re(z3.StringVal(pattern[i + 1])))
            i += 2
        elif pattern[i] == "%":
            parts.append(allchar)
            i += 1
        elif pattern[i] == "_":
            parts.append(singlechar)
            i += 1
        else:
            parts.append(z3.Re(z3.StringVal(pattern[i])))
            i += 1

    if not parts:
        return z3.Re(z3.StringVal(""))
    if len(parts) == 1:
        return parts[0]
    return z3.Concat(*parts)


def encode_like(
    s: z3.SeqRef,
    pattern: str,
    escape: str | None = None,
) -> z3.BoolRef:
    """Encode SQL LIKE predicate using the best available strategy.

    Uses specialized encoding for common patterns, falls back to regex
    compilation for general patterns.
    """
    category = classify_like_pattern(pattern, escape)
    if category != "general":
        return encode_like_specialized(s, pattern, escape)
    regex = like_to_regex(pattern, escape)
    return z3.InRe(s, regex)
