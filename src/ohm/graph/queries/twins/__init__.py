"""Twins subpackage — re-exports for queries/__init__.py (OHM-447 Phase 2)."""

from ohm.graph.queries.twins.core import (
    register_twin, twin_predict, twin_constraints,
    validate_action_against_twin, explain_twin,
    create_twin_template, list_twin_templates,
    get_twin_template, instantiate_twin_from_template,
    assemble_twin_for_decision,
)
from ohm.graph.queries.twins.model_registry import (
    register_model_candidate, evaluate_model, compare_models,
    promote_model, register_shadow_model,
    detect_drift, run_walk_forward_validation,
    ensemble_predict, compute_decision_value,
    auto_retire_model, set_freshness_threshold,
    get_freshness_status,
)
from ohm.graph.queries.twins.design_sessions import (
    start_twin_design_session, transition_session,
    add_session_observation, propose_twin_config,
    review_proposal, instantiate_from_session,
    record_calibration, evolve_session,
    get_session_state, get_session_audit,
    set_promotion_policy,
    auto_promote_best_model,
)
from ohm.graph.queries.twins.bindings import (
    register_twin_with_bindings, add_twin_bindings,
    attach_twin_models, get_twin_readiness,
)
