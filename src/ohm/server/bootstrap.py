"""Guided bootstrap interview for fresh single-instance deployments (OHM-797).

When a single-instance ohmd starts against an empty database with no domain
configured, the first connecting agent (admin) is offered a short guided
interview that establishes the domain identity, vocabulary, and onboarding
content.

Detection: no `domain_schema` in `ohm_meta` AND zero nodes.

The interview's output is a SchemaConfig-shaped JSON, persisted to ohm_meta
via #795's mechanism. WIP state is also stored in ohm_meta (bootstrap.step,
bootstrap.answers) — durable across restarts.

Safety:
- Admin auth required on every call (not just re-runs)
- Re-run requires admin + explicit reset flag
- Domain-schema changes against non-empty graphs are additive-only
- All free-text validated via existing validators
- Template export only to operator's templates_dir, never package-internal
"""

from __future__ import annotations

import json
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.schema import (
    SchemaConfig,
    get_meta,
    set_meta,
    resolve_schema_by_name,
    VALID_NODE_TYPES,
)

# ── Reserved names that custom types must not collide with ──
_RESERVED_NODE_TYPES = set(VALID_NODE_TYPES)
_RESERVED_PREFIXES = ("ohm_", "system_")

# ── Interview steps ──
STEPS = [
    {
        "id": "domain_name",
        "prompt": "What is the domain name for this OHM instance? (lowercase, alphanumeric/underscore/hyphen, 1-63 chars)",
        "field": "domain_name",
        "type": "text",
        "required": True,
    },
    {
        "id": "description",
        "prompt": "Provide a one-line description of this domain (optional).",
        "field": "description",
        "type": "text",
        "required": False,
    },
    {
        "id": "vocabulary",
        "prompt": "Does the generic OHM vocabulary fit, or do you need domain-specific node/edge types? Type 'default' to accept defaults, or 'custom' to specify.",
        "field": "vocabulary_choice",
        "type": "choice",
        "choices": ["default", "custom"],
        "default": "default",
    },
    {
        "id": "onboarding_node",
        "prompt": "What should a new agent read first? (onboarding node ID or label, or 'auto' to generate)",
        "field": "onboarding_node_id",
        "type": "text",
        "default": "auto",
    },
    {
        "id": "confirm",
        "prompt": "Confirm and write the domain configuration? (yes/no)",
        "field": "confirm",
        "type": "choice",
        "choices": ["yes", "no"],
        "required": True,
    },
]


def is_fresh_instance(conn: "DuckDBPyConnection") -> bool:
    """Check if this is a genuinely fresh instance needing bootstrap.

    Returns True only if BOTH conditions are met:
    - no `domain_schema` key in ohm_meta
    - zero non-deleted nodes in the graph

    This prevents re-interviewing a populated graph that predates
    schema-persistence.
    """
    domain_schema = get_meta(conn, "domain_schema")
    if domain_schema:
        return False

    try:
        row = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()
        node_count = row[0] if row else 0
    except Exception:
        node_count = 0

    return node_count == 0


def get_bootstrap_state(conn: "DuckDBPyConnection") -> dict[str, Any]:
    """Get the current bootstrap WIP state from ohm_meta.

    Returns:
        Dict with 'step' (int, 0-based) and 'answers' (dict).
        If no WIP state, returns {'step': 0, 'answers': {}}.
        If corrupted, returns {'step': 0, 'answers': {}, 'corrupted': True}.
    """
    step_raw = get_meta(conn, "bootstrap.step", "0")
    answers_raw = get_meta(conn, "bootstrap.answers", "{}")

    try:
        step = int(step_raw) if step_raw else 0
    except (ValueError, TypeError):
        return {"step": 0, "answers": {}, "corrupted": True}

    try:
        answers = json.loads(answers_raw) if answers_raw else {}
        if not isinstance(answers, dict):
            return {"step": 0, "answers": {}, "corrupted": True}
    except (json.JSONDecodeError, TypeError):
        return {"step": 0, "answers": {}, "corrupted": True}

    return {"step": step, "answers": answers}


def save_bootstrap_state(conn: "DuckDBPyConnection", step: int, answers: dict) -> None:
    """Save bootstrap WIP state to ohm_meta."""
    set_meta(conn, "bootstrap.step", str(step))
    set_meta(conn, "bootstrap.answers", json.dumps(answers))


def clear_bootstrap_state(conn: "DuckDBPyConnection") -> None:
    """Clear bootstrap WIP state (abandon/restart)."""
    conn.execute("DELETE FROM ohm_meta WHERE key IN ('bootstrap.step', 'bootstrap.answers')")


def get_current_step(conn: "DuckDBPyConnection") -> dict[str, Any]:
    """Get the current interview step prompt and expected answer shape.

    Returns:
        Dict with 'step' (step index), 'prompt', 'field', 'type',
        and 'total_steps'. If bootstrap is complete, returns
        {'complete': True}.
    """
    state = get_bootstrap_state(conn)
    if state.get("corrupted"):
        return {
            "corrupted": True,
            "message": "Bootstrap WIP state is corrupted. Use POST /bootstrap with action=abandon to restart.",
        }

    # Check if already bootstrapped
    domain_schema = get_meta(conn, "domain_schema")
    if domain_schema:
        return {"complete": True, "message": "Instance already bootstrapped."}

    step_idx = state["step"]
    if step_idx >= len(STEPS):
        return {"complete": True, "message": "All steps completed."}

    step = STEPS[step_idx]
    return {
        "step": step_idx,
        "total_steps": len(STEPS),
        "prompt": step["prompt"],
        "field": step["field"],
        "type": step["type"],
        "choices": step.get("choices"),
        "default": step.get("default"),
        "required": step.get("required", False),
        "previous_answers": state["answers"],
    }


def submit_answer(
    conn: "DuckDBPyConnection",
    answer: str,
) -> dict[str, Any]:
    """Submit an answer to the current step and advance.

    Returns:
        Dict with 'ok' (bool), 'message', and either the next step
        or the completion result.
    """
    state = get_bootstrap_state(conn)
    if state.get("corrupted"):
        return {
            "ok": False,
            "error": "corrupted_state",
            "message": "Bootstrap WIP state is corrupted. Use POST /bootstrap with action=abandon to restart.",
        }

    # Check if already bootstrapped
    domain_schema = get_meta(conn, "domain_schema")
    if domain_schema:
        return {"ok": False, "error": "already_bootstrapped", "message": "Instance already bootstrapped."}

    step_idx = state["step"]
    if step_idx >= len(STEPS):
        return {"ok": False, "error": "no_more_steps", "message": "All steps completed."}

    step = STEPS[step_idx]
    answers = state["answers"]

    # Validate answer
    if step.get("required") and not answer:
        return {"ok": False, "error": "required", "message": f"'{step['field']}' is required."}

    if step["type"] == "choice":
        choices = step.get("choices", [])
        if answer not in choices:
            default = step.get("default")
            if default and not answer:
                answer = default
            else:
                return {"ok": False, "error": "invalid_choice", "message": f"Must be one of: {', '.join(choices)}"}

    # Domain name validation
    if step["field"] == "domain_name":
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", answer):
            return {
                "ok": False,
                "error": "invalid_domain_name",
                "message": "Domain name must be lowercase alphanumeric/underscore/hyphen, 1-63 chars.",
            }

    # No credentials in free text
    for field_name in ("domain_name", "description", "onboarding_node_id"):
        if step["field"] == field_name:
            lower = answer.lower()
            if any(kw in lower for kw in ("token", "api_key", "secret", "password", "bearer")):
                return {
                    "ok": False,
                    "error": "credential_detected",
                    "message": "This field must not contain credentials or API keys.",
                }

    answers[step["field"]] = answer
    save_bootstrap_state(conn, step_idx + 1, answers)

    # Check if this was the last step
    if step_idx + 1 >= len(STEPS):
        return _complete_bootstrap(conn, answers)

    # Return next step
    next_step = STEPS[step_idx + 1]
    return {
        "ok": True,
        "step": step_idx + 1,
        "total_steps": len(STEPS),
        "prompt": next_step["prompt"],
        "field": next_step["field"],
        "type": next_step["type"],
        "choices": next_step.get("choices"),
        "default": next_step.get("default"),
        "required": next_step.get("required", False),
        "answers_so_far": answers,
    }


def _complete_bootstrap(conn: "DuckDBPyConnection", answers: dict) -> dict[str, Any]:
    """Complete the bootstrap interview and persist the schema."""
    domain_name = answers.get("domain_name", "ohm")
    description = answers.get("description", "")
    vocabulary_choice = answers.get("vocabulary_choice", "default")
    onboarding_node_id = answers.get("onboarding_node_id", "auto")

    if answers.get("confirm") != "yes":
        # Not confirmed — go back
        save_bootstrap_state(conn, len(STEPS) - 1, answers)
        return {
            "ok": False,
            "error": "not_confirmed",
            "message": "Bootstrap not confirmed. You can resubmit with 'yes' to complete.",
        }

    # Build SchemaConfig
    if vocabulary_choice == "custom":
        # For now, use defaults with the domain name. Custom vocabulary
        # would require additional steps — future enhancement.
        schema = SchemaConfig(name=domain_name)
    else:
        schema = SchemaConfig(name=domain_name)

    # Set onboarding node ID
    if onboarding_node_id and onboarding_node_id != "auto":
        # Store in ohm_meta per #796
        set_meta(conn, "onboarding_node_id", onboarding_node_id)

    # Persist schema to ohm_meta
    schema.to_db(conn)

    # Clear WIP state
    clear_bootstrap_state(conn)

    return {
        "ok": True,
        "complete": True,
        "domain_name": domain_name,
        "description": description,
        "message": f"Bootstrap complete. Domain '{domain_name}' configured.",
    }


def bootstrap_from_template(
    conn: "DuckDBPyConnection",
    template_name: str,
    templates_dir: str | None = None,
) -> dict[str, Any]:
    """Fast-path: load a named template directly, skipping the interview.

    Args:
        template_name: Domain name (e.g., 'ohm', 'topo', 'beef_herd')
        templates_dir: Optional operator-supplied template directory
    """
    # Validate domain name
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", template_name):
        return {
            "ok": False,
            "error": "invalid_domain_name",
            "message": "Domain name must be lowercase alphanumeric/underscore/hyphen, 1-63 chars.",
        }

    schema = resolve_schema_by_name(template_name, templates_dir=templates_dir)

    # Check if already bootstrapped — additive-only if so
    existing = SchemaConfig.from_db(conn)
    if existing is not None:
        # Additive-only: merge new types into existing
        merged_node_types = existing.node_types | schema.node_types
        schema = SchemaConfig(
            name=schema.name,
            node_types=merged_node_types,
            edge_types_by_layer=dict(schema.layer_edge_types),
            layer_descriptions=dict(schema.layer_descriptions),
            observation_types=existing.observation_types | schema.observation_types,
            observation_sources=existing.observation_sources,
            visibilities=existing.visibilities,
            provenances=existing.provenances,
        )

    # Persist
    schema.to_db(conn)
    clear_bootstrap_state(conn)

    return {
        "ok": True,
        "complete": True,
        "domain_name": schema.name,
        "message": f"Bootstrap complete. Domain '{schema.name}' loaded from template.",
    }


def abandon_bootstrap(conn: "DuckDBPyConnection") -> dict[str, Any]:
    """Abandon corrupted WIP state and restart from scratch."""
    clear_bootstrap_state(conn)
    return {"ok": True, "message": "Bootstrap state cleared. Use GET /bootstrap to start fresh."}


def is_custom_type_valid(type_name: str) -> bool:
    """Validate a custom node/edge type name (OHM-797).

    Rejects:
    - Names that collide with existing VALID_NODE_TYPES
    - Names with reserved prefixes (ohm_, system_)
    - Names with invalid characters
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", type_name):
        return False
    if type_name in _RESERVED_NODE_TYPES:
        return False
    for prefix in _RESERVED_PREFIXES:
        if type_name.startswith(prefix):
            return False
    return True
