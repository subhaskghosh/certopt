"""Certificate generation and replay verification.

A certificate is a machine-checkable proof artifact that records:
  - The bounded scope Σ
  - The normalized, bound IR
  - The set of tracked constraints and their labels
  - The solver outcome (SAT + stats)
  - The rendered SQL

The replay verifier re-encodes constraints and re-runs the solver to
confirm the certificate is valid.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..ir.normalization import normalize
from ..ir.render_sql import render
from ..ir.types import QueryIR
from ..schema.catalog import Catalog
from .constraints import VerificationResult, smt_verify
from .encode_z3 import BoundedScope


@dataclass
class Certificate:
    """A replayable proof artifact for a certified query.

    FIX.28a: Extended to support equivalence proofs.  When proof_kind
    is "equivalence", the certificate records BOTH the original and
    rewrite IRs together with the UNSAT result from witness synthesis,
    proving Q_original ≡_Σ Q_rewrite under bounded scope Σ.
    """
    # Scope
    scope: dict[str, Any]
    # IR (normalized, serialized) — rewrite query
    ir_json: dict[str, Any]
    # SQL — rewrite query
    sql: str
    dialect: str
    # Structural constraints (rewrite query)
    constraints: list[dict[str, Any]]
    # Structural solver (rewrite query)
    solver_status: str
    solver_time_ms: float
    solver_stats: dict[str, Any]
    # Hashes for integrity (rewrite query)
    ir_hash: str
    sql_hash: str
    catalog_hash: str = ""
    # Timestamp
    timestamp: str = ""
    # Version
    version: str = "1.1"
    # FIX.28a: Proof kind — "structural" (legacy) or "equivalence"
    proof_kind: str = "structural"
    # FIX.28a: Original query (for equivalence proofs)
    original_ir_json: Optional[dict[str, Any]] = None
    original_sql: str = ""
    original_ir_hash: str = ""
    original_sql_hash: str = ""
    # FIX.28a: Equivalence check result
    equivalence_status: str = ""  # "unsat" for valid equivalence proof
    equivalence_solver_time_ms: float = 0.0
    # FIX.28b: Proof bound metadata
    equivalence_proven_k: Optional[int] = None
    equivalence_complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = {
            "version": self.version,
            "timestamp": self.timestamp,
            "proof_kind": self.proof_kind,
            "scope": self.scope,
            "ir": self.ir_json,
            "ir_hash": self.ir_hash,
            "sql": self.sql,
            "sql_hash": self.sql_hash,
            "catalog_hash": self.catalog_hash,
            "dialect": self.dialect,
            "constraints": self.constraints,
            "solver_status": self.solver_status,
            "solver_time_ms": self.solver_time_ms,
            "solver_stats": self.solver_stats,
        }
        if self.proof_kind == "equivalence" and self.original_ir_json is not None:
            d["original_ir"] = self.original_ir_json
            d["original_sql"] = self.original_sql
            d["original_ir_hash"] = self.original_ir_hash
            d["original_sql_hash"] = self.original_sql_hash
            d["equivalence_status"] = self.equivalence_status
            d["equivalence_solver_time_ms"] = self.equivalence_solver_time_ms
            if self.equivalence_proven_k is not None:
                d["equivalence_proven_k"] = self.equivalence_proven_k
            d["equivalence_complete"] = self.equivalence_complete
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def save(self, path: Path, catalog: Optional[Catalog] = None) -> None:
        """Save certificate to a directory.

        Args:
            path: Directory to save into.
            catalog: If provided, saves catalog snapshot for standalone replay.
        """
        path.mkdir(parents=True, exist_ok=True)
        (path / "certificate.json").write_text(self.to_json())
        (path / "query.sql").write_text(self.sql)
        if catalog is not None:
            catalog_data = {
                "tables": {
                    k: v.model_dump(mode="json")
                    for k, v in catalog.tables.items()
                },
                "foreign_keys": [
                    fk.model_dump(mode="json") for fk in catalog.foreign_keys
                ],
            }
            (path / "catalog.json").write_text(
                json.dumps(catalog_data, indent=2, default=str)
            )

    @classmethod
    def load(cls, path: Path) -> Certificate:
        """Load a certificate from a directory."""
        data = json.loads((path / "certificate.json").read_text())
        return cls(
            scope=data["scope"],
            ir_json=data["ir"],
            sql=data["sql"],
            dialect=data.get("dialect", "sqlite"),
            constraints=data["constraints"],
            solver_status=data["solver_status"],
            solver_time_ms=data.get("solver_time_ms", 0),
            solver_stats=data.get("solver_stats", {}),
            ir_hash=data["ir_hash"],
            sql_hash=data["sql_hash"],
            catalog_hash=data.get("catalog_hash", ""),
            timestamp=data.get("timestamp", ""),
            version=data.get("version", "1.0"),
            proof_kind=data.get("proof_kind", "structural"),
            original_ir_json=data.get("original_ir"),
            original_sql=data.get("original_sql", ""),
            original_ir_hash=data.get("original_ir_hash", ""),
            original_sql_hash=data.get("original_sql_hash", ""),
            equivalence_status=data.get("equivalence_status", ""),
            equivalence_solver_time_ms=data.get("equivalence_solver_time_ms", 0.0),
            equivalence_proven_k=data.get("equivalence_proven_k"),
            equivalence_complete=data.get("equivalence_complete", True),
        )

    @staticmethod
    def load_catalog(path: Path) -> Optional[Catalog]:
        """Load a catalog snapshot saved alongside a certificate."""
        catalog_path = path / "catalog.json"
        if not catalog_path.exists():
            return None
        data = json.loads(catalog_path.read_text())
        from ..schema.catalog import ForeignKey, TableInfo
        tables = {}
        for name, tbl_data in data.get("tables", {}).items():
            tables[name] = TableInfo.model_validate(tbl_data)
        fks = [
            ForeignKey.model_validate(fk_data)
            for fk_data in data.get("foreign_keys", [])
        ]
        return Catalog(tables=tables, foreign_keys=fks)


def _compute_catalog_hash(catalog: Catalog) -> str:
    """Compute a stable hash of the catalog schema."""
    tables_dict = {
        k: v.model_dump(mode="json") for k, v in sorted(catalog.tables.items())
    }
    fk_list = [fk.model_dump(mode="json") for fk in catalog.foreign_keys]
    payload = json.dumps(
        {"tables": tables_dict, "foreign_keys": fk_list},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def create_certificate(
    ir: QueryIR,
    catalog: Catalog,
    verification: VerificationResult,
    scope: Optional[BoundedScope] = None,
    dialect: str = "sqlite",
    *,
    original_ir: Optional[QueryIR] = None,
    equivalence_status: str = "",
    equivalence_solver_time_ms: float = 0.0,
    equivalence_proven_k: Optional[int] = None,
    equivalence_complete: bool = True,
) -> Certificate:
    """Create a certificate from a verified IR.

    Args:
        ir: The normalized, bound rewrite IR.
        catalog: The schema catalog.
        verification: The structural verification result (must be SAT).
        scope: The bounded semantics scope.
        dialect: SQL dialect for rendering.
        original_ir: The original query IR (for equivalence proofs).
        equivalence_status: Result of witness synthesis ("unsat" for valid).
        equivalence_solver_time_ms: Time taken for equivalence check.

    Returns:
        A Certificate object.

    Raises:
        ValueError: If verification status is not SAT.
    """
    if not verification.certified:
        raise ValueError(
            f"Cannot certify: verification status is '{verification.status}', "
            f"not 'sat'"
        )

    if scope is None:
        scope = BoundedScope()

    normed = normalize(ir)
    sql = render(normed, dialect=dialect)

    ir_json = normed.model_dump(mode="json")
    ir_str = json.dumps(ir_json, sort_keys=True, default=str)
    ir_hash = hashlib.sha256(ir_str.encode()).hexdigest()[:16]
    sql_hash = hashlib.sha256(sql.encode()).hexdigest()[:16]
    catalog_hash = _compute_catalog_hash(catalog)

    constraint_dicts = []
    for c in verification.constraints:
        constraint_dicts.append({
            "label": c.label,
            "kind": c.kind.value,
            "description": c.description,
            "ir_node": c.ir_node,
            "satisfied": c.satisfied,
        })

    scope_dict = {
        "k_rows": scope.k_rows,
        "int_bounds": list(scope.int_bounds),
        "string_symbols": scope.string_symbols,
        "date_values": scope.date_values,
        "null_semantics": scope.null_semantics,
        "solver_timeout_ms": scope.solver_timeout_ms,
    }

    # FIX.28a: Build equivalence proof fields if original_ir provided
    proof_kind = "structural"
    orig_ir_json = None
    orig_sql = ""
    orig_ir_hash = ""
    orig_sql_hash = ""
    if original_ir is not None and equivalence_status == "unsat":
        proof_kind = "equivalence"
        orig_normed = normalize(original_ir)
        orig_sql = render(orig_normed, dialect=dialect)
        orig_ir_json = orig_normed.model_dump(mode="json")
        orig_ir_str = json.dumps(orig_ir_json, sort_keys=True, default=str)
        orig_ir_hash = hashlib.sha256(orig_ir_str.encode()).hexdigest()[:16]
        orig_sql_hash = hashlib.sha256(orig_sql.encode()).hexdigest()[:16]

    return Certificate(
        scope=scope_dict,
        ir_json=ir_json,
        sql=sql,
        dialect=dialect,
        constraints=constraint_dicts,
        solver_status=verification.status,
        solver_time_ms=verification.solver_time_ms,
        solver_stats=verification.solver_stats,
        ir_hash=ir_hash,
        sql_hash=sql_hash,
        catalog_hash=catalog_hash,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        proof_kind=proof_kind,
        original_ir_json=orig_ir_json,
        original_sql=orig_sql,
        original_ir_hash=orig_ir_hash,
        original_sql_hash=orig_sql_hash,
        equivalence_status=equivalence_status,
        equivalence_solver_time_ms=equivalence_solver_time_ms,
        equivalence_proven_k=equivalence_proven_k,
        equivalence_complete=equivalence_complete,
    )


# ---------------------------------------------------------------------------
# Replay verification
# ---------------------------------------------------------------------------

@dataclass
class ReplayResult:
    """Result of replaying a certificate."""
    valid: bool
    original_status: str
    replay_status: str
    ir_hash_match: bool
    sql_hash_match: bool
    catalog_hash_match: bool
    constraint_count_match: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class CompositionalCertificate:
    """Certificate for a compositionally verified rewrite (Direction D.4).

    Records the local equivalence proof together with the context
    preservation check, enabling independent replay of each part.
    """
    local_certificate: Certificate
    context_class: str
    context_check: dict[str, bool]
    region_context_path: list[str]
    region_input_tables: list[str]
    composed_scope: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "compositional",
            "local_certificate": self.local_certificate.to_dict(),
            "context_class": self.context_class,
            "context_check": self.context_check,
            "region_context_path": self.region_context_path,
            "region_input_tables": self.region_input_tables,
            "composed_scope": self.composed_scope,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


def _reconstruct_scope(cert: Certificate) -> BoundedScope:
    """Reconstruct a BoundedScope from a certificate's scope dict."""
    raw_bounds = cert.scope.get("int_bounds", [-10, 10])
    try:
        int_bounds = (raw_bounds[0], raw_bounds[1])
    except (IndexError, TypeError):
        int_bounds = (-10, 10)
    return BoundedScope(
        k_rows=cert.scope.get("k_rows", 3),
        int_bounds=int_bounds,
        string_symbols=cert.scope.get("string_symbols", []),
        date_values=cert.scope.get("date_values", []),
        null_semantics=cert.scope.get("null_semantics", True),
        solver_timeout_ms=cert.scope.get("solver_timeout_ms", 30000),
    )


def replay_certificate(
    cert: Certificate,
    catalog: Catalog,
) -> ReplayResult:
    """Replay a certificate to verify it's still valid.

    For structural certificates: re-encodes constraints and re-runs the
    structural solver, checking hashes and solver agreement.

    FIX.28a: For equivalence certificates: additionally reconstructs the
    original IR and re-runs witness synthesis to confirm UNSAT, proving
    the queries are still equivalent under the recorded scope Σ.
    """
    errors: list[str] = []

    # Reconstruct rewrite IR from certificate
    try:
        ir = QueryIR.model_validate(cert.ir_json)
    except Exception as e:
        return ReplayResult(
            valid=False,
            original_status=cert.solver_status,
            replay_status="error",
            ir_hash_match=False,
            sql_hash_match=False,
            catalog_hash_match=False,
            constraint_count_match=False,
            errors=[f"Failed to reconstruct IR: {e}"],
        )

    # Verify rewrite hashes
    normed = normalize(ir)
    ir_json = normed.model_dump(mode="json")
    ir_str = json.dumps(ir_json, sort_keys=True, default=str)
    ir_hash = hashlib.sha256(ir_str.encode()).hexdigest()[:16]
    ir_hash_match = ir_hash == cert.ir_hash

    sql = render(normed, dialect=cert.dialect)
    sql_hash = hashlib.sha256(sql.encode()).hexdigest()[:16]
    sql_hash_match = sql_hash == cert.sql_hash

    catalog_hash = _compute_catalog_hash(catalog)
    catalog_hash_match = (
        cert.catalog_hash == "" or catalog_hash == cert.catalog_hash
    )

    if not ir_hash_match:
        errors.append(f"IR hash mismatch: {ir_hash} vs {cert.ir_hash}")
    if not sql_hash_match:
        errors.append(f"SQL hash mismatch: {sql_hash} vs {cert.sql_hash}")
    if not catalog_hash_match:
        errors.append(f"Catalog hash mismatch: {catalog_hash} vs {cert.catalog_hash}")

    scope = _reconstruct_scope(cert)

    # Re-run structural SMT verification
    replay_result = smt_verify(normed, catalog, scope)
    constraint_count_match = len(replay_result.constraints) == len(cert.constraints)

    if not constraint_count_match:
        errors.append(
            f"Constraint count mismatch: {len(replay_result.constraints)} "
            f"vs {len(cert.constraints)}"
        )

    if replay_result.status != cert.solver_status:
        errors.append(
            f"Solver status mismatch: replay={replay_result.status}, "
            f"original={cert.solver_status}"
        )

    # When structural verify was skipped during cert creation (equivalence-only
    # certificates), don't require structural replay to match.
    structural_skipped = cert.solver_status == "skipped"
    valid = (
        ir_hash_match
        and sql_hash_match
        and catalog_hash_match
        and (structural_skipped or replay_result.status == cert.solver_status)
        and (structural_skipped or constraint_count_match)
    )

    # FIX.28a: For equivalence proofs, also replay witness synthesis
    if cert.proof_kind == "equivalence" and cert.original_ir_json is not None:
        equiv_valid, equiv_errors = _replay_equivalence(
            cert, normed, catalog, scope,
        )
        valid = valid and equiv_valid
        errors.extend(equiv_errors)

    return ReplayResult(
        valid=valid,
        original_status=cert.solver_status,
        replay_status=replay_result.status,
        ir_hash_match=ir_hash_match,
        sql_hash_match=sql_hash_match,
        catalog_hash_match=catalog_hash_match,
        constraint_count_match=constraint_count_match,
        errors=errors,
    )


def _replay_equivalence(
    cert: Certificate,
    rewrite_normed: QueryIR,
    catalog: Catalog,
    scope: BoundedScope,
) -> tuple[bool, list[str]]:
    """Replay the equivalence proof portion of a certificate.

    Reconstructs the original IR, verifies its hashes, and re-runs
    witness synthesis to confirm UNSAT (bounded equivalence).

    Returns:
        (valid, errors) tuple.
    """
    from ..cegis.witness_synthesis import synthesize_witness_adaptive

    errors: list[str] = []

    # Reconstruct original IR
    try:
        original_ir = QueryIR.model_validate(cert.original_ir_json)
    except Exception as e:
        return False, [f"Failed to reconstruct original IR: {e}"]

    # Verify original IR hashes
    orig_normed = normalize(original_ir)
    orig_ir_json = orig_normed.model_dump(mode="json")
    orig_ir_str = json.dumps(orig_ir_json, sort_keys=True, default=str)
    orig_ir_hash = hashlib.sha256(orig_ir_str.encode()).hexdigest()[:16]

    if orig_ir_hash != cert.original_ir_hash:
        errors.append(
            f"Original IR hash mismatch: {orig_ir_hash} vs {cert.original_ir_hash}"
        )

    orig_sql = render(orig_normed, dialect=cert.dialect)
    orig_sql_hash = hashlib.sha256(orig_sql.encode()).hexdigest()[:16]

    if orig_sql_hash != cert.original_sql_hash:
        errors.append(
            f"Original SQL hash mismatch: {orig_sql_hash} vs {cert.original_sql_hash}"
        )

    # Re-run witness synthesis: must be UNSAT for valid equivalence proof.
    # Use the adaptive pipeline with witness validation to match the
    # evaluation harness (catches spurious SAT from conservative encoding).
    orig_sql_text = render(orig_normed, dialect=cert.dialect)
    rewrite_sql_text = render(rewrite_normed, dialect=cert.dialect)
    witness_result = synthesize_witness_adaptive(
        orig_normed, rewrite_normed, catalog, scope,
        validate_witnesses=True,
        original_sql=(orig_sql_text, rewrite_sql_text),
        at_most_k=True,
        normalize_column_order=False,
    )

    if witness_result.status != cert.equivalence_status:
        errors.append(
            f"Equivalence status mismatch: replay={witness_result.status}, "
            f"original={cert.equivalence_status}"
        )

    valid = (
        orig_ir_hash == cert.original_ir_hash
        and orig_sql_hash == cert.original_sql_hash
        and witness_result.status == cert.equivalence_status
    )

    return valid, errors
