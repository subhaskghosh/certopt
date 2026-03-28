"""CEGIS optimization loop: generate → verify → rank → select.

The main entry point is `optimize()`, which:
  1. Parses input SQL → QueryIR
  2. Generates rewrite candidates via RewriteGenerator
  3. Verifies each candidate against the original via witness synthesis
  4. Ranks verified-equivalent candidates by cost
  5. Returns the cheapest equivalent rewrite with its certificate
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from ..cegis.equivalence import Candidate
from ..cegis.witness_export import validate_witness
from ..cegis.witness_synthesis import WitnessResult, synthesize_witness
from ..config import OptimizerConfig
from ..cost.estimator import CostEstimate, CostEstimator, SyntacticCostEstimator
from ..ir.render_sql import render
from ..ir.types import QueryIR
from ..parser.sql_to_ir import sql_to_ir
from ..rewrite.families import RewriteFamily, classify_rewrites, prune_families
from ..rewrite.generator import RewriteConfig, RewriteGenerator
from ..schema.catalog import Catalog
from ..verify.certificate import Certificate, create_certificate
from ..verify.constraints import VerificationResult, structural_verify
from ..verify.encode_z3 import BoundedScope
from .result import OptimizationResult, RejectedRewrite

logger = logging.getLogger(__name__)


def optimize(
    sql: str,
    catalog: Catalog,
    *,
    scope: Optional[BoundedScope] = None,
    rewrite_config: Optional[RewriteConfig] = None,
    cost_estimator: Optional[CostEstimator] = None,
    dialect: str = "sqlite",
    validate_witnesses: bool = True,
) -> OptimizationResult:
    """Run the CEGIS optimization loop.

    Args:
        sql: Input SQL query to optimize.
        catalog: Schema metadata.
        scope: Bounded scope for witness synthesis (default: k=3).
        rewrite_config: Which rules to enable, candidate caps.
        cost_estimator: Cost model for ranking (default: SyntacticCostEstimator).
        dialect: SQL dialect for rendering.
        validate_witnesses: Whether to validate SAT witnesses in SQLite.

    Returns:
        OptimizationResult with the optimized query, certificate, and diagnostics.
    """
    t_start = time.monotonic()

    if scope is None:
        scope = BoundedScope()
    if cost_estimator is None:
        cost_estimator = SyntacticCostEstimator()

    # ---------------------------------------------------------------
    # Step 1: Parse input SQL → QueryIR
    # ---------------------------------------------------------------
    original_ir, parse_err = sql_to_ir(sql, dialect=dialect)
    if original_ir is None:
        raise ValueError(f"Failed to parse input SQL: {parse_err}")

    original_sql = render(original_ir, dialect=dialect)
    cost_original = cost_estimator.estimate(original_ir, catalog)

    # ---------------------------------------------------------------
    # Step 2: Generate rewrite candidates
    # ---------------------------------------------------------------
    generator = RewriteGenerator(catalog, rewrite_config)
    candidates = generator.generate(original_ir)

    if not candidates:
        logger.info("No rewrite candidates generated")
        return _no_improvement(original_sql, original_ir, cost_original, t_start)

    # ---------------------------------------------------------------
    # Step 3: Classify into families for pruning
    # ---------------------------------------------------------------
    families = classify_rewrites(candidates)

    # ---------------------------------------------------------------
    # Step 4: Verify each candidate against the original
    # ---------------------------------------------------------------
    verified: list[tuple[Candidate, CostEstimate, WitnessResult]] = []
    rejected: list[RejectedRewrite] = []
    rejected_ids: set[str] = set()
    pruned_ids: set[str] = set()
    total_solver_ms = 0.0

    for cand in candidates:
        # Skip if family-pruned
        if cand.id in pruned_ids:
            rejected.append(RejectedRewrite(
                candidate=cand,
                reason="family_pruned",
            ))
            continue

        # Step 4a: Structural verification
        sv_result = structural_verify(cand.ir, catalog, scope, dialect=dialect)
        if not sv_result.ok:
            logger.debug("Candidate %s failed structural verification", cand.id)
            rejected.append(RejectedRewrite(
                candidate=cand,
                reason="structural",
            ))
            rejected_ids.add(cand.id)
            continue

        # Step 4b: Bounded equivalence check via witness synthesis
        witness_result = synthesize_witness(
            original_ir, cand.ir, catalog, scope,
            validate_witnesses=validate_witnesses,
        )
        total_solver_ms += witness_result.solver_time_ms

        if witness_result.status == "unsat":
            # Equivalent — accept
            cost = cost_estimator.estimate(cand.ir, catalog)
            verified.append((cand, cost, witness_result))
            logger.info("Candidate %s verified equivalent (cost=%.1f)",
                        cand.id, cost.total_cost)

        elif witness_result.status == "sat":
            # Not equivalent — reject with witness
            rejected.append(RejectedRewrite(
                candidate=cand,
                witness_db=witness_result.witness_db,
                reason="non_equivalent",
            ))
            rejected_ids.add(cand.id)

            # Family pruning
            newly_pruned = prune_families(families, rejected_ids)
            pruned_ids |= newly_pruned

        else:
            # Unknown/timeout — conservatively reject without witness
            rejected.append(RejectedRewrite(
                candidate=cand,
                reason=f"solver_{witness_result.status}",
            ))

    # ---------------------------------------------------------------
    # Step 5: Select cheapest verified candidate
    # ---------------------------------------------------------------
    if not verified:
        logger.info("No equivalent rewrites found among %d candidates",
                    len(candidates))
        return _no_improvement(
            original_sql, original_ir, cost_original, t_start,
            rejected=rejected, total_candidates=len(candidates),
            solver_time_ms=total_solver_ms,
        )

    # Sort by cost, pick cheapest
    verified.sort(key=lambda vc: vc[1].total_cost)
    best_cand, best_cost, best_witness = verified[0]

    # Only accept if it's actually cheaper than the original
    if best_cost.total_cost >= cost_original.total_cost:
        logger.info("Best rewrite (cost=%.1f) not cheaper than original (cost=%.1f)",
                    best_cost.total_cost, cost_original.total_cost)
        return _no_improvement(
            original_sql, original_ir, cost_original, t_start,
            all_verified=verified, rejected=rejected,
            total_candidates=len(candidates), solver_time_ms=total_solver_ms,
        )

    # ---------------------------------------------------------------
    # Step 6: Build certificate for the chosen rewrite
    # ---------------------------------------------------------------
    certificate = _build_certificate(
        original_ir, best_cand.ir, catalog, scope, dialect,
        equivalence_status=best_witness.status,
        equivalence_solver_time_ms=best_witness.solver_time_ms,
        equivalence_proven_k=best_witness.proven_k,
        equivalence_complete=best_witness.complete,
    )

    optimized_sql = render(best_cand.ir, dialect=dialect)
    speedup = cost_original.total_cost / max(best_cost.total_cost, 0.001)

    t_total = (time.monotonic() - t_start) * 1000

    logger.info(
        "Optimization complete: %.1f× speedup (cost %.1f → %.1f), "
        "%d verified, %d rejected, %.0fms",
        speedup, cost_original.total_cost, best_cost.total_cost,
        len(verified), len(rejected), t_total,
    )

    return OptimizationResult(
        original_sql=original_sql,
        original_ir=original_ir,
        optimized_sql=optimized_sql,
        optimized_ir=best_cand.ir,
        certificate=certificate,
        cost_original=cost_original,
        cost_optimized=best_cost,
        speedup=speedup,
        all_verified=[(c, cost) for c, cost, _ in verified],
        rejected=rejected,
        total_candidates=len(candidates),
        solver_time_ms=total_solver_ms,
        total_time_ms=t_total,
    )


def optimize_with_config(
    sql: str,
    catalog: Catalog,
    config: OptimizerConfig,
) -> OptimizationResult:
    """Run the CEGIS optimization loop using an OptimizerConfig.

    This is the config-driven entry point that supports LLM rewrite candidates
    and fine-grained feature flags for ablation studies.

    Args:
        sql: Input SQL query to optimize.
        catalog: Schema metadata.
        config: Optimizer configuration with feature flags and parameters.

    Returns:
        OptimizationResult with the optimized query, certificate, and diagnostics.
    """
    t_start = time.monotonic()

    scope = config.scope
    dialect = config.dialect

    # Select cost estimator based on config
    if config.cost_model == "explain":
        from ..cost.estimator import ExplainCostEstimator

        cost_estimator: CostEstimator = ExplainCostEstimator(db_path=config.db_path)
    else:
        cost_estimator = SyntacticCostEstimator()

    # ---------------------------------------------------------------
    # Step 1: Parse input SQL → QueryIR
    # ---------------------------------------------------------------
    original_ir, parse_err = sql_to_ir(sql, dialect=dialect)
    if original_ir is None:
        raise ValueError(f"Failed to parse input SQL: {parse_err}")

    original_sql = render(original_ir, dialect=dialect)
    cost_original = cost_estimator.estimate(original_ir, catalog)

    # ---------------------------------------------------------------
    # Step 2: Generate rewrite candidates
    # ---------------------------------------------------------------
    candidates: list[Candidate] = []

    # Rule-based candidates
    if config.enable_rule_rewrites:
        generator = RewriteGenerator(catalog, config.rewrite_config)
        candidates.extend(generator.generate(original_ir))

    # LLM-based candidates
    if config.enable_llm_rewrites:
        try:
            from ..llm.provider import LLMConfig, create_provider

            llm_config = LLMConfig(
                provider=config.llm_provider,
                model=config.llm_model,
                n_candidates=config.llm_n_candidates,
                amp_mode=config.llm_mode,
            )
            provider = create_provider(llm_config)
            llm_candidates = provider.generate(sql, catalog, dialect=dialect)
            candidates.extend(llm_candidates)
        except Exception as e:
            logger.warning("LLM rewrite generation failed: %s", e)

    if not candidates:
        logger.info("No rewrite candidates generated")
        return _no_improvement(original_sql, original_ir, cost_original, t_start)

    # ---------------------------------------------------------------
    # Step 3: Classify into families for pruning
    # ---------------------------------------------------------------
    families = classify_rewrites(candidates)

    # ---------------------------------------------------------------
    # Step 4: Verify each candidate against the original
    # ---------------------------------------------------------------
    verified: list[tuple[Candidate, CostEstimate, Optional[WitnessResult]]] = []
    rejected: list[RejectedRewrite] = []
    rejected_ids: set[str] = set()
    pruned_ids: set[str] = set()
    total_solver_ms = 0.0

    for cand in candidates:
        # Skip if family-pruned
        if config.enable_family_pruning and cand.id in pruned_ids:
            rejected.append(RejectedRewrite(
                candidate=cand,
                reason="family_pruned",
            ))
            continue

        # Step 4a: Structural verification
        if config.enable_structural_verify:
            sv_result = structural_verify(cand.ir, catalog, scope, dialect=dialect)
            if not sv_result.ok:
                logger.debug("Candidate %s failed structural verification", cand.id)
                rejected.append(RejectedRewrite(
                    candidate=cand,
                    reason="structural",
                ))
                rejected_ids.add(cand.id)
                continue

        # Step 4b: Bounded equivalence check via witness synthesis
        if not config.enable_witness_synthesis:
            # Accept all structural-ok candidates without synthesis
            cost = cost_estimator.estimate(cand.ir, catalog)
            verified.append((cand, cost, None))
            continue

        witness_result = synthesize_witness(
            original_ir, cand.ir, catalog, scope,
            validate_witnesses=config.validate_witnesses if hasattr(config, 'validate_witnesses') else False,
        )
        total_solver_ms += witness_result.solver_time_ms

        if witness_result.status == "unsat":
            # Equivalent — accept
            cost = cost_estimator.estimate(cand.ir, catalog)
            verified.append((cand, cost, witness_result))
            logger.info("Candidate %s verified equivalent (cost=%.1f)",
                        cand.id, cost.total_cost)

        elif witness_result.status == "sat":
            # Not equivalent — reject with witness
            rejected.append(RejectedRewrite(
                candidate=cand,
                witness_db=witness_result.witness_db,
                reason="non_equivalent",
            ))
            rejected_ids.add(cand.id)

            # Family pruning
            if config.enable_family_pruning:
                newly_pruned = prune_families(families, rejected_ids)
                pruned_ids |= newly_pruned

        else:
            # Unknown/timeout — try compositional fallback (D.5)
            if config.enable_compositional and witness_result.status == "unknown":
                comp_result = _try_compositional(
                    original_ir, cand, catalog, scope, cost_estimator,
                    verified, rejected, rejected_ids, families, pruned_ids,
                    config,
                )
                if comp_result is not None:
                    total_solver_ms += (comp_result.local_result.solver_time_ms
                                        if comp_result.local_result else 0)
                    continue

            # Conservatively reject without witness
            rejected.append(RejectedRewrite(
                candidate=cand,
                reason=f"solver_{witness_result.status}",
            ))

    # ---------------------------------------------------------------
    # Step 5: Select cheapest verified candidate
    # ---------------------------------------------------------------
    if not verified:
        logger.info("No equivalent rewrites found among %d candidates",
                    len(candidates))
        return _no_improvement(
            original_sql, original_ir, cost_original, t_start,
            rejected=rejected, total_candidates=len(candidates),
            solver_time_ms=total_solver_ms,
        )

    # Sort by cost, pick cheapest
    verified.sort(key=lambda vc: vc[1].total_cost)
    best_cand, best_cost, best_witness = verified[0]

    # Only accept if it's actually cheaper than the original
    if best_cost.total_cost >= cost_original.total_cost:
        logger.info("Best rewrite (cost=%.1f) not cheaper than original (cost=%.1f)",
                    best_cost.total_cost, cost_original.total_cost)
        return _no_improvement(
            original_sql, original_ir, cost_original, t_start,
            all_verified=verified, rejected=rejected,
            total_candidates=len(candidates), solver_time_ms=total_solver_ms,
        )

    # ---------------------------------------------------------------
    # Step 6: Build certificate for the chosen rewrite
    # ---------------------------------------------------------------
    eq_status = best_witness.status if best_witness else ""
    eq_time = best_witness.solver_time_ms if best_witness else 0.0
    eq_proven_k = best_witness.proven_k if best_witness else None
    eq_complete = best_witness.complete if best_witness else True
    certificate = _build_certificate(
        original_ir, best_cand.ir, catalog, scope, dialect,
        equivalence_status=eq_status,
        equivalence_solver_time_ms=eq_time,
        equivalence_proven_k=eq_proven_k,
        equivalence_complete=eq_complete,
    )

    optimized_sql = render(best_cand.ir, dialect=dialect)
    speedup = cost_original.total_cost / max(best_cost.total_cost, 0.001)

    t_total = (time.monotonic() - t_start) * 1000

    logger.info(
        "Optimization complete: %.1f× speedup (cost %.1f → %.1f), "
        "%d verified, %d rejected, %.0fms",
        speedup, cost_original.total_cost, best_cost.total_cost,
        len(verified), len(rejected), t_total,
    )

    return OptimizationResult(
        original_sql=original_sql,
        original_ir=original_ir,
        optimized_sql=optimized_sql,
        optimized_ir=best_cand.ir,
        certificate=certificate,
        cost_original=cost_original,
        cost_optimized=best_cost,
        speedup=speedup,
        all_verified=[(c, cost) for c, cost, _ in verified],
        rejected=rejected,
        total_candidates=len(candidates),
        solver_time_ms=total_solver_ms,
        total_time_ms=t_total,
    )


def _try_compositional(
    original_ir: QueryIR,
    cand: Candidate,
    catalog: Catalog,
    scope: BoundedScope,
    cost_estimator: CostEstimator,
    verified: list,
    rejected: list,
    rejected_ids: set,
    families: list,
    pruned_ids: set,
    config: "OptimizerConfig",
) -> Optional["CompositionalResult"]:
    """Attempt compositional verification as a fallback.

    Returns CompositionalResult if compositional was attempted (regardless
    of success), or None if compositional couldn't be attempted.
    """
    try:
        from ..cegis.compositional import compositional_verify

        comp_result = compositional_verify(original_ir, cand.ir, catalog, scope)

        if comp_result.success:
            cost = cost_estimator.estimate(cand.ir, catalog)
            # Compositional success → treat as equivalence proof (no direct WitnessResult)
            verified.append((cand, cost, None))
            logger.info(
                "Candidate %s verified via compositional (%d local combos vs %d mono)",
                cand.id, comp_result.local_combo_count, comp_result.monolithic_combo_count,
            )
            return comp_result

        # Compositional failed — reject
        logger.debug(
            "Compositional verification failed for %s: %s",
            cand.id, comp_result.reason,
        )
        if comp_result.local_result and comp_result.local_result.status == "sat":
            # Local SAT is inconclusive — the outer context might filter
            # the difference. Do NOT treat as non-equivalent or prune family.
            rejected.append(RejectedRewrite(
                candidate=cand,
                reason="compositional_inconclusive:local_sat",
            ))
        else:
            rejected.append(RejectedRewrite(
                candidate=cand,
                reason=f"compositional_{comp_result.reason}",
            ))
        return comp_result

    except Exception:
        logger.debug("Compositional verification error for %s", cand.id, exc_info=True)
        return None


def _no_improvement(
    original_sql: str,
    original_ir: QueryIR,
    cost_original: CostEstimate,
    t_start: float,
    *,
    all_verified: Optional[list] = None,
    rejected: Optional[list] = None,
    total_candidates: int = 0,
    solver_time_ms: float = 0.0,
) -> OptimizationResult:
    """Return result indicating no optimization was found."""
    t_total = (time.monotonic() - t_start) * 1000
    return OptimizationResult(
        original_sql=original_sql,
        original_ir=original_ir,
        optimized_sql=original_sql,
        optimized_ir=original_ir,
        certificate=None,
        cost_original=cost_original,
        cost_optimized=cost_original,
        speedup=1.0,
        all_verified=all_verified or [],
        rejected=rejected or [],
        total_candidates=total_candidates,
        solver_time_ms=solver_time_ms,
        total_time_ms=t_total,
    )


def _build_certificate(
    original_ir: QueryIR,
    rewrite_ir: QueryIR,
    catalog: Catalog,
    scope: BoundedScope,
    dialect: str,
    equivalence_status: str = "",
    equivalence_solver_time_ms: float = 0.0,
    equivalence_proven_k: Optional[int] = None,
    equivalence_complete: bool = True,
) -> Optional[Certificate]:
    """Build an equivalence certificate for the chosen rewrite.

    FIX.28a: Now records both original and rewrite IRs with the
    equivalence proof (UNSAT witness synthesis), not just structural
    validity of the rewrite.

    FIX.28b: Records proven_k and complete metadata so the certificate
    explicitly states the cardinality bound at which equivalence was proved.
    """
    try:
        sv = structural_verify(rewrite_ir, catalog, scope, dialect=dialect)
        if sv.ok:
            return create_certificate(
                rewrite_ir, catalog, sv, scope, dialect,
                original_ir=original_ir,
                equivalence_status=equivalence_status,
                equivalence_solver_time_ms=equivalence_solver_time_ms,
                equivalence_proven_k=equivalence_proven_k,
                equivalence_complete=equivalence_complete,
            )
    except Exception:
        logger.exception("Failed to create certificate")
    return None


def _validate_witness_db(
    original_ir: QueryIR,
    rewrite_ir: QueryIR,
    witness_db: dict,
    catalog: Catalog,
) -> None:
    """Validate a witness database by executing both queries in SQLite."""
    try:
        val = validate_witness(original_ir, rewrite_ir, witness_db, catalog)
        if not val.results_differ:
            logger.warning(
                "Witness validation FAILED: queries agree on witness DB. "
                "Q1=%s Q2=%s", val.q1_result, val.q2_result,
            )
    except Exception:
        logger.debug("Witness validation skipped (execution error)")
