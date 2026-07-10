"""Nudge taxonomy — classification layer over existing nudge types (OHM-769).

This module provides:

1. **NudgeCategory** enum: 8 high-level purposes + 6 new categories
   from the agora/deliberation design.
2. **NUDGE_TYPE_MAP**: maps each of the 19 existing nudge type strings
   to a category and default severity.
3. **classify_nudge()**: returns (category, severity) for any nudge.
4. **SeverityLevel**: 0-silent, 1-context, 2-soft, 3-firm.
5. **compute_severity()**: adjusts severity based on divergence, blast
   radius, and calibration profile.
6. **NudgeThrottle**: per-target, per-type throttle to prevent
   over-nudging (same type for same target is throttled to once per
   N turns unless belief moves by >0.15).

This is a *classification layer* alongside the existing ``type`` field
in nudges.py — callers key behavior off of ``type`` (existing), while
``category`` and ``severity`` provide the higher-level taxonomy for
throttling, filtering, and per-agent personalization.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any


class NudgeCategory(enum.Enum):
    """High-level nudge purposes (OHM-769 taxonomy)."""

    CONTEXT = "context"  # Append graph belief to agent's context
    CALIBRATION = "calibration"  # Flag over/under-confident language
    VOI = "voi"  # Suggest observation before high-impact decision
    PERT = "pert"  # Encourage range estimates
    METHOD = "method"  # Flag divergence between statistical methods
    PLURALISM = "pluralism"  # Surface multi-agent disagreement
    SYNTHESIS = "synthesis"  # Suggest recording accumulated belief
    AUTONOMY = "autonomy"  # Withhold graph belief to elicit own view
    NOVELTY = "novelty"  # Ask for disconfirming evidence
    STALE_EVIDENCE = "stale_evidence"  # Graph belief rests on old evidence
    METHOD_DIVERGENCE = "method_divergence"  # Statistical methods disagree
    SOCRATIC = "socratic"  # "What would change your mind?"
    EVIDENCE_RACE = "evidence_race"  # Disagreement resolution mechanism
    THRESHOLD_NOT_MET = "threshold_not_met"  # Decision threshold not reached
    HUMAN_ESCALATION = "human_escalation"  # Quarantine / human review
    QUALITY = "quality"  # Source quality / citation reminders
    STRUCTURE = "structure"  # Graph structure guidance (patterns, clusters)
    CONTRADICTION = "contradiction"  # Contradictions and value conflicts


class SeverityLevel(enum.IntEnum):
    """Nudge severity levels (OHM-769)."""

    SILENT = 0  # Nothing emitted
    CONTEXT = 1  # Appended to context (agent may not notice)
    SOFT = 2  # One sentence in tool response
    FIRM = 3  # Requires explicit acknowledgment


# Default severity per category
_DEFAULT_SEVERITY: dict[NudgeCategory, SeverityLevel] = {
    NudgeCategory.CONTEXT: SeverityLevel.CONTEXT,
    NudgeCategory.CALIBRATION: SeverityLevel.SOFT,
    NudgeCategory.VOI: SeverityLevel.SOFT,
    NudgeCategory.PERT: SeverityLevel.SOFT,
    NudgeCategory.METHOD: SeverityLevel.SOFT,
    NudgeCategory.PLURALISM: SeverityLevel.SOFT,
    NudgeCategory.SYNTHESIS: SeverityLevel.CONTEXT,
    NudgeCategory.AUTONOMY: SeverityLevel.SOFT,
    NudgeCategory.NOVELTY: SeverityLevel.SOFT,
    NudgeCategory.STALE_EVIDENCE: SeverityLevel.CONTEXT,
    NudgeCategory.METHOD_DIVERGENCE: SeverityLevel.SOFT,
    NudgeCategory.SOCRATIC: SeverityLevel.SOFT,
    NudgeCategory.EVIDENCE_RACE: SeverityLevel.FIRM,
    NudgeCategory.THRESHOLD_NOT_MET: SeverityLevel.SOFT,
    NudgeCategory.HUMAN_ESCALATION: SeverityLevel.FIRM,
    NudgeCategory.QUALITY: SeverityLevel.CONTEXT,
    NudgeCategory.STRUCTURE: SeverityLevel.CONTEXT,
    NudgeCategory.CONTRADICTION: SeverityLevel.FIRM,
}


# Mapping from existing 19 nudge type strings to categories.
# This is the reconciliation table the #769 comment asked for.
NUDGE_TYPE_MAP: dict[str, NudgeCategory] = {
    # Existing types → category
    "babel_insight": NudgeCategory.STRUCTURE,
    "batch_suggestion": NudgeCategory.SYNTHESIS,
    "causal_edge_confirmed": NudgeCategory.STRUCTURE,
    "causal_edge_missing_mechanism": NudgeCategory.STRUCTURE,
    "causal_edge_suggestion": NudgeCategory.STRUCTURE,
    "challenge_reminder": NudgeCategory.PLURALISM,
    "cluster_synthesis": NudgeCategory.SYNTHESIS,
    "confidence_outlier": NudgeCategory.CALIBRATION,
    "contradiction_alert": NudgeCategory.CONTRADICTION,
    "decision_node_suggestion": NudgeCategory.VOI,
    "fast_decaying_observation": NudgeCategory.STALE_EVIDENCE,
    "high_confidence_weak_source": NudgeCategory.QUALITY,
    "inference_delta": NudgeCategory.VOI,
    "mechanism_gate": NudgeCategory.STRUCTURE,
    "pattern_detection": NudgeCategory.STRUCTURE,
    "pattern_to_causal_warning": NudgeCategory.STRUCTURE,
    "pert_estimation": NudgeCategory.PERT,
    "semantic_edge_warning": NudgeCategory.CONTRADICTION,
    "source_citation": NudgeCategory.QUALITY,
    "value_contradiction": NudgeCategory.CONTRADICTION,
    # New types from the agora/deliberation design
    "belief_statement_suggestion": NudgeCategory.CALIBRATION,
    "autonomy_nudge": NudgeCategory.AUTONOMY,
    "autonomy": NudgeCategory.AUTONOMY,
    "novelty_nudge": NudgeCategory.NOVELTY,
    "method_divergence": NudgeCategory.METHOD_DIVERGENCE,
    "socratic_question": NudgeCategory.SOCRATIC,
    "socratic_falsifiability": NudgeCategory.SOCRATIC,
    "socratic_steel_man": NudgeCategory.SOCRATIC,
    "evidence_race": NudgeCategory.EVIDENCE_RACE,
    "threshold_not_met": NudgeCategory.THRESHOLD_NOT_MET,
    "human_escalation": NudgeCategory.HUMAN_ESCALATION,
    "loop_detected": NudgeCategory.AUTONOMY,
    "dissent_rewarded": NudgeCategory.NOVELTY,
}


def classify_nudge(nudge: dict[str, Any]) -> tuple[NudgeCategory, SeverityLevel]:
    """Classify a nudge dict into (category, severity).

    If the nudge has an explicit ``category`` field, use it.
    Otherwise, look up the ``type`` in NUDGE_TYPE_MAP.
    Severity defaults from _DEFAULT_SEVERITY unless the nudge has an
    explicit ``severity`` field (string → enum).
    """
    # Category
    if "category" in nudge:
        try:
            category = NudgeCategory(nudge["category"])
        except ValueError:
            category = NudgeCategory.STRUCTURE
    else:
        nudge_type = nudge.get("type", "")
        category = NUDGE_TYPE_MAP.get(nudge_type, NudgeCategory.STRUCTURE)

    # Severity
    if "severity" in nudge:
        sev = nudge["severity"]
        if isinstance(sev, int):
            severity = SeverityLevel(min(max(sev, 0), 3))
        elif isinstance(sev, str):
            severity_map = {
                "hint": SeverityLevel.CONTEXT,
                "soft": SeverityLevel.SOFT,
                "firm": SeverityLevel.FIRM,
                "silent": SeverityLevel.SILENT,
                "context": SeverityLevel.CONTEXT,
            }
            severity = severity_map.get(sev.lower(), _DEFAULT_SEVERITY[category])
        else:
            severity = _DEFAULT_SEVERITY[category]
    else:
        severity = _DEFAULT_SEVERITY[category]

    return category, severity


def compute_severity(
    base_severity: SeverityLevel,
    divergence: float | None = None,
    blast_radius: str | None = None,
    calibration_error: float | None = None,
) -> SeverityLevel:
    """Adjust severity based on contextual factors (OHM-769).

    Escalates severity when:
    - divergence is high (|claimed - graph| >= 0.35 → at least FIRM)
    - blast_radius is "high" → escalate by one level
    - calibration_error is high (agent historically over/under-confident)
    """
    severity = base_severity

    if divergence is not None:
        if divergence >= 0.35:
            severity = max(severity, SeverityLevel.FIRM)
        elif divergence >= 0.25:
            severity = max(severity, SeverityLevel.SOFT)

    if blast_radius == "high":
        severity = SeverityLevel(min(int(severity) + 1, 3))

    if calibration_error is not None and calibration_error >= 0.25:
        severity = SeverityLevel(min(int(severity) + 1, 3))

    return severity


@dataclass
class NudgeThrottle:
    """Per-target, per-type nudge throttle (OHM-769).

    Prevents the same nudge type for the same target from firing
    more than once per ``min_turns`` turns, unless the belief has
    moved by > ``belief_movement_threshold`` since the last nudge.
    """

    min_turns: int = 5
    belief_movement_threshold: float = 0.15
    max_history: int = 50
    _history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def _key(self, target: str, nudge_type: str) -> str:
        return f"{target}:{nudge_type}"

    def should_emit(
        self,
        target: str,
        nudge_type: str,
        current_belief: float | None = None,
        turn: int = 0,
    ) -> bool:
        """Return True if the nudge should be emitted (not throttled)."""
        key = self._key(target, nudge_type)
        history = self._history.get(key, [])

        if not history:
            return True

        last = history[-1]
        turns_since = turn - last.get("turn", 0)

        # If belief moved significantly, allow regardless of turn count
        if current_belief is not None and "belief" in last:
            movement = abs(current_belief - last["belief"])
            if movement > self.belief_movement_threshold:
                return True

        # Otherwise, throttle to once per min_turns
        return turns_since >= self.min_turns

    def record(
        self,
        target: str,
        nudge_type: str,
        belief: float | None = None,
        turn: int = 0,
    ) -> None:
        """Record that a nudge was emitted."""
        key = self._key(target, nudge_type)
        history = self._history.setdefault(key, [])
        history.append({"turn": turn, "belief": belief, "ts": time.monotonic()})
        # Bound history size
        if len(history) > self.max_history:
            self._history[key] = history[-self.max_history :]

    def reset(self) -> None:
        """Clear all throttle state (for testing)."""
        self._history.clear()
