#!/usr/bin/env python3
"""Seed Agent Security cluster in OHM (OHM-4qd).

Adversarial domain testing cluster with AND-gate security controls
and OR-gate bypass patterns. Tests whether OHM correctly identifies
attack surfaces where AND-gates (authentication, authorization) can
be bypassed via OR-gate paths (prompt injection, social engineering,
mid-execution evasion).

Usage:
    python seed_agent_security_cluster.py [--base URL] [--token TOKEN]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    print("requests is required: pip install requests")
    sys.exit(1)


def create_node(base: str, headers: dict, node_id: str, label: str,
                node_type: str, content: str, confidence: float,
                provenance: str) -> None:
    r = requests.post(
        f"{base}/node?create_only=false",
        headers=headers,
        json={
            "id": node_id,
            "label": label,
            "type": node_type,
            "content": content,
            "confidence": confidence,
            "provenance": provenance,
            "visibility": "team",
        },
    )
    if r.status_code not in (200, 201):
        print(f"  WARN: node {node_id}: {r.status_code} {r.text[:120]}")
    time.sleep(0.3)


def create_edge(base: str, headers: dict, from_id: str, to_id: str,
                edge_type: str, layer: str, confidence: float,
                provenance: str, probability: float | None = None) -> None:
    body = {
        "from": from_id,
        "to": to_id,
        "type": edge_type,
        "layer": layer,
        "confidence": confidence,
        "provenance": provenance,
    }
    if probability is not None:
        body["probability"] = probability
    r = requests.post(f"{base}/edge", headers=headers, json=body)
    if r.status_code not in (200, 201):
        print(f"  WARN: edge {from_id}->{to_id}: {r.status_code} {r.text[:120]}")
    time.sleep(0.15)


def seed_cluster(base: str, headers: dict) -> None:
    print("Seeding Agent Security cluster...")

    # ── Attack Pattern Nodes (OR-gate bypasses) ──────────────────────

    create_node(base, headers,
        "adv-prompt-injection", "Adversarial Prompt Injection",
        "pattern",
        "OR-gate bypass of input validation. Any single injection vector "
        "(system prompt override, context manipulation, instruction hiding, "
        "multi-modal smuggling) suffices to bypass the AND-gate of input "
        "sanitization. The attacker needs only ONE path to succeed while "
        "the defender must close ALL paths simultaneously.",
        0.92, "metis-security")

    create_node(base, headers,
        "adv-nl-coercion", "Natural Language Coercion",
        "pattern",
        "Social engineering via natural language rather than code. Exploits "
        "the ambiguity of human language to create plausible-sounding "
        "requests that bypass authorization AND-gates. The OR-gate: any "
        "plausible framing (urgency, authority, dependency) can convince "
        "the agent to act outside its scope.",
        0.88, "metis-security")

    create_node(base, headers,
        "adv-mid-execution-evasion", "Mid-Execution Evasion",
        "pattern",
        "Attack during execution when AND-gate checks are already passed. "
        "The agent has been authorized, the task is running, and the "
        "security boundary has been crossed. OR-gate: any runtime "
        "modification (tool substitution, output redirection, context "
        "drift) can alter the authorized behavior without re-triggering "
        "authorization checks.",
        0.85, "metis-security")

    create_node(base, headers,
        "adv-identity-theft", "Agent Identity Theft",
        "pattern",
        "Exploiting agent identity stability gaps. When model changes "
        "invalidate identity, the AND-gate of accountability fails: the "
        "agent that acted is not the agent being held accountable. OR-gate: "
        "any identity confusion (version change, prompt modification, "
        "context injection) breaks the identity chain.",
        0.87, "metis-security")

    # ── Authorization Gap Nodes (AND-gate failures) ──────────────────

    create_node(base, headers,
        "auth-scope-gap", "Authorization Scope Gap",
        "vulnerability",
        "AND-gate failure: agents receive overly broad access scopes. "
        "The AND-gate of 'need this specific resource AND this specific "
        "action AND this specific time' is replaced by an OR-gate where "
        "any single scope grant opens the entire permission plane. "
        "74% of deployed agents are over-privileged.",
        0.90, "metis-security")

    create_node(base, headers,
        "auth-visibility-gap", "Authorization Visibility Gap",
        "vulnerability",
        "AND-gate failure: agent actions are indistinguishable from human "
        "actions in audit logs. The AND-gate of 'visible that agent acted "
        "AND visible what agent did AND visible who authorized' is broken "
        "because agent actions appear as human actions. The OR-gate: any "
        "action without agent attribution is invisible.",
        0.88, "metis-security")

    create_node(base, headers,
        "auth-control-gap", "Authorization Control Gap",
        "vulnerability",
        "AND-gate failure: flat LLM permission plane. All tokens have equal "
        "weight in the context window — there is no hierarchical access "
        "control. The AND-gate of 'authenticated AND authorized AND scoped' "
        "collapses because all instructions in context carry the same "
        "authority weight.",
        0.89, "metis-security")

    create_node(base, headers,
        "auth-accountability-gap", "Authorization Accountability Gap",
        "vulnerability",
        "AND-gate failure: no single owner for agent actions. When an agent "
        "acts, who is responsible — the developer, deployer, user, or model? "
        "The AND-gate of 'action taken AND owner identified AND consequences "
        "assigned' fails because accountability is diffused across multiple "
        "parties.",
        0.86, "metis-security")

    # ── Governance Nodes ─────────────────────────────────────────────

    create_node(base, headers,
        "gov-telos-governance", "Telos Governance Framework",
        "concept",
        "Agent governance through purpose definition. Telos (purpose) as "
        "AND-gate: agent actions must align with stated purpose AND be "
        "constrained by purpose scope AND be reversible if purpose is "
        "violated. The OR-gate threat: any ambiguity in purpose definition "
        "creates bypass opportunities.",
        0.82, "metis-security")

    create_node(base, headers,
        "gov-devenex-execution", "Devenex Agent Execution Model",
        "concept",
        "Execution sandbox model for agent actions. Devenex constrains "
        "agent execution to an AND-gate: actions must be pre-approved AND "
        "execution must be monitored AND deviations must be caught. The "
        "OR-gate threat: any execution path not explicitly constrained "
        "is implicitly permitted.",
        0.80, "metis-security")

    # ── Edges: Attack patterns threaten authorization gaps ───────────

    print("Creating attack→vulnerability edges...")
    create_edge(base, headers, "adv-prompt-injection", "auth-scope-gap",
        "THREATENS", "L4", 0.90, "metis-security", 0.85)
    create_edge(base, headers, "adv-prompt-injection", "auth-control-gap",
        "THREATENS", "L4", 0.88, "metis-security", 0.80)
    create_edge(base, headers, "adv-nl-coercion", "auth-visibility-gap",
        "THREATENS", "L4", 0.85, "metis-security", 0.75)
    create_edge(base, headers, "adv-nl-coercion", "auth-scope-gap",
        "THREATENS", "L4", 0.82, "metis-security", 0.70)
    create_edge(base, headers, "adv-mid-execution-evasion", "auth-control-gap",
        "THREATENS", "L4", 0.88, "metis-security", 0.82)
    create_edge(base, headers, "adv-mid-execution-evasion", "auth-accountability-gap",
        "THREATENS", "L4", 0.85, "metis-security", 0.78)
    create_edge(base, headers, "adv-identity-theft", "auth-accountability-gap",
        "THREATENS", "L4", 0.92, "metis-security", 0.88)
    create_edge(base, headers, "adv-identity-theft", "auth-visibility-gap",
        "THREATENS", "L4", 0.80, "metis-security", 0.72)

    # ── Edges: Attack patterns cause each other (attack chains) ──────

    print("Creating attack chain edges...")
    create_edge(base, headers, "adv-prompt-injection", "adv-mid-execution-evasion",
        "CAUSES", "L3", 0.75, "metis-security", 0.65)
    create_edge(base, headers, "adv-nl-coercion", "adv-identity-theft",
        "CAUSES", "L3", 0.70, "metis-security", 0.60)
    create_edge(base, headers, "adv-mid-execution-evasion", "adv-identity-theft",
        "CAUSES", "L3", 0.65, "metis-security", 0.55)

    # ── Edges: Governance mitigates authorization gaps ───────────────

    print("Creating governance→vulnerability edges...")
    create_edge(base, headers, "gov-telos-governance", "auth-scope-gap",
        "DEPENDS_ON", "L4", 0.80, "metis-security")
    create_edge(base, headers, "gov-telos-governance", "auth-accountability-gap",
        "DEPENDS_ON", "L4", 0.78, "metis-security")
    create_edge(base, headers, "gov-devenex-execution", "auth-control-gap",
        "DEPENDS_ON", "L4", 0.82, "metis-security")
    create_edge(base, headers, "gov-devenex-execution", "auth-visibility-gap",
        "DEPENDS_ON", "L4", 0.75, "metis-security")

    # ── Edges: Link to existing AND→OR framework ────────────────────

    print("Linking to AND→OR framework...")
    create_edge(base, headers, "adv-prompt-injection",
        "concept-agent-authorization-gap",
        "APPLIES_TO", "L3", 0.90, "metis-security")
    create_edge(base, headers, "adv-mid-execution-evasion",
        "concept-least-privilege-as-and-gate",
        "APPLIES_TO", "L3", 0.85, "metis-security")
    create_edge(base, headers, "adv-identity-theft",
        "concept-agent-identity-stability",
        "APPLIES_TO", "L3", 0.92, "metis-security")
    create_edge(base, headers, "auth-scope-gap",
        "concept-privilege-escalation-path",
        "APPLIES_TO", "L3", 0.88, "metis-security")
    create_edge(base, headers, "auth-accountability-gap",
        "concept-delegated-authority-problem",
        "APPLIES_TO", "L3", 0.85, "metis-security")

    # ── Checkpoint ───────────────────────────────────────────────────

    print("Checkpointing...")
    r = requests.post(f"{base}/admin/checkpoint", headers=headers, json={})
    if r.status_code == 200:
        print("Checkpoint complete.")
    else:
        print(f"Checkpoint warning: {r.status_code}")

    print("Agent Security cluster seeded.")


def main():
    parser = argparse.ArgumentParser(description="Seed Agent Security cluster in OHM")
    parser.add_argument("--base", default="http://127.0.0.1:8710",
                        help="OHM daemon base URL")
    parser.add_argument("--token", default=None,
                        help="Bearer token (or set OHM_TOKEN env var)")
    args = parser.parse_args()

    token = args.token or os.environ.get("OHM_TOKEN")
    if not token:
        try:
            with open("/root/olympus/shared/ohm-config.json") as f:
                config = json.load(f)
            token = config["agents"]["metis"]
        except Exception:
            print("Error: No token provided. Use --token, OHM_TOKEN env var, or ohm-config.json")
            sys.exit(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    seed_cluster(args.base, headers)


if __name__ == "__main__":
    main()
