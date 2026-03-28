"""Unsat core analysis and diagnostic explanation.

When SMT verification returns UNSAT, this module maps the core labels
back to IR nodes and produces human-readable diagnostic explanations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constraints import ConstraintKind, VerificationResult


@dataclass
class Diagnostic:
    """A single diagnostic message from unsat core analysis."""
    severity: str  # "error" or "warning"
    kind: ConstraintKind
    message: str
    ir_node: str | None = None
    suggestion: str | None = None


@dataclass
class DiagnosticReport:
    """Full diagnostic report from unsat core analysis."""
    diagnostics: list[Diagnostic] = field(default_factory=list)
    summary: str = ""

    @property
    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self.diagnostics)


# Suggestion templates per constraint kind
_SUGGESTIONS: dict[ConstraintKind, str] = {
    ConstraintKind.SCHEMA_VALIDITY: (
        "Check that the table/column name is correct and exists in the schema. "
        "Possible misspelling or wrong alias."
    ),
    ConstraintKind.TYPE_SOUNDNESS: (
        "The operation is applied to incompatible types. "
        "Check that aggregates are on numeric columns and comparisons match types."
    ),
    ConstraintKind.JOIN_VALIDITY: (
        "The join predicate does not correspond to a known FK relationship. "
        "Verify the join columns match an actual relationship in the schema."
    ),
    ConstraintKind.GROUPING_LEGALITY: (
        "A non-aggregated expression appears in SELECT but not in GROUP BY. "
        "Either add it to GROUP BY or wrap it in an aggregate."
    ),
    ConstraintKind.GRAIN_CONSTRAINT: (
        "The query grain may produce duplicated/inflated results. "
        "Consider adding DISTINCT or adjusting the join path."
    ),
    ConstraintKind.POLICY: (
        "The query violates a safety/governance policy. "
        "Check for CROSS JOINs, missing LIMITs, or prohibited operations."
    ),
}


def analyze_unsat_core(result: VerificationResult) -> DiagnosticReport:
    """Analyze an UNSAT verification result and produce diagnostics.

    Args:
        result: A VerificationResult with status "unsat".

    Returns:
        A DiagnosticReport with error messages mapped to IR nodes.
    """
    if result.status != "unsat":
        return DiagnosticReport(
            summary="Verification passed (SAT); no diagnostics needed."
        )

    failed = result.failed_constraints()
    diagnostics: list[Diagnostic] = []

    for constraint in failed:
        diagnostics.append(Diagnostic(
            severity="error",
            kind=constraint.kind,
            message=constraint.description,
            ir_node=constraint.ir_node,
            suggestion=_SUGGESTIONS.get(constraint.kind),
        ))

    # Group by kind for summary
    kind_counts: dict[str, int] = {}
    for d in diagnostics:
        kind_counts[d.kind.value] = kind_counts.get(d.kind.value, 0) + 1

    parts = [f"{count} {kind}" for kind, count in sorted(kind_counts.items())]
    summary = f"Verification failed: {', '.join(parts)} constraint(s) violated."

    return DiagnosticReport(diagnostics=diagnostics, summary=summary)


def format_diagnostics(report: DiagnosticReport) -> str:
    """Format a diagnostic report as a human-readable string."""
    if not report.has_errors:
        return report.summary

    lines: list[str] = [report.summary, ""]

    for i, d in enumerate(report.diagnostics, 1):
        lines.append(f"  [{i}] {d.severity.upper()}: {d.message}")
        if d.ir_node:
            lines.append(f"      IR node: {d.ir_node}")
        if d.suggestion:
            lines.append(f"      Suggestion: {d.suggestion}")
        lines.append("")

    return "\n".join(lines)
