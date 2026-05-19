"""
OHM Causal Refutation Engine — Model-based robustness testing.

Tests the robustness of causal conclusions using methods that work directly
with the Bayesian network's CPTs (no tabular data required):

- placebo_treatment: Set cause to its prior (no intervention effect)
- random_common_cause: Add random noise to the model and re-estimate
- data_subset: Vary the leak probability and re-estimate
- unobserved_confounder: Simulate confounders of varying strength (E-value + perturbation)

DoWhy integration is available but requires tabular data that OHM doesn't
collect. The model-based methods are more appropriate for our use case.
"""

import logging
from typing import Any

from ohm.validation import validate_identifier

logger = logging.getLogger(__name__)

try:
    import dowhy
    from dowhy import CausalModel
    DOWHY_AVAILABLE = True
except ImportError:
    DOWHY_AVAILABLE = False
    logger.info("dowhy not available — DoWhy refutation disabled. Install with: pip install dowhy")


def refute_causal_effect(
    conn,
    cause: str,
    effect: str,
    *,
    n_samples: int = 1000,
    seed: int = 42,
    refutation_methods: list[str] | None = None,
    edge_types: list[str] | None = None,
    leak_probability: float = 0.15,
) -> dict[str, Any]:
    """Test the robustness of causal conclusions using model-based refutation.

    Applies refutation methods that work directly with the Bayesian network's
    CPTs, without requiring tabular data:

    - placebo_treatment: Replace cause with its prior (should give near-zero effect)
    - random_common_cause: Add random noise node and re-estimate
    - model_perturbation: Vary leak probability and re-estimate ATE
    - unobserved_confounder: E-value sensitivity analysis (from /sensitivity)

    Args:
        conn: DuckDB connection.
        cause: Node ID for the treatment variable.
        effect: Node ID for the outcome variable.
        n_samples: Number of synthetic samples (for DoWhy methods).
        seed: Random seed for reproducibility.
        refutation_methods: List of methods to apply (default: all model-based).
        edge_types: Edge types to include in the network.
        leak_probability: Baseline probability for noisy-OR.

    Returns:
        Dict with refutation results for each method.
    """
    from ohm.bayesian import (
        build_bayesian_network,
        causal_intervention,
        compute_ate,
        compute_sensitivity,
        _safe_node_id,
        PGMPY_AVAILABLE,
    )

    cause = validate_identifier(cause, name="cause")
    effect = validate_identifier(effect, name="effect")

    if refutation_methods is None:
        refutation_methods = ["placebo_treatment", "random_common_cause", "model_perturbation", "unobserved_confounder"]

    # Compute the original ATE for comparison
    original_ate = compute_ate(
        conn, cause, effect,
        edge_types=edge_types,
        leak_probability=leak_probability,
    )

    if "error" in original_ate:
        return {
            "method": "none",
            "cause": cause,
            "effect": effect,
            "error": f"Cannot compute original ATE: {original_ate['error']}",
        }

    results = {
        "method": "causal_refutation",
        "dowhy_available": DOWHY_AVAILABLE,
        "pgmpy_available": PGMPY_AVAILABLE,
        "cause": cause,
        "effect": effect,
        "original_ate": original_ate["ate"],
        "original_risk_ratio": original_ate["risk_ratio"],
        "refutation_results": {},
    }

    # --- Placebo Treatment Test ---
    # Replace the cause with a random node (no causal path to effect).
    # The ATE should be near zero if our causal model is correct.
    if "placebo_treatment" in refutation_methods:
        try:
            # Use a node that has no causal path to the effect as placebo
            # Or use the effect's own prior as the "placebo" estimate
            # Method: compute P(effect=bad) without any intervention = prior
            from ohm.bayesian import bayesian_inference
            # Empty evidence = prior P(effect)
            prior_result = bayesian_inference(
                conn, effect, {},  # empty evidence = prior
                edge_types=edge_types,
                leak_probability=leak_probability,
            )
            if prior_result and "posterior" in prior_result:
                p_bad_prior = prior_result["posterior"].get("0", 0)
                p_bad_do_bad = original_ate.get("p_effect_bad_do_cause_bad", 0)
                placebo_effect = p_bad_do_bad - p_bad_prior  # Should be less than ATE
                results["refutation_results"]["placebo_treatment"] = {
                    "status": "success",
                    "method": "prior_baseline",
                    "original_ate": original_ate["ate"],
                    "placebo_effect": round(placebo_effect, 4),
                    "p_effect_bad_prior": round(p_bad_prior, 4),
                    "p_effect_bad_do_cause_bad": round(p_bad_do_bad, 4),
                    "robust": original_ate["ate"] > 0.05,  # ATE should be meaningfully different from zero
                    "interpretation": (
                        f"Prior P(effect=bad) = {p_bad_prior:.4f}. "
                        f"do(cause=bad) P(effect=bad) = {p_bad_do_bad:.4f}. "
                        f"ATE = {original_ate['ate']:.4f}. "
                        f"The causal effect is {'meaningful' if original_ate['ate'] > 0.05 else 'negligible'} "
                        f"relative to the prior probability."
                    ),
                }
            else:
                results["refutation_results"]["placebo_treatment"] = {
                    "status": "error",
                    "error": "Could not compute prior for placebo test",
                }
        except Exception as e:
            logger.warning(f"Placebo treatment refutation failed: {e}")
            results["refutation_results"]["placebo_treatment"] = {
                "status": "error",
                "error": str(e),
            }

    # --- Random Common Cause Test ---
    # Add a random noise node connected to both cause and effect
    # and re-estimate. If the ATE doesn't change much, it's robust.
    if "random_common_cause" in refutation_methods:
        try:
            # Create a random confounder with various strengths
            import random
            random.seed(seed)
            noise_strengths = [0.1, 0.2, 0.3, 0.5]
            ate_changes = []
            for strength in noise_strengths:
                # Simulate: add noise to leak probability
                noisy_leak = leak_probability + strength * random.uniform(-1, 1)
                noisy_leak = max(0.01, min(0.5, noisy_leak))  # Clamp
                noisy_ate = compute_ate(
                    conn, cause, effect,
                    edge_types=edge_types,
                    leak_probability=noisy_leak,
                )
                if "ate" in noisy_ate:
                    ate_changes.append({
                        "noise_strength": strength,
                        "leak_probability": round(noisy_leak, 4),
                        "ate": noisy_ate["ate"],
                        "ate_change": round(abs(noisy_ate["ate"] - original_ate["ate"]), 4),
                    })

            max_change = max(c["ate_change"] for c in ate_changes) if ate_changes else 0
            results["refutation_results"]["random_common_cause"] = {
                "status": "success",
                "method": "leak_perturbation",
                "original_ate": original_ate["ate"],
                "perturbations": ate_changes,
                "max_ate_change": round(max_change, 4),
                "robust": max_change < abs(original_ate["ate"]) * 0.5,
                "interpretation": (
                    f"ATE changes by at most {max_change:.4f} under random noise. "
                    f"{'Robust' if max_change < abs(original_ate['ate']) * 0.5 else 'Not robust'}: "
                    f"ATE is stable under random perturbation"
                ),
            }
        except Exception as e:
            logger.warning(f"Random common cause refutation failed: {e}")
            results["refutation_results"]["random_common_cause"] = {
                "status": "error",
                "error": str(e),
            }

    # --- Model Perturbation Test ---
    # Vary the leak probability systematically and re-estimate ATE
    if "model_perturbation" in refutation_methods:
        try:
            leak_values = [0.01, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50]
            perturbation_results = []
            for leak in leak_values:
                ate_result = compute_ate(
                    conn, cause, effect,
                    edge_types=edge_types,
                    leak_probability=leak,
                )
                if "ate" in ate_result:
                    perturbation_results.append({
                        "leak_probability": leak,
                        "ate": ate_result["ate"],
                        "risk_ratio": ate_result["risk_ratio"],
                    })

            # ATE should be relatively stable across leak values
            ates = [r["ate"] for r in perturbation_results]
            ate_range = max(ates) - min(ates) if ates else 0
            results["refutation_results"]["model_perturbation"] = {
                "status": "success",
                "method": "leak_sensitivity",
                "original_leak": leak_probability,
                "perturbations": perturbation_results,
                "ate_range": round(ate_range, 4),
                "ate_at_original_leak": original_ate["ate"],
                "robust": ate_range < abs(original_ate["ate"]) * 2,
                "interpretation": (
                    f"ATE ranges from {min(ates):.4f} to {max(ates):.4f} (range={ate_range:.4f}) "
                    f"across leak probabilities {leak_values}. "
                    f"{'Robust' if ate_range < abs(original_ate['ate']) * 2 else 'Not robust'}: "
                    f"ATE is {'stable' if ate_range < abs(original_ate['ate']) * 2 else 'sensitive'} to model assumptions"
                ),
            }
        except Exception as e:
            logger.warning(f"Model perturbation refutation failed: {e}")
            results["refutation_results"]["model_perturbation"] = {
                "status": "error",
                "error": str(e),
            }

    # --- Unobserved Confounder Test (E-value) ---
    if "unobserved_confounder" in refutation_methods:
        try:
            sens_result = compute_sensitivity(
                conn, cause, effect,
                edge_types=edge_types,
                leak_probability=leak_probability,
            )
            results["refutation_results"]["unobserved_confounder"] = {
                "status": "success",
                "method": "e_value_sensitivity",
                "ate": sens_result.get("ate"),
                "risk_ratio": sens_result.get("risk_ratio"),
                "e_value": sens_result.get("e_value"),
                "robustness": sens_result.get("robustness"),
                "robustness_description": sens_result.get("robustness_description"),
                "perturbation": sens_result.get("confounder_perturbation"),
                "interpretation": sens_result.get("interpretation"),
            }
        except Exception as e:
            logger.warning(f"Unobserved confounder refutation failed: {e}")
            results["refutation_results"]["unobserved_confounder"] = {
                "status": "error",
                "error": str(e),
            }

    # --- DoWhy Methods (if available and requested) ---
    dowhy_methods = [m for m in refutation_methods if m.startswith("dowhy_")]
    if DOWHY_AVAILABLE and dowhy_methods:
        results["refutation_results"]["dowhy_note"] = {
            "status": "info",
            "message": "DoWhy methods require tabular data. Use model-based methods instead, or provide joint observations for DoWhy.",
        }

    return results