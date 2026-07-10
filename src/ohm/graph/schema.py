"""OHM database schema — DDL statements and schema management.

Tables:
    - ohm_nodes: Graph nodes (ideas, sources, people, concepts, etc.)
    - ohm_edges: Directed edges between nodes with layer/type/confidence
    - ohm_observations: Observations attached to nodes or edges
    - ohm_agent_state: Per-agent focus, values, goals, and configuration
    - ohm_change_feed: Append-only log of graph mutations
    - ohm_snapshots: Named snapshots for time-travel queries

Layer model (L1-L4):
    L1: Structure — Fully shared, all agents read/write
    L2: Flow — Shared with attribution
    L3: Knowledge — Agent-owned, challengeable
    L4: Prospect — Agent-owned, visible
    Private: Agent-only, not shared

Schema customization:
    Use SchemaConfig to create domain-specific configurations (e.g., TOPO
    for industrial knowledge graphs). The default config provides the base
    OHM schema; domain configs extend or override it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from types import MappingProxyType

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = logging.getLogger(__name__)

# ── Node Types ──────────────────────────────────────────────────────────────

VALID_NODE_TYPES = frozenset(
    {
        "idea",
        "source",
        "person",
        "concept",
        "pattern",
        "event",
        "institution",
        "technology",
        "equipment",
        "system",
        "infrastructure",  # Physical/virtual hosts, platforms (OHM-infra)
        "service",  # Running software, daemons, APIs (OHM-infra)
        "release",  # Software versions/deployments (OHM-infra)
        "area",
        "site",
        "agent",
        "skill",
        "value",
        "goal",
        "topic",
        "task",  # Action items with status, priority, assignment
        "decision",  # Decision nodes with utility function (OHM-6mv.2)
        "fragment",  # L0 thinking fragments (OHM-a5rz.2)
        "hypothesis",  # Testable research claim (OHM-ss22)
        "experiment",  # Bounded evaluation with artifact + metrics (OHM-ss22)
        # ── Feedback-graph types (OHM-iuoz) ──
        "scenario",  # A counterfactual scenario — "what if X were 0.3?"
        "action",  # A proposed or executed action — "increase supplier B reliability"
        "intervention",  # A node-level do-operator — "force node Y to state Z"
        # ── Digital twin type (OHM-8dg4) ──
        "twin",  # A digital twin of an external system — registered via snap-in contract
        # ── Twin template type (OHM-hl61) ──
        "twin_template",  # Reusable primitive for agents to assemble twins
        # ── Model marketplace types (OHM-75tw) ──
        "model_candidate",  # A registered predictive model competing for a twin
        "model_evaluation",  # A single evaluation result for a model candidate
        # ── Operational twin model types (OHM-bf45) ──
        "drift_event",  # A detected drift incident (residual/feature/concept/ensemble)
        "validation_run",  # A walk-forward / replay validation of a model
        "ensemble_vote",  # A recorded ensemble decision across model candidates
        "freshness_threshold",  # Per-decision freshness constraint (OHM-2x2u)
        "feed_investment",  # VoI-driven observation investment plan (OHM-2x2u)
        "mode_switch",  # Real-time/deliberative mode transition record (OHM-2x2u)
        # ── Twin design session types (OHM-konq) ──
        "twin_design_session",  # A conversational twin design session
        "twin_design_proposal",  # A specific twin configuration proposal
        # ── Open Skills portable skill contract (OHM-461f) ──
        "runbook",  # Ordered DEPENDS_ON chain of skill nodes
    }
)

# ── Analysis guide for /schema endpoints ───────────────────────────────────
# Quick guidance for agents and clients: what each node type is good for,
# which evidence it needs, whether it supports causal interventions, and
# provenance rules to keep in mind.
ANALYSIS_GUIDE: dict[str, dict[str, object]] = {
    "source": {
        "use_for": ["raw evidence", "outcome tracking", "reliability scoring"],
        "supports_inference": False,
        "required_evidence": ["source_url"],
        "provenance_rules": ["source_url is required (ADR-013)", "reliability is tracked via recorded outcomes"],
    },
    "concept": {
        "use_for": ["definitions", "shared vocabulary", "L2 structure"],
        "supports_inference": True,
        "required_evidence": [],
        "provenance_rules": ["link to sources via REFERENCES edges", "keep definitions narrow enough to challenge"],
    },
    "pattern": {
        "use_for": ["syntheses", "cross-domain abstractions", "Bayesian targets"],
        "supports_inference": True,
        "required_evidence": ["supporting edges", "confidence >= 0.8 for L3"],
        "provenance_rules": ["must be challengeable", "prefer CAUSES/ENABLES/DEPENDS_ON edges for inference"],
    },
    "event": {
        "use_for": ["dated facts", "turning points", "trigger nodes"],
        "supports_inference": True,
        "required_evidence": [],
        "provenance_rules": ["include date/timestamp in metadata when possible", "link to decisions it triggers"],
    },
    "decision": {
        "use_for": ["action selection", "utility optimization", "policy nodes"],
        "supports_inference": True,
        "required_evidence": ["utility_scale", "action_alternatives"],
        "provenance_rules": ["utility_scale and action_alternatives enable /nash and /policy", "update current_best_action when evidence shifts"],
    },
    "task": {
        "use_for": ["work tracking", "status transitions", "agent assignments"],
        "supports_inference": False,
        "required_evidence": ["status", "assigned_to"],
        "provenance_rules": ["use status lifecycle: open -> in_progress -> review -> done", "link to runbooks/skills for execution context"],
    },
    "hypothesis": {
        "use_for": ["testable claims", "experiment targets"],
        "supports_inference": True,
        "required_evidence": ["falsifiability criteria"],
        "provenance_rules": ["link to at least one experiment or evidence node", "update confidence as outcomes arrive"],
    },
    "experiment": {
        "use_for": ["bounded evaluations", "metric collection"],
        "supports_inference": False,
        "required_evidence": ["metrics", "artifact reference"],
        "provenance_rules": ["link to the hypothesis being tested", "record outcome to update source reliability"],
    },
    "fragment": {
        "use_for": ["L0 hunches", "raw agent thinking", "questions"],
        "supports_inference": False,
        "required_evidence": [],
        "provenance_rules": ["excluded from search/stats/neighborhood by default", "promote to a typed node when confidence >= 0.8"],
    },
    "agent": {
        "use_for": ["actor metadata", "services offered", "focus areas"],
        "supports_inference": False,
        "required_evidence": [],
        "provenance_rules": ["keep services and focus_areas current", "use for A2A routing and trust scoring"],
    },
    "skill": {
        "use_for": ["portable capability", "Open Skills contracts"],
        "supports_inference": False,
        "required_evidence": ["trigger", "output_format"],
        "provenance_rules": ["declare required_tools and boundaries", "link to runbooks that invoke the skill"],
    },
    "runbook": {
        "use_for": ["ordered skill chains", "reusable procedures"],
        "supports_inference": False,
        "required_evidence": ["DEPENDS_ON chain of skill nodes"],
        "provenance_rules": ["order matters: edges are DEPENDS_ON", "version for breaking changes"],
    },
    "twin": {
        "use_for": ["digital twins", "model candidates", "drift monitoring"],
        "supports_inference": True,
        "required_evidence": ["snap-in contract", "registered model candidates"],
        "provenance_rules": ["model candidates compete via model_evaluation nodes", "drift_events should reference this twin"],
    },
    "scenario": {
        "use_for": ["counterfactuals", "what-if reasoning"],
        "supports_inference": True,
        "required_evidence": ["target node and assumed state"],
        "provenance_rules": ["link to the node being varied", "actions are proposed in response"],
    },
    "action": {
        "use_for": ["proposed or executed interventions"],
        "supports_inference": True,
        "required_evidence": ["linked scenario or decision"],
        "provenance_rules": ["record whether executed", "update outcome for causal learning"],
    },
}

# Generic guidance for types that don't have a dedicated entry above.
_DEFAULT_ANALYSIS = {
    "use_for": ["general knowledge graph node"],
    "supports_inference": False,
    "required_evidence": [],
    "provenance_rules": ["link to related nodes to avoid orphans", "include source_url when representing external information"],
}


def node_analysis(node_type: str) -> dict[str, object]:
    """Return analysis guidance for a node type (generic fallback if unknown)."""
    return ANALYSIS_GUIDE.get(node_type, _DEFAULT_ANALYSIS)


# ── Cross-link requirement (ADR-018 / OHM-tjzh) ──────────────────────────────
# Node types in this set represent derived claims — synthesis, decisions, tasks.
# A bare creation of one of these is a dead-end: it can never be reached from
# context, can never be challenged, and cannot propagate through Bayesian
# inference. The shared graph today has ~21% dead-end nodes (OHM-tjzh).
#
# Agents creating a node of one of these types MUST either:
#   1. include a `connects_to` field referencing an existing node id, OR
#   2. submit at least one edge in the same request body.
#
# This is enforced at the HTTP boundary in `server/handlers/graph.py`.
# Forward-compatible types ("synthesis", "observation", "interpretation",
# "challenge") from the OHM-tjzh spec are included so the policy takes effect
# the moment they are added to VALID_NODE_TYPES.
MUST_HAVE_EDGE_NODE_TYPES: frozenset[str] = frozenset(
    {
        # Active claim types in the current schema
        "pattern",
        "idea",
        "task",
        "decision",
        "hypothesis",  # Dead-end claim unless linked to evidence (OHM-ss22)
        "experiment",  # Dead-end unless linked to a hypothesis or concept (OHM-ss22)
        # Feedback-graph types (OHM-iuoz)
        "scenario",  # Must link to the node it evaluates
        "action",  # Must link to the scenario that proposed it
        "intervention",  # Must link to the node it intervenes on
        "twin",  # Must link to the node/system it models (OHM-8dg4)
        "twin_template",  # Must link to the node/system it templates (OHM-hl61)
        # Model marketplace types (OHM-75tw)
        "model_candidate",  # Must link to the twin it competes for
        "model_evaluation",  # Must link to the model_candidate it evaluates
        # Operational twin model types (OHM-bf45)
        "drift_event",  # Must link to the twin/model it signals
        "validation_run",  # Must link to the model_candidate it validates
        "ensemble_vote",  # Must link to the twin it votes on
        # Twin design session types (OHM-konq)
        "twin_design_session",  # Must link to goal/context nodes
        "twin_design_proposal",  # Must link to the session that proposed it
        # Open Skills (OHM-461f)
        "runbook",  # Must link to at least one skill node it chains
        # Forward-compat (per OHM-tjzh spec)
        "synthesis",
        "observation",
        "interpretation",
        "challenge",
    }
)

# Node types that are allowed to exist as bare stubs. The spec (OHM-tjzh)
# lists `source`, `concept`, `entity` as exempt — they are foundational or
# external references that legitimately stand alone until linked.
EXEMPT_CROSS_LINK_NODE_TYPES: frozenset[str] = frozenset({"source", "concept", "entity", "fragment", "infrastructure", "service", "release"})

VALID_VISIBILITIES = frozenset({"private", "team", "public", "vault"})

# ── AND-gate governance (OHM-as17) ──────────────────────────────────────────
# A gate_type of 'AND' means all incoming edges must hold for the node's
# claim to be valid. 'OR' means any one suffices. NULL (the default) means
# the node is not a gate. gate_status tracks whether the gate has been
# converted (AND->OR) or compromised.
VALID_GATE_TYPES = frozenset({"AND", "OR"})
VALID_GATE_STATUSES = frozenset(
    {
        "intact",  # Gate is functioning as designed
        "converted",  # AND-gate has been converted to OR-gate (strategic shift)
        "compromised",  # One or more inputs have failed but gate hasn't fully collapsed
        "failed",  # Gate has collapsed — all inputs lost
        # OHM-8dg4 reconciliation: Metis design-note aliases
        "open",  # Alias for 'intact' — gate is open and processing
        "closed",  # Alias for 'converted' — gate has been deliberately closed
        "stuck",  # Alias for 'compromised' — gate is stuck waiting for input
    }
)

VALID_PROVENANCES = frozenset(
    {
        "conversation",
        "research",
        "bookmark",
        "observation",
        "feed-ingest",
        "healthcheck",  # Automated infrastructure health checks (OHM-infra)
        "metis-research",
        "metis-synthesis",
        "metis-review-request",
        "socrates-curriculum",
        "clio-research",
    }
)

VALID_HD_DIMENSIONS = frozenset({10000})

# ── Edge Types by Layer ─────────────────────────────────────────────────────

LAYER_EDGE_TYPES: dict[str, frozenset[str]] = {
    "L0": frozenset({"CONTEXT_OF", "INSPIRED_BY", "CONTRADICTS_FRAG", "REFINES_FRAG", "RESONANCE"}),
    "L1": frozenset({"CONTAINS", "BELONGS_TO", "HAS_COMPONENT", "PART_OF", "CAPABLE_OF", "VALUES", "GOALS", "INTERESTED_IN", "RUNS_ON", "HOSTS", "VERSION_OF", "LOCATED_IN"}),
    "L2": frozenset(
        {
            "DERIVES_FROM",
            "INFLUENCES",
            "REFERENCES",
            "USES",
            "FEEDS",
            "FLOWS_TO",
            "NOTIFIES",
            "TRUSTS",
            "SERVES",
            # ── Multi-scenario additions (OHM-af8.6) ──
            "BATCH_EXPIRES_BEFORE",  # retail inventory expiry
            "TRANSFERRED_TO",  # customer support handoff
            "OPENED_BY",
            "STARTED_BY",
            "AWAITING",
            "RESOLVED_BY",
            "CLOSED_BY",  # support state machine
            "INVESTIGATED_BY",
            "CONTAINED_BY",
            "ERADICATED_BY",
            "RECOVERED_BY",  # incident state machine
            "NEGOTIATES_WITH",  # SLAs, commitments
            "UPSTREAM_OF",  # Infrastructure: service dependency (OHM-infra)
            # ── BOS ODPS data product provenance (ADR-027 / OHM-ovwq) ──
            "PRODUCES",  # producer agent → data product node
            "CONSUMES",  # consumer agent → data product node
        }
    ),
    "L3": frozenset(
        {
            "CAUSES",
            "CORRELATES_WITH",
            "PREDICTS",
            "EXPLAINS",
            "CHALLENGED_BY",
            "SUPPORTS",
            "REFINES",
            "CONTRADICTS",
            "LISTENS_TO",
            "DEFERS_TO",
            "COLLABORATES_WITH",
            "APPLIES_TO",
            "RELATED_TO",
            # ── Multi-scenario additions (OHM-af8.6) ──
            "NEGATES",  # medical: rules-out diagnosis
            "EXPECTED_LIKELIHOOD",  # supply chain: probability claim
            "ESCALATED_TO",  # support: escalation path
            "DELEGATED_TO",  # support: delegation
            "THREAT_CLUSTER",  # cybersecurity: IOC linkage
            "TRANSITIONS_TO",  # Markov: state transition edge (OHM-g09)
            # ── Hypothesis-tree primitives (OHM-ss22) ──
            "TESTS",  # experiment → hypothesis
            "SUPPORTS_EVIDENCE",  # experiment → node/edge
            "CONTRADICTS_EVIDENCE",  # experiment → node/edge
            # ── Decision nodes (OHM-6mv.2 + OHM-decision) ──
            "DECISION_DEPENDS_ON",  # decision → hypothesis/concept
            # ── Feedback-graph edges (OHM-iuoz) ──
            "COUNTERFACTUAL_OF",  # scenario → original node (this scenario is a counterfactual of)
            "PROPOSES_ACTION",  # scenario → action (this scenario suggests this action)
            "EVALUATES",  # scenario → node (this scenario evaluates this node)
            # ── Model marketplace edges (OHM-75tw) ──
            "COMPETES_WITH",  # model_candidate → model_candidate (competition)
            "EVALUATED_BY",  # model_candidate → model_evaluation (evaluation result)
            # ── Operational twin model edges (OHM-bf45) ──
            "SHADOWS",  # shadow model → active model (divergence detection)
            "DRIFT_SIGNAL",  # twin → drift_event (drift detected)
            # ── Temporal decision layer (OHM-2x2u) ──
            "GOVERNS_FRESHNESS",  # freshness_threshold → decision (freshness constraint)
            "INVESTS_IN",  # feed_investment → decision (VoI-driven observation plan)
            # ── Twin design session edges (OHM-konq) ──
            "PROPOSES",  # session → proposal (session proposes a twin config)
            "APPROVES",  # user → proposal (user approves a proposal)
            "DECLINES",  # user → proposal (user declines a proposal)
            "MODIFIES",  # user → proposal (user requests modifications)
            "INSTANTIATED_FROM",  # twin → session (twin was designed by this session)
            "NUDGES_FOR_VERIFICATION",  # nudge task → claim node (prompt agent to verify)
        }
    ),
    "L4": frozenset(
        {
            "EXPECTS",
            "PLANS",
            "RISKS",
            "DEPENDS_ON",
            "THREATENS",
            "ENABLES",
            "EXPECTS_FROM",
            "PREDICTS",
            # ── Multi-scenario additions (OHM-af8.6) ──
            "ORDERS_TEST",  # medical: trigger diagnostic test
            "TRIGGERS_INCIDENT",  # cybersecurity: finding triggers incident
            # ── Task management additions ──
            "BLOCKS",  # task blocks another task
            # ── Feedback-graph edges (OHM-iuoz) ──
            "PROPOSED_BY",  # action → scenario/agent (who proposed this action)
            "EXECUTED_BY",  # action → agent (who executed this action)
            "FEEDBACK_TO",  # action/observation → scenario (result feeds back to scenario)
            "INTERVENES_ON",  # intervention → node (this intervention targets this node)
        }
    ),
}

ALL_EDGE_TYPES: frozenset[str] = frozenset().union(*LAYER_EDGE_TYPES.values())

VALID_LAYERS = frozenset(LAYER_EDGE_TYPES.keys())

# ── Observation Types ───────────────────────────────────────────────────────

VALID_OBSERVATION_TYPES = frozenset(
    {
        "anomaly",
        "measurement",
        "pattern",
        "challenge",
        "support",
        "sentiment",  # customer support: sentiment observation
        "health_check",  # Infrastructure health/status (OHM-infra)
        "experiment_result",  # Experiment measurement with dev/test metrics (OHM-ss22)
        "assessment",  # Agent-evaluated judgment without raw measurement (OHM-36ps)
    }
)

VALID_OBSERVATION_SOURCES = frozenset(
    {
        "signal",
        "research",
        "conversation",
        "analysis",
    }
)

VALID_OBSERVATION_SCALES = frozenset(
    {
        "probability",
        "count",
        "currency",
        "percent",
        "unknown",
        "binary",  # ADR-025: alias for probability with value 0/1
    }
)

# ── ADR-026: Myth Compression Framework ──────────────────────────────────────

VALID_COMPRESSION_TYPES = frozenset(
    {
        "inversion",  # Lossless: AND→OR bypass, destroys necessity
        "normative_inversion",  # Lossless: AND→OR negation, destroys visibility
        "retrojection",  # Lossy: OR→AND compression, destroys accuracy
        "composite",  # Multiple operations on same target
    }
)

# Compression degree ranges:
#   0.0-0.3  Elaboration (high info preservation)
#   0.3-0.6  Compression (medium)
#   0.6-0.8  Aggressive compression (low)
#   0.8-1.0  Fabrication (none)
#
# Revisability ranges:
#   0.0-0.3  Revisable (can challenge with evidence)
#   0.3-0.6  Sticky (challenge with counter-narrative)
#   0.6-0.8  Infrastructure (identity-constituting, challenge = attack)
#   0.8-1.0  Sacred (blasphemy to challenge, violence risk)

# ── ADR-022: Layer Promotion Constraints ────────────────────────────────────

# Export the constraint dictionaries for use by other modules.
# Canonical definitions live in ohm.graph.constraints.

# ── Urgency / Priority ──────────────────────────────────────────────────────

VALID_URGENCY = frozenset({"low", "normal", "high", "critical"})

VALID_PRIORITY = frozenset({"P0", "P1", "P2", "P3", "P4"})

# ── Source Tier (ADR-028) ───────────────────────────────────────────────────
# Quality dimension for claims and edges. Bridges ADR-015 (citation_status)
# and ADR-018.3 (verification decay). Each tier caps the confidence that can
# be claimed — a 0.9 confidence from `raw` is rejected.

VALID_SOURCE_TIERS = frozenset(
    {
        "raw",  # Single unverified claim, no corroboration
        "unverified",  # Cited but no independent verification
        "preliminary",  # One independent verification
        "official",  # Institutional / peer-reviewed source
        "verified",  # Multi-source confirmed + outcome recorded
    }
)

SOURCE_TIER_CEILINGS: dict[str, float] = {
    "raw": 0.3,
    "unverified": 0.5,
    "preliminary": 0.7,
    "official": 0.9,
    "verified": 1.0,
}

VALID_DATA_ORIGINS = frozenset(
    {
        "ugc",
        "peer_reviewed",
        "government",
        "news_wire",
        "sensor",
        "agent_synthesis",
        "expert",
        "unknown",
    }
)

VALID_TASK_STATUSES = frozenset(
    {
        "open",  # New task, not yet started
        "in_progress",  # Agent is actively working on it
        "blocked",  # Waiting on dependency or external input
        "review",  # Awaiting review by another agent
        "done",  # Completed
        "cancelled",  # No longer needed
    }
)

# OHM-f5iq: outcome values recorded when a task is closed.
# TRUE = the task's expected_claim was confirmed by the work.
# FALSE = the task's expected_claim was falsified.
# AMBIGUOUS = the work could not determine the claim either way.
VALID_TASK_OUTCOMES = frozenset({"TRUE", "FALSE", "AMBIGUOUS"})

# ── Emerging Concept Detection (OHM-tlqz) ────────────────────────────────────

VALID_EMERGING_CONCEPT_STATUSES = frozenset({"unnamed", "naming_candidate", "named", "rejected"})

EMERGING_CONCEPT_STABILITY_THRESHOLD = 0.45
EMERGING_CONCEPT_MIN_OBSERVATIONS = 3

# ── TELOS Signing (OHM-enwb) ─────────────────────────────────────────────────

VALID_SIGNING_ALGORITHMS = frozenset({"ed25519", "hmac-sha256"})

# ── Suggestions Lifecycle (OHM-xtzk) ────────────────────────────────────────

VALID_SUGGESTION_TYPES = frozenset({"edge", "node_link"})
VALID_SUGGESTION_STATUSES = frozenset({"ripe", "promoted", "expired", "rejected"})
VALID_SUGGESTION_METHODS = frozenset({"semantic", "hd_membership", "orphan", "manual", "oppositional_review"})

# ── Read Scopes (OHM-ybyb) ───────────────────────────────────────────────────

VALID_READ_SCOPE_DIMENSIONS = frozenset({"layer", "source_tier", "node_id", "created_by"})

# ── Layer Descriptions ──────────────────────────────────────────────────────

LAYER_DESCRIPTIONS: dict[str, str] = {
    "L0": "Thinking — Fragments, hunches, raw associations; unreliable, auto-linked",
    "L1": "Structure — Fully shared, all agents read/write",
    "L2": "Flow — Shared with attribution",
    "L3": "Knowledge — Agent-owned, challengeable",
    "L4": "Prospect — Agent-owned, visible",
}

# ── Schema Configuration ────────────────────────────────────────────────────


@dataclass(frozen=True)
class DomainTable:
    """Declarative definition of a domain-specific table (OHM-vl8o).

    OHM's core DDL is fixed (ohm_nodes, ohm_edges, ohm_observations, …).
    Domain templates (e.g. TOPO) need extra tables (topo_prospects,
    topo_observations, topo_observation_assessments, …) created in the
    same migration sequence as the base OHM tables, so they ride along
    with `initialize_schema()` and OhmStore/ohmd without per-domain
    bootstrap code.

    Attributes:
        name: SQL table name. Conventionally domain-prefixed
            (e.g. ``topo_prospects``). Unprefixed names are allowed but
            reserved names (``ohm_*``) are rejected at creation time.
        columns: Ordered list of ``(column_name, sql_type)`` pairs.
            ``sql_type`` is the raw DuckDB type (e.g. ``"VARCHAR"``,
            ``"FLOAT"``, ``"TIMESTAMP DEFAULT CURRENT_TIMESTAMP"``).
        primary_key: Column name to declare as ``PRIMARY KEY``. Omit
            (``None``) to leave DuckDB's row-id as the implicit key.
        indexes: List of ``(index_name, [column_name, ...])`` to create
            after the table. Each becomes
            ``CREATE INDEX IF NOT EXISTS <name> ON <table>(<cols>)``.
        ordering: Migration ordering — lower values run first. Lets
            domain A depend on domain B by ordering the table defs
            appropriately. Defaults to 100 (mid-band) so domain tables
            sit after the base OHM DDL (which has implicit ordering 0).
        initial_data: Optional seed rows. Each entry is a dict mapping
            column name to literal value; inserted via
            ``INSERT INTO <table> (...) VALUES (...)`` on first creation
            only (idempotent: skipped if the table already has rows).
        description: Human-readable description for tooling/docs.
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    primary_key: str | None = None
    indexes: tuple[tuple[str, tuple[str, ...]], ...] = ()
    ordering: int = 100
    initial_data: tuple[dict, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        # Normalize mutable defaults; frozen=True requires object.__setattr__.
        if not self.name or not isinstance(self.name, str):
            raise ValueError("DomainTable.name must be a non-empty string")
        if self.name.lower().startswith("ohm_"):
            raise ValueError(f"DomainTable.name='{self.name}' uses the reserved 'ohm_' prefix; domain tables must not collide with the core OHM tables.")
        # Validate identifier characters (DuckDB: alphanumeric + underscore, must start with letter/_)
        if not (self.name[0].isalpha() or self.name[0] == "_") or not all(c.isalnum() or c == "_" for c in self.name):
            raise ValueError(f"DomainTable.name='{self.name}' is not a valid SQL identifier")
        if not self.columns:
            raise ValueError(f"DomainTable.name='{self.name}' must declare at least one column")
        col_names = {c[0] for c in self.columns}
        if self.primary_key is not None and self.primary_key not in col_names:
            raise ValueError(f"DomainTable.name='{self.name}' primary_key='{self.primary_key}' not found in columns {sorted(col_names)}")
        for idx_name, idx_cols in self.indexes:
            if not idx_name:
                raise ValueError(f"DomainTable.name='{self.name}' has an index with empty name")
            for c in idx_cols:
                if c not in col_names:
                    raise ValueError(f"DomainTable.name='{self.name}' index '{idx_name}' references missing column '{c}'")

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict (for to_dict/from_dict round-trip)."""
        d: dict = {
            "name": self.name,
            "columns": [[c, t] for c, t in self.columns],
            "ordering": self.ordering,
        }
        if self.primary_key is not None:
            d["primary_key"] = self.primary_key
        if self.indexes:
            d["indexes"] = [[n, list(cols)] for n, cols in self.indexes]
        if self.initial_data:
            d["initial_data"] = [dict(r) for r in self.initial_data]
        if self.description:
            d["description"] = self.description
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "DomainTable":
        """Inverse of :meth:`to_dict`."""
        if "name" not in data or "columns" not in data:
            raise ValueError("DomainTable dict requires 'name' and 'columns'")
        return cls(
            name=data["name"],
            columns=tuple((c, t) for c, t in data["columns"]),
            primary_key=data.get("primary_key"),
            indexes=tuple((n, tuple(cols)) for n, cols in data.get("indexes", [])),
            ordering=int(data.get("ordering", 100)),
            initial_data=tuple(dict(r) for r in data.get("initial_data", [])),
            description=data.get("description", ""),
        )


@dataclass(frozen=True)
class DuckLakeTable:
    """Declarative definition of a table to mirror in DuckLake (OHM-8bli).

    OHM's DuckLake sync was previously hardcoded to three tables
    (ohm_nodes, ohm_edges, ohm_observations). Domain templates that
    added their own tables via :class:`DomainTable` (e.g. TOPO's
    topo_prospects) had those tables silently lost on crash/recovery
    because the sync code didn't know about them.

    ``DuckLakeTable`` is the sync-side registry entry: which table to
    mirror, its primary key (for upsert/merge), its timestamp column
    (for incremental sync detection), and a fallback chain when the
    primary timestamp is NULL. Per-table DDL for the mirror (which
    uses all-VARCHAR columns) is derived from the source table's
    ``information_schema.columns`` at sync time.

    Attributes:
        name: Source table name (e.g. ``"ohm_nodes"``,
            ``"topo_prospects"``).
        primary_key: Column used for upsert/merge logic. Defaults to
            ``"id"``. The mirror DDL does NOT declare a PK (mirrors
            are append-only with VARCHAR columns); the primary key is
            used in the WHERE clauses of the sync queries.
        timestamp_col: Column for incremental sync detection (e.g.
            ``"updated_at"``). Rows whose timestamp_col > last_sync
            are pushed. Must exist on the source table; validated at
            sync time.
        timestamp_fallback: Column to use when timestamp_col is NULL
            (typically ``"created_at"``). Required because most OHM
            domain tables only have created_at, not updated_at.
        has_deleted_at: Whether the table has a ``deleted_at`` column
            used for soft-delete tracking. Affects how the sync code
            filters active rows.
        description: Human-readable description.
    """

    name: str
    primary_key: str = "id"
    timestamp_col: str = "updated_at"
    timestamp_fallback: str = "created_at"
    has_deleted_at: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("DuckLakeTable.name must be a non-empty string")
        if not (self.name[0].isalpha() or self.name[0] == "_") or not all(c.isalnum() or c == "_" for c in self.name):
            raise ValueError(f"DuckLakeTable.name='{self.name}' is not a valid SQL identifier")

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict."""
        d: dict = {"name": self.name, "primary_key": self.primary_key}
        if self.timestamp_col != "updated_at":
            d["timestamp_col"] = self.timestamp_col
        if self.timestamp_fallback != "created_at":
            d["timestamp_fallback"] = self.timestamp_fallback
        if not self.has_deleted_at:
            d["has_deleted_at"] = False
        if self.description:
            d["description"] = self.description
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "DuckLakeTable":
        """Inverse of :meth:`to_dict`."""
        if "name" not in data:
            raise ValueError("DuckLakeTable dict requires 'name'")
        return cls(
            name=data["name"],
            primary_key=data.get("primary_key", "id"),
            timestamp_col=data.get("timestamp_col", "updated_at"),
            timestamp_fallback=data.get("timestamp_fallback", "created_at"),
            has_deleted_at=bool(data.get("has_deleted_at", True)),
            description=data.get("description", ""),
        )

    @classmethod
    def from_domain_table(cls, dt: "DomainTable") -> "DuckLakeTable":
        """Derive a DuckLakeTable entry from a :class:`DomainTable`.

        Inspects the column list for ``updated_at``/``created_at`` to
        pick the timestamp_col and fallback. The primary_key is taken
        from ``DomainTable.primary_key`` (defaults to ``"id"``).
        has_deleted_at is True if a ``deleted_at`` column is present.
        """
        col_names = {c[0] for c in dt.columns}
        has_updated = "updated_at" in col_names
        has_created = "created_at" in col_names
        return cls(
            name=dt.name,
            primary_key=dt.primary_key or "id",
            timestamp_col="updated_at" if has_updated else "created_at",
            timestamp_fallback="created_at" if has_created else "created_at",
            has_deleted_at="deleted_at" in col_names,
            description=f"Derived from DomainTable '{dt.name}' (OHM-8bli)",
        )


# Default DuckLake tables — the three core OHM tables plus the change feed
# (which is synced separately but lives in the registry for completeness).
DEFAULT_DUCKLAKE_TABLES: tuple[DuckLakeTable, ...] = (
    DuckLakeTable(
        name="ohm_nodes",
        primary_key="id",
        timestamp_col="updated_at",
        timestamp_fallback="created_at",
        has_deleted_at=True,
        description="Core OHM nodes",
    ),
    DuckLakeTable(
        name="ohm_edges",
        primary_key="id",
        timestamp_col="updated_at",
        timestamp_fallback="created_at",
        has_deleted_at=True,
        description="Core OHM edges",
    ),
    DuckLakeTable(
        name="ohm_observations",
        primary_key="id",
        timestamp_col="created_at",  # No updated_at
        timestamp_fallback="created_at",
        has_deleted_at=True,
        description="Core OHM observations (no updated_at)",
    ),
    DuckLakeTable(
        name="ohm_change_feed",
        primary_key="id",
        timestamp_col="occurred_at",
        timestamp_fallback="occurred_at",
        has_deleted_at=False,
        description="Append-only change feed",
    ),
    DuckLakeTable(
        name="ohm_outcomes",
        primary_key="id",
        timestamp_col="recorded_at",
        timestamp_fallback="recorded_at",
        has_deleted_at=False,
        description="Source reliability outcomes (OHM-knxf: was missing from DuckLake sync, causing data loss on recovery)",
    ),
)


class SchemaConfig:
    """Configurable schema for domain-specific knowledge graphs.

    OHM and TOPO share the same engine but with different node types,
    edge types, and layer descriptions. SchemaConfig allows creating
    domain-specific configurations that extend or override the defaults.

    Usage:
        # Default OHM schema
        config = SchemaConfig()

        # TOPO (industrial) schema
        topo = SchemaConfig.topo()

        # Custom schema
        custom = SchemaConfig(
            name="my-domain",
            node_types=VALID_NODE_TYPES | {"custom_type"},
            layer_edge_types={**LAYER_EDGE_TYPES, "L5": frozenset({"CUSTOM_EDGE"})},
            layer_descriptions={**LAYER_DESCRIPTIONS, "L5": "Custom layer"},
        )
    """

    def __init__(
        self,
        name: str = "ohm",
        node_types: frozenset[str] | None = None,
        edge_types_by_layer: dict[str, frozenset[str]] | None = None,
        layer_descriptions: dict[str, str] | None = None,
        observation_types: frozenset[str] | None = None,
        observation_sources: frozenset[str] | None = None,
        visibilities: frozenset[str] | None = None,
        provenances: frozenset[str] | None = None,
        required_integrations: dict | None = None,
        optional_integrations: dict | None = None,
        template_version: int = 0,
        seed_agents: list[dict] | None = None,
        domain_tables: list[DomainTable] | tuple[DomainTable, ...] | None = None,
        ducklake_tables: list[DuckLakeTable] | tuple[DuckLakeTable, ...] | None = None,
        case_strategy: str = "lowercase",
    ):
        self.name = name
        self.node_types = node_types if node_types is not None else VALID_NODE_TYPES
        self.layer_edge_types = MappingProxyType(edge_types_by_layer if edge_types_by_layer is not None else dict(LAYER_EDGE_TYPES))  # OHM-cyms: immutable
        self.layer_descriptions = MappingProxyType(layer_descriptions if layer_descriptions is not None else dict(LAYER_DESCRIPTIONS))  # OHM-cyms: immutable
        self.observation_types = observation_types if observation_types is not None else VALID_OBSERVATION_TYPES
        self.observation_sources = observation_sources if observation_sources is not None else VALID_OBSERVATION_SOURCES
        self.visibilities = visibilities if visibilities is not None else VALID_VISIBILITIES
        self.provenances = provenances if provenances is not None else VALID_PROVENANCES
        self.required_integrations = MappingProxyType(required_integrations) if required_integrations else MappingProxyType({})  # OHM-cyms: immutable
        self.optional_integrations = MappingProxyType(optional_integrations) if optional_integrations else MappingProxyType({})  # OHM-cyms: immutable
        self.template_version = template_version
        self.seed_agents = seed_agents if seed_agents is not None else []  # OHM-tss4.1.1: domain agent pre-seeding
        # OHM-vl8o: domain-specific tables created alongside the base OHM schema.
        # Stored as a tuple for immutability (frozen DomainTable + tuple container).
        if domain_tables is None:
            self.domain_tables: tuple[DomainTable, ...] = ()
        else:
            # Normalize: validate types, sort by ordering, freeze as tuple.
            for dt in domain_tables:
                if not isinstance(dt, DomainTable):
                    raise TypeError(f"SchemaConfig.domain_tables must contain DomainTable instances, got {type(dt).__name__}")
            self.domain_tables = tuple(sorted(domain_tables, key=lambda d: (d.ordering, d.name)))
        # OHM-8bli: DuckLake sync table registry. Defaults to the four core
        # tables (ohm_nodes, ohm_edges, ohm_observations, ohm_change_feed).
        # Domain tables from self.domain_tables are NOT auto-merged here —
        # callers must explicitly add them via ducklake_tables=[...].
        # This makes the registry explicit: the source of truth is what
        # the operator declares, not what's incidentally in the schema.
        if ducklake_tables is None:
            # Build the default: core tables + auto-derived from domain_tables.
            derived = tuple(DuckLakeTable.from_domain_table(dt) for dt in self.domain_tables)
            self.ducklake_tables: tuple[DuckLakeTable, ...] = DEFAULT_DUCKLAKE_TABLES + derived
        else:
            for dlt in ducklake_tables:
                if not isinstance(dlt, DuckLakeTable):
                    raise TypeError(f"SchemaConfig.ducklake_tables must contain DuckLakeTable instances, got {type(dlt).__name__}")
            self.ducklake_tables = tuple(ducklake_tables)

        # OHM-ue9k: case strategy for node / edge / observation type
        # validation. Default is "lowercase" (canonical OHM convention).
        # Set to "uppercase" to accept the legacy ALL-CAPS form used by
        # TOPO's pre-migration store. "preserve" accepts any case as long
        # as the canonical name appears (case-sensitive match).
        if case_strategy not in ("lowercase", "uppercase", "preserve"):
            raise ValueError(f"case_strategy must be 'lowercase', 'uppercase', or 'preserve', got {case_strategy!r}")
        self.case_strategy = case_strategy

    @property
    def all_edge_types(self) -> frozenset[str]:
        """All edge types across all layers (cached, OHM-x3ar)."""
        if not hasattr(self, "_all_edge_types_cache"):
            self._all_edge_types_cache = frozenset().union(*self.layer_edge_types.values())
        return self._all_edge_types_cache

    @property
    def valid_layers(self) -> frozenset[str]:
        """All valid layer identifiers."""
        return frozenset(self.layer_edge_types.keys())

    @property
    def must_have_edge_node_types(self) -> frozenset[str]:
        """Node types that must have at least one edge when created."""
        return MUST_HAVE_EDGE_NODE_TYPES

    @property
    def exempt_cross_link_node_types(self) -> frozenset[str]:
        """Node types exempt from the cross-link requirement."""
        return EXEMPT_CROSS_LINK_NODE_TYPES

    def validate_node_type(self, node_type: str) -> bool:
        """Check that *node_type* is valid for this schema.

        The case-insensitive comparison respects ``self.case_strategy``:
        - "lowercase" (default): accepts lowercase form of canonical types.
        - "uppercase": accepts UPPERCASE form (legacy TOPO migration).
        - "preserve": accepts the canonical name case-sensitively.
        """
        if not node_type:
            return False
        if self.case_strategy == "preserve":
            return node_type in self.node_types
        # lowercase or uppercase: normalize to canonical
        normalized = self.normalize_node_type(node_type)
        if normalized is None:
            return False
        return normalized in self.node_types

    def normalize_node_type(self, node_type: str) -> str | None:
        """Convert a node_type string to its canonical form (OHM-ue9k).

        Returns the canonical (lowercase) name if the input matches a
        known type under the current case_strategy, else None.

        Strategy matrix:
        - "lowercase" (default): input must already be lowercase, return as-is
          if in node_types, else None.
        - "uppercase": input may be UPPERCASE. Lower it and check membership.
        - "preserve": same as "lowercase" (case-sensitive canonical only).
        """
        if not node_type:
            return None
        if self.case_strategy == "uppercase":
            # Accept the legacy UPPERCASE form. If the lowercased input
            # matches a canonical name, return that canonical name.
            lower = node_type.lower()
            if lower in self.node_types:
                return lower
            return None
        # "lowercase" or "preserve": case-sensitive match against canonical
        return node_type if node_type in self.node_types else None

    def validate_edge_type(self, layer: str, edge_type: str) -> bool:
        """Check that *edge_type* is valid for the given *layer*."""
        allowed = self.layer_edge_types.get(layer)
        if allowed is None:
            return False
        return edge_type in allowed

    def validate_layer(self, layer: str) -> bool:
        """Check that *layer* is a valid layer identifier."""
        return layer in self.layer_edge_types

    @classmethod
    def topo(cls) -> "SchemaConfig":
        """Create a TOPO (industrial knowledge graph) schema configuration.

        Extends the base OHM schema with industrial-specific types:
        - Additional node types: equipment, system, area, site (already in base)
        - Additional edge types: FEEDS, FLOWS_TO, DEPENDS_ON (already in base)
        - Custom layer descriptions for industrial context
        - Additional observation types for industrial monitoring
        - METRIC, DATA_PRODUCT, COMPONENT, OTHER (OHM-ue9k — TOPO's
          existing schema uses these node types for the data product
          catalog. Without them, the migration from the legacy
          TOPO store cannot create valid OHM nodes.)

        The canonical type names are lowercase. With
        ``case_strategy="uppercase"``, the schema also accepts the
        legacy ALL-CAPS form (e.g. ``METRIC`` validates as ``metric``)
        so existing TOPO data can be ingested without a destructive
        rename. See OHM-ue9k for the migration recipe.
        """
        topo_node_types = VALID_NODE_TYPES | frozenset(
            {
                "process",
                "instrument",
                "controller",
                "valve",
                "pump",
                "motor",
                "sensor",
                "pipeline",
                "vessel",
                "reactor",
                "heat_exchanger",
                "tank",
                "compressor",
                "generator",
                "transformer",
                "circuit",
                "bus",
                "line",
                # OHM-ue9k: TOPO data product catalog node types
                "metric",
                "data_product",
                "component",
                "other",
            }
        )

        topo_layer_descriptions = {
            "L1": "Structure — Physical hierarchy (site → area → system → equipment)",
            "L2": "Flow — Process flows, material/energy/information paths",
            "L3": "Knowledge — Operational insights, failure modes, best practices",
            "L4": "Prospect — Predictive maintenance, risk assessments, what-if scenarios",
        }

        topo_observation_types = VALID_OBSERVATION_TYPES | frozenset(
            {
                "vibration",
                "temperature",
                "pressure",
                "flow_rate",
                "voltage",
                "current",
                "rpm",
                "level",
            }
        )

        topo_observation_sources = VALID_OBSERVATION_SOURCES | frozenset(
            {
                "scada",
                "dcs",
                "historian",
                "maintenance_log",
            }
        )

        topo_provenances = VALID_PROVENANCES | frozenset(
            {
                "inspection",
                "monitoring",
                "audit",
                "simulation",
            }
        )

        topo_domain_tables: list[DomainTable] = [
            DomainTable(
                name="topo_prospects",
                columns=(
                    ("id", "VARCHAR"),
                    ("equipment_id", "VARCHAR"),
                    ("site_id", "VARCHAR"),
                    ("rul_days", "FLOAT"),
                    ("risk_class", "VARCHAR"),
                    ("model_version", "VARCHAR"),
                    ("created_by", "VARCHAR NOT NULL"),
                    ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("metadata", "JSON"),
                ),
                primary_key="id",
                indexes=(
                    ("idx_topo_prospects_equipment", ("equipment_id",)),
                    ("idx_topo_prospects_site", ("site_id",)),
                    ("idx_topo_prospects_risk", ("risk_class",)),
                ),
                ordering=100,
                description="TOPO predictive-maintenance prospects: ranked equipment with RUL and risk class.",
            ),
            DomainTable(
                name="topo_observations",
                columns=(
                    ("id", "VARCHAR"),
                    ("node_id", "VARCHAR"),
                    ("obs_type", "VARCHAR"),
                    ("obs_value", "FLOAT"),
                    ("obs_unit", "VARCHAR"),
                    ("source", "VARCHAR"),
                    ("observed_at", "TIMESTAMP"),
                    ("created_by", "VARCHAR NOT NULL"),
                    ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("metadata", "JSON"),
                ),
                primary_key="id",
                indexes=(
                    ("idx_topo_obs_node", ("node_id",)),
                    ("idx_topo_obs_type", ("obs_type",)),
                    ("idx_topo_obs_time", ("observed_at",)),
                ),
                ordering=110,
                description="TOPO observation records: sensor readings, anomalies, and measurements linked to OHM nodes.",
            ),
            DomainTable(
                name="topo_observation_assessments",
                columns=(
                    ("id", "VARCHAR"),
                    ("observation_id", "VARCHAR"),
                    ("assessment_type", "VARCHAR"),
                    ("assessment_value", "VARCHAR"),
                    ("is_current", "BOOLEAN DEFAULT TRUE"),
                    ("assessed_by", "VARCHAR NOT NULL"),
                    ("assessed_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("notes", "TEXT"),
                    ("metadata", "JSON"),
                ),
                primary_key="id",
                indexes=(
                    ("idx_topo_asmt_obs", ("observation_id",)),
                    ("idx_topo_asmt_current", ("is_current",)),
                ),
                ordering=120,
                description="Append-only assessment history for TOPO observations with is_current flags.",
            ),
            DomainTable(
                name="topo_observation_annotations",
                columns=(
                    ("id", "VARCHAR"),
                    ("observation_id", "VARCHAR"),
                    ("annotation_type", "VARCHAR"),
                    ("annotation_value", "TEXT"),
                    ("annotated_by", "VARCHAR NOT NULL"),
                    ("annotated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("metadata", "JSON"),
                ),
                primary_key="id",
                indexes=(("idx_topo_anno_obs", ("observation_id",)),),
                ordering=130,
                description="Annotations (comments, tags, context) on TOPO observations.",
            ),
            DomainTable(
                name="topo_observation_followups",
                columns=(
                    ("id", "VARCHAR"),
                    ("observation_id", "VARCHAR"),
                    ("followup_type", "VARCHAR"),
                    ("status", "VARCHAR DEFAULT 'open'"),
                    ("assigned_to", "VARCHAR"),
                    ("due_date", "TIMESTAMP"),
                    ("closed_at", "TIMESTAMP"),
                    ("created_by", "VARCHAR NOT NULL"),
                    ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("metadata", "JSON"),
                ),
                primary_key="id",
                indexes=(
                    ("idx_topo_fup_obs", ("observation_id",)),
                    ("idx_topo_fup_status", ("status",)),
                    ("idx_topo_fup_assignee", ("assigned_to",)),
                ),
                ordering=140,
                description="Followup tracking for TOPO observations: actions, investigations, monitoring.",
            ),
            DomainTable(
                name="topo_plans",
                columns=(
                    ("id", "VARCHAR"),
                    ("node_id", "VARCHAR"),
                    ("plan_type", "VARCHAR"),
                    ("label", "VARCHAR"),
                    ("start_ts", "TIMESTAMP"),
                    ("end_ts", "TIMESTAMP"),
                    ("horizon", "VARCHAR"),
                    ("status", "VARCHAR DEFAULT 'active'"),
                    ("created_by", "VARCHAR NOT NULL"),
                    ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("metadata", "JSON"),
                ),
                primary_key="id",
                indexes=(
                    ("idx_topo_plans_node", ("node_id",)),
                    ("idx_topo_plans_type", ("plan_type",)),
                    ("idx_topo_plans_window", ("start_ts", "end_ts")),
                    ("idx_topo_plans_status", ("status",)),
                    ("idx_topo_plans_horizon", ("horizon",)),
                ),
                ordering=200,
                description="TOPO maintenance plans: time-bounded groupings of events (e.g., 4-day maintenance window, annual outage).",
            ),
            DomainTable(
                name="topo_events",
                columns=(
                    ("id", "VARCHAR"),
                    ("plan_id", "VARCHAR"),
                    ("node_id", "VARCHAR"),
                    ("node_path", "VARCHAR"),
                    ("event_class", "VARCHAR"),
                    ("title", "VARCHAR"),
                    ("start_ts", "TIMESTAMP"),
                    ("end_ts", "TIMESTAMP"),
                    ("horizon", "VARCHAR"),
                    ("operating_state", "VARCHAR"),
                    ("description", "TEXT"),
                    ("source_refs", "JSON"),
                    ("l3_context", "JSON"),
                    ("flow_impact", "JSON"),
                    ("forecast_basis", "JSON"),
                    ("decision_metadata", "JSON"),
                    ("confidence", "DOUBLE"),
                    ("authority", "VARCHAR"),
                    ("revision", "INTEGER DEFAULT 1"),
                    ("created_by", "VARCHAR NOT NULL"),
                    ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("metadata", "JSON"),
                ),
                primary_key="id",
                indexes=(
                    ("idx_topo_events_plan", ("plan_id",)),
                    ("idx_topo_events_node", ("node_id",)),
                    ("idx_topo_events_path_class", ("node_path", "event_class", "start_ts")),
                    ("idx_topo_events_path_window", ("node_path", "start_ts", "end_ts")),
                    ("idx_topo_events_horizon", ("plan_id", "horizon", "event_class")),
                    ("idx_topo_events_state", ("operating_state", "start_ts")),
                ),
                ordering=210,
                description="TOPO temporal events: discrete occurrences within a plan (e.g., shutdown, restart, inspection, outage).",
            ),
            DomainTable(
                name="topo_event_links",
                columns=(
                    ("id", "VARCHAR"),
                    ("from_event_id", "VARCHAR"),
                    ("to_event_id", "VARCHAR"),
                    ("edge_type", "VARCHAR"),
                    ("layer", "VARCHAR DEFAULT 'L1'"),
                    ("confidence", "DOUBLE DEFAULT 1.0"),
                    ("revision", "INTEGER DEFAULT 1"),
                    ("created_by", "VARCHAR NOT NULL"),
                    ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("metadata", "JSON"),
                ),
                primary_key="id",
                indexes=(
                    ("idx_topo_elinks_from", ("from_event_id",)),
                    ("idx_topo_elinks_to", ("to_event_id",)),
                    ("idx_topo_elinks_from_type", ("from_event_id", "edge_type")),
                    ("idx_topo_elinks_to_type", ("to_event_id", "edge_type")),
                ),
                ordering=220,
                description="TOPO event links: directed relationships between events (e.g., caused_by, followed_by, overlaps).",
            ),
            DomainTable(
                name="topo_reports",
                columns=(
                    ("id", "VARCHAR"),
                    ("report_type", "VARCHAR"),
                    ("node_id", "VARCHAR"),
                    ("plan_id", "VARCHAR"),
                    ("title", "VARCHAR"),
                    ("summary", "TEXT"),
                    ("findings", "JSON"),
                    ("recommendations", "JSON"),
                    ("confidence_adjustments", "JSON"),
                    ("status", "VARCHAR DEFAULT 'draft'"),
                    ("version", "INTEGER DEFAULT 1"),
                    ("superseded_by", "VARCHAR"),
                    ("created_by", "VARCHAR NOT NULL"),
                    ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("finalized_at", "TIMESTAMP"),
                    ("metadata", "JSON"),
                ),
                primary_key="id",
                indexes=(
                    ("idx_topo_reports_type", ("report_type",)),
                    ("idx_topo_reports_node", ("node_id",)),
                    ("idx_topo_reports_plan", ("plan_id",)),
                    ("idx_topo_reports_status", ("status",)),
                    ("idx_topo_reports_version", ("id", "version")),
                ),
                ordering=230,
                description="TOPO versioned analytical report artifacts (e.g., sensitivity_analysis, rca_report, correlation_study) linked to graph entities.",
            ),
            DomainTable(
                name="topo_runs",
                columns=(
                    ("id", "VARCHAR"),
                    ("report_id", "VARCHAR"),
                    ("node_id", "VARCHAR"),
                    ("run_type", "VARCHAR"),
                    ("status", "VARCHAR DEFAULT 'pending'"),
                    ("inputs", "JSON"),
                    ("outputs", "JSON"),
                    ("error", "TEXT"),
                    ("duration_ms", "INTEGER"),
                    ("started_at", "TIMESTAMP"),
                    ("completed_at", "TIMESTAMP"),
                    ("created_by", "VARCHAR NOT NULL"),
                    ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("metadata", "JSON"),
                ),
                primary_key="id",
                indexes=(
                    ("idx_topo_runs_report", ("report_id",)),
                    ("idx_topo_runs_node", ("node_id",)),
                    ("idx_topo_runs_type", ("run_type",)),
                    ("idx_topo_runs_status", ("status",)),
                ),
                ordering=240,
                description="TOPO DataProductRun execution tracking: individual notebook/analytical runs with inputs, outputs, status, and timing.",
            ),
        ]

        return cls(
            name="topo",
            node_types=topo_node_types,
            layer_descriptions=topo_layer_descriptions,
            observation_types=topo_observation_types,
            observation_sources=topo_observation_sources,
            provenances=topo_provenances,
            case_strategy="uppercase",  # OHM-ue9k: accept legacy ALL-CAPS for migration
            domain_tables=topo_domain_tables,
        )

    @classmethod
    def beef_herd(cls) -> "SchemaConfig":
        """Create a Beef Herd Management schema configuration.

        Extends the base OHM schema with domain-specific types for
        beef cattle herd management decision systems:
        - Additional node types: animal, herd, pasture, feed, breed, health_event
        - Task management for operational decisions (heifer retention, drought response)
        - L4 edge types for risk/threat assessment (drought, disease, market volatility)
        - Observation types for PLF sensors and veterinary data
        """
        beef_node_types = VALID_NODE_TYPES | frozenset(
            {
                "animal",
                "herd",
                "breed",
                "feed",
                "pasture",
                "weather",
                "water",
                "health_event",
                "diagnosis",
                "treatment",
                "market",
                "contract",
                "equipment",
                "system",
            }
        )

        beef_layer_descriptions = {
            "L1": "Structure — Herd hierarchy (ranch → herd → cohort → animal), land, infrastructure",
            "L2": "Flow — Animal movements, feed flows, market transactions, veterinary records",
            "L3": "Knowledge — AND-gate analysis, drought response, disease patterns, market cycles",
            "L4": "Prospect — Risk assessments, heifer retention decisions, what-if scenarios",
        }

        beef_observation_types = VALID_OBSERVATION_TYPES | frozenset(
            {
                "weight",
                "temperature",
                "movement",
                "intake",
                "mortality",
                "conception",
                "price",
                "rainfall",
            }
        )

        beef_observation_sources = VALID_OBSERVATION_SOURCES | frozenset(
            {
                "sensor",
                "veterinarian",
                "auction",
                "usda",
                "noaa",
                "producer",
            }
        )

        beef_provenances = VALID_PROVENANCES | frozenset(
            {
                "plf",
                "veterinary",
                "market_report",
                "weather_service",
                "extension",
            }
        )

        return cls(
            name="beef_herd",
            node_types=beef_node_types,
            layer_descriptions=beef_layer_descriptions,
            observation_types=beef_observation_types,
            observation_sources=beef_observation_sources,
            provenances=beef_provenances,
        )

    def to_dict(self) -> dict:
        """Serialize the schema configuration to a dictionary."""
        result = {
            "name": self.name,
            "node_types": sorted(self.node_types),
            "layer_edge_types": {layer: sorted(types) for layer, types in self.layer_edge_types.items()},
            "layer_descriptions": dict(self.layer_descriptions),
            "observation_types": sorted(self.observation_types),
            "observation_sources": sorted(self.observation_sources),
            "visibilities": sorted(self.visibilities),
            "provenances": sorted(self.provenances),
        }
        if self.template_version > 0:
            result["template_version"] = self.template_version
        if self.required_integrations:
            result["required_integrations"] = self.required_integrations
        if self.optional_integrations:
            result["optional_integrations"] = self.optional_integrations
        if self.domain_tables:
            result["domain_tables"] = [dt.to_dict() for dt in self.domain_tables]
        result["ducklake_tables"] = [dlt.to_dict() for dlt in self.ducklake_tables]
        if self.case_strategy != "lowercase":
            result["case_strategy"] = self.case_strategy
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "SchemaConfig":
        """Deserialize a schema configuration from a dictionary.

        Inverse of ``to_dict()`` — converts sorted lists back to frozensets.

        Args:
            data: Dict with keys matching ``to_dict()`` output.

        Returns:
            SchemaConfig instance.

        Raises:
            ValueError: If required keys are missing.
        """
        required = {"name", "node_types", "layer_descriptions", "observation_types", "observation_sources", "provenances"}
        missing = required - set(data.keys())
        if missing:
            raise ValueError(f"Schema dict missing required keys: {sorted(missing)}")

        layer_edge_types = None
        if "layer_edge_types" in data:
            layer_edge_types = {layer: frozenset(types) for layer, types in data["layer_edge_types"].items()}

        domain_tables = None
        if "domain_tables" in data:
            domain_tables = [DomainTable.from_dict(d) for d in data["domain_tables"]]

        ducklake_tables = None
        if "ducklake_tables" in data:
            ducklake_tables = [DuckLakeTable.from_dict(d) for d in data["ducklake_tables"]]

        return cls(
            name=data["name"],
            node_types=frozenset(data["node_types"]),
            edge_types_by_layer=layer_edge_types,
            layer_descriptions=data["layer_descriptions"],
            observation_types=frozenset(data["observation_types"]),
            observation_sources=frozenset(data["observation_sources"]),
            visibilities=frozenset(data.get("visibilities", ["private", "team", "public"])),
            provenances=frozenset(data["provenances"]),
            required_integrations=data.get("required_integrations", {}),
            optional_integrations=data.get("optional_integrations", {}),
            template_version=int(data.get("template_version", 0)),
            seed_agents=data.get("seed_agents", []),  # OHM-tss4.1.1: domain agent pre-seeding
            domain_tables=domain_tables,  # OHM-vl8o: domain DDL
            ducklake_tables=ducklake_tables,  # OHM-8bli: DuckLake sync registry
            case_strategy=data.get("case_strategy", "lowercase"),  # OHM-ue9k: case strategy
        )

    @classmethod
    def from_json_file(cls, filename: str, *, search_paths: list[str] | None = None) -> "SchemaConfig":
        """Load a schema configuration from a JSON template file.

        Search order:
        1. Each directory in *search_paths* (if provided)
        2. ``/var/lib/ohm/templates/`` (custom templates without code deploy)
        3. Package-bundled ``ohm/graph/templates/`` directory

        Args:
            filename: Template filename (e.g., ``"topo.json"``).
            search_paths: Optional list of directories to search first.

        Returns:
            SchemaConfig instance loaded from the JSON file.

        Raises:
            FileNotFoundError: If the template file is not found in any search path.
            ValueError: If the JSON is invalid or missing required keys.
        """
        import json

        candidates = []
        if search_paths:
            candidates.extend(search_paths)
        candidates.append("/var/lib/ohm/templates")
        candidates.append(str(Path(__file__).parent / "templates"))

        for search_dir in candidates:
            path = Path(search_dir) / filename
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
                return cls.from_dict(data)

        raise FileNotFoundError(f"Schema template '{filename}' not found in: {candidates}")


# Default OHM schema config instance
DEFAULT_SCHEMA = SchemaConfig()

# TOPO (industrial) schema config instance
TOPO_SCHEMA = SchemaConfig.topo()

# Beef Herd schema config instance
BEEF_SCHEMA = SchemaConfig.beef_herd()

# Home Services schema config instance
HOME_SERVICES_SCHEMA = SchemaConfig.from_json_file("home_services.json")
MANUFACTURING_SCHEMA = SchemaConfig.from_json_file("manufacturing.json")
CONSTRUCTION_SCHEMA = SchemaConfig.from_json_file("construction.json")
HEALTHCARE_SCHEMA = SchemaConfig.from_json_file("healthcare.json")
INFRASTRUCTURE_SCHEMA = SchemaConfig.from_json_file("infrastructure.json")

# ── DDL Statements ──────────────────────────────────────────────────────────

DDL_STATEMENTS: list[str] = [
    # ── Nodes ────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_nodes (
        id            VARCHAR PRIMARY KEY,
        label         VARCHAR NOT NULL,
        type          VARCHAR NOT NULL,
        content       TEXT,
        url           TEXT,
        created_by    VARCHAR NOT NULL,
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_by    VARCHAR,
        confidence    DOUBLE DEFAULT 1.0,
        visibility    VARCHAR DEFAULT 'team',
        provenance    VARCHAR,
        tags          JSON,
        metadata      JSON,
        priority      VARCHAR,
        task_status   VARCHAR,          -- Task status: open/in_progress/blocked/review/done/cancelled
        assigned_to   VARCHAR,          -- Agent assigned to this task
        due_date      TIMESTAMP,        -- Task due date
        utility_scale FLOAT,            -- Decision node: how much does being wrong matter? (0-1)
        current_best_action VARCHAR,    -- Decision node: what we'd do with current info
        action_alternatives JSON,       -- Decision node: what we'd do if we knew more
        expected_claim   VARCHAR,       -- Task node: id of the claim this task tests (OHM-f5iq)
        success_criteria TEXT,          -- Task node: how to judge whether the claim held (OHM-f5iq)
        outcome          VARCHAR,       -- Task node: TRUE/FALSE/AMBIGUOUS recorded on close (OHM-f5iq)
        outcome_notes    TEXT,          -- Task node: free-text justification for the outcome (OHM-f5iq)
        gate_type        VARCHAR,         -- AND-gate governance: 'AND', 'OR', or NULL (OHM-as17)
        gate_status      VARCHAR,         -- AND-gate status: 'intact', 'converted', 'compromised' (OHM-as17)
        node_path        VARCHAR,         -- UNS hierarchical address (e.g., 'pns.fm10.main_drive') (OHM-ivlt)
        deleted_at    TIMESTAMP          -- Soft delete: NULL = active, set = deleted
    );
    """,
    # ── Edges ────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_edges (
        id              VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        from_node       VARCHAR NOT NULL,
        to_node         VARCHAR NOT NULL,
        layer           VARCHAR NOT NULL,
        edge_type       VARCHAR NOT NULL,
        confidence      DOUBLE,
        probability     DOUBLE,
        probability_p05 DOUBLE,
        probability_p50 DOUBLE,
        probability_p95 DOUBLE,
        confidence_p05  DOUBLE,
        confidence_p50  DOUBLE,
        confidence_p95  DOUBLE,
        urgency         VARCHAR,
        condition       TEXT,
        provenance      VARCHAR,
        created_by      VARCHAR NOT NULL,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_by      VARCHAR,
        challenge_of    VARCHAR,
        challenge_type  VARCHAR,
        metadata        JSON,
        constraint_expr TEXT,             -- AND-gate constraint expression (OHM-as17)
        deleted_at      TIMESTAMP          -- Soft delete: NULL = active, set = deleted
    );
    """,
    # ── Observations ─────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_observations (
        id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        node_id     VARCHAR,
        edge_id     VARCHAR,
        type        VARCHAR NOT NULL,
        value       FLOAT,
        baseline    FLOAT,
        sigma       FLOAT,
        source      VARCHAR,
        scale       VARCHAR DEFAULT 'unknown',
        created_by  VARCHAR NOT NULL,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        metadata    JSON,
        notes       TEXT,
        source_name TEXT,
        source_url  TEXT,
        deleted_at  TIMESTAMP          -- Soft delete: NULL = active, set = deleted
    );
    """,
    # ── Agent State ──────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_agent_state (
        agent_name           VARCHAR PRIMARY KEY,
        current_focus        TEXT,
        active_patterns      TEXT,
        last_sync            TIMESTAMP,
        confidence_threshold FLOAT DEFAULT 0.7,
        available_services   TEXT,
        current_session_id   VARCHAR,
        values               TEXT,
        goals                TEXT,
        updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Change Feed ──────────────────────────────────────────────────────
    # Split into separate execute() calls — DuckDB only runs the first
    # statement in a multi-statement string, so CREATE TABLE would be silently
    # skipped if combined with CREATE SEQUENCE in one string.
    "CREATE SEQUENCE IF NOT EXISTS seq_change_feed START 1",
    """CREATE TABLE IF NOT EXISTS ohm_change_feed (
        id          BIGINT PRIMARY KEY DEFAULT nextval('seq_change_feed'),
        table_name  VARCHAR NOT NULL,
        row_id      VARCHAR NOT NULL,
        operation   VARCHAR NOT NULL,
        agent_name  VARCHAR NOT NULL,
        old_data    JSON,
        new_data    JSON,
        occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    # ── Snapshots ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_snapshots (
        id          VARCHAR PRIMARY KEY,
        description VARCHAR,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Change Log (compatibility with store.py) ─────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_change_log (
        id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        table_name  VARCHAR NOT NULL,
        row_id      VARCHAR NOT NULL,
        operation   VARCHAR NOT NULL,
        agent_name  VARCHAR NOT NULL,
        layer       VARCHAR,
        snapshot_id VARCHAR,
        changed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        change_data JSON
    );
    """,
    # ── Agent Config (admin-set, read-only for agents) ───────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_agent_config (
        agent_name           VARCHAR PRIMARY KEY,
        optimization_target  VARCHAR NOT NULL,
        services             JSON,
        confidence_threshold FLOAT DEFAULT 0.7,
        sync_interval_sec    INTEGER DEFAULT 300,
        created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Schema Metadata ──────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_meta (
        key   VARCHAR PRIMARY KEY,
        value VARCHAR NOT NULL
    );
    """,
    # ── Metric Action Log (OHM-wx42) ─────────────────────────────────────
    # Tracks when semantic-layer metric actions last fired so the same
    # (metric, threshold, action_type) does not create duplicate tasks
    # within a configurable rate-limit window.
    """
    CREATE TABLE IF NOT EXISTS ohm_metric_action_log (
        id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        metric       VARCHAR NOT NULL,
        threshold    VARCHAR NOT NULL,
        action_type  VARCHAR NOT NULL,
        created_task_id VARCHAR,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_metric_action_log_lookup
        ON ohm_metric_action_log(metric, threshold, action_type, created_at);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_metric_action_log_created_at
        ON ohm_metric_action_log(created_at);
    """,
    # ── Webhook Outbox ────────────────────────────────────────────────────
    # Reliable webhook delivery with retry logic (OHM-ufjk)
    """
    CREATE SEQUENCE IF NOT EXISTS seq_webhook_outbox START 1
    """,
    """
    CREATE TABLE IF NOT EXISTS ohm_webhook_outbox (
        id          BIGINT PRIMARY KEY DEFAULT nextval('seq_webhook_outbox'),
        customer_id VARCHAR,
        agent       VARCHAR NOT NULL,
        url         VARCHAR NOT NULL,
        event_type  VARCHAR NOT NULL,
        event       JSON NOT NULL,
        status      VARCHAR DEFAULT 'pending',
        attempts    INTEGER DEFAULT 0,
        next_retry  TIMESTAMP,
        last_error  TEXT,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Webhook Dead Letter ─────────────────────────────────────────────
    """
    CREATE SEQUENCE IF NOT EXISTS seq_webhook_dead_letter START 1
    """,
    """
    CREATE TABLE IF NOT EXISTS webhook_dead_letter (
        id           BIGINT PRIMARY KEY DEFAULT nextval('seq_webhook_dead_letter'),
        agent_id     VARCHAR NOT NULL,
        event_type   VARCHAR NOT NULL,
        payload      JSON NOT NULL,
        error        TEXT,
        attempt_count INTEGER DEFAULT 0,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Webhook Subscriptions (OHM-whbk) ────────────────────────────────────
    # Persists agent→URL webhook registrations across server restarts. The
    # previous in-memory dict was lost on every restart, breaking deliveries
    # for any agent that registered after the last boot.
    """
    CREATE TABLE IF NOT EXISTS ohm_webhook_subscriptions (
        customer_id VARCHAR NOT NULL,
        agent       VARCHAR NOT NULL,
        url         VARCHAR NOT NULL,
        events      VARCHAR NOT NULL DEFAULT '["node.created","node.updated","edge.created"]',
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (customer_id, agent)
    );
    """,
    # ── Source Reliability Outcomes ──────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_outcomes (
        id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        source_agent VARCHAR NOT NULL,
        claim_node   VARCHAR NOT NULL,
        outcome      BOOLEAN NOT NULL,
        recorded_by  VARCHAR NOT NULL,
        recorded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        notes        TEXT,
        claimed_by   VARCHAR,
        verified_by  VARCHAR,
        domain       VARCHAR DEFAULT '*'
    );
    """,
    # ── Discovery Queue (OHM-od01.4) ────────────────────────────────────
    # Candidate edges from structure learning, pending agent review.
    # Not auto-added to ohm_edges — agents accept or reject via API.
    """
    CREATE TABLE IF NOT EXISTS ohm_discovery_queue (
        id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        from_node    VARCHAR NOT NULL,
        to_node      VARCHAR NOT NULL,
        edge_type    VARCHAR NOT NULL,
        layer        VARCHAR NOT NULL DEFAULT 'L3',
        confidence   FLOAT,
        provenance   VARCHAR NOT NULL DEFAULT 'structure_learning',
        method       VARCHAR NOT NULL,
        p_value      FLOAT,
        metadata     VARCHAR,
        status       VARCHAR NOT NULL DEFAULT 'pending',
        reviewed_by  VARCHAR,
        reviewed_at  TIMESTAMP,
        review_notes TEXT,
        created_by   VARCHAR NOT NULL DEFAULT 'system',
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Hook Registry (OHM-aznh) ───────────────────────────────────────
    # Registered shell/Python hooks for the staged ingestion pipeline.
    # Hooks run at pre_ingest, post_ingest, pre_query, post_query events.
    """
    CREATE TABLE IF NOT EXISTS ohm_hooks (
        id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        event       VARCHAR NOT NULL,
        command     VARCHAR NOT NULL,
        timeout_ms  INTEGER DEFAULT 5000,
        enabled     BOOLEAN DEFAULT TRUE,
        created_by  VARCHAR NOT NULL,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ohm_hook_log (
        id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        hook_id      VARCHAR NOT NULL,
        event        VARCHAR NOT NULL,
        payload      JSON,
        exit_code    INTEGER,
        stdout       TEXT,
        stderr       TEXT,
        duration_ms  FLOAT,
        timed_out    BOOLEAN DEFAULT FALSE,
        triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ohm_aliases (
        id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        alias_norm   VARCHAR NOT NULL,
        node_id      VARCHAR NOT NULL,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ohm_content_hashes (
        id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        node_id      VARCHAR NOT NULL,
        content_hash VARCHAR NOT NULL,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Data Products (ADR-027 / OHM-ksi0) ────────────────────────────────
    # ODPS v4.1-compliant data product catalog. Each row is one product in
    # one language; full ODPS YAML kept in odps_yaml for round-trip fidelity.
    """
    CREATE TABLE IF NOT EXISTS ohm_data_products (
        internal_id      VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        customer_id      VARCHAR,
        product_id       VARCHAR NOT NULL,
        name             VARCHAR NOT NULL,
        language         VARCHAR DEFAULT 'en',
        visibility       VARCHAR DEFAULT 'private',
        status           VARCHAR DEFAULT 'draft',
        type             VARCHAR NOT NULL,
        value_proposition VARCHAR,
        description      VARCHAR,
        producer_agent   VARCHAR,
        output_port_type VARCHAR,
        access_format    VARCHAR,
        access_url       VARCHAR,
        authentication_method VARCHAR,
        output_file_formats VARCHAR,
        ohm_node_id      VARCHAR,
        confidence       REAL,
        source_reliability REAL,
        product_version  VARCHAR,
        created          VARCHAR,
        updated          VARCHAR,
        odps_yaml        TEXT,
        created_by       VARCHAR,
        created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        deleted_at       TIMESTAMP,
        UNIQUE(customer_id, product_id, language)
    );
    """,
    # ── Suggestions Lifecycle (OHM-xtzk) ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_suggestions (
        id              VARCHAR PRIMARY KEY,
        suggestion_type VARCHAR NOT NULL,
        from_node       VARCHAR,
        to_node         VARCHAR,
        target_node     VARCHAR,
        suggested_edge_type VARCHAR,
        suggested_layer VARCHAR,
        confidence      FLOAT DEFAULT 0.5,
        status          VARCHAR DEFAULT 'ripe',
        suggested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        evidence_count  INTEGER DEFAULT 1,
        ripeness_score  FLOAT DEFAULT 0.0,
        last_ripened_at TIMESTAMP,
        source_method   VARCHAR,
        source_agent    VARCHAR,
        metadata        JSON,
        reviewed_by     VARCHAR,
        reviewed_at     TIMESTAMP,
        review_notes    TEXT,
        created_by      VARCHAR NOT NULL,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        deleted_at      TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ohm_nudge_log (
        id              VARCHAR PRIMARY KEY,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        agent           VARCHAR NOT NULL,
        action          VARCHAR NOT NULL,
        nudge_type      VARCHAR NOT NULL,
        severity        VARCHAR DEFAULT 'info',
        target_id       VARCHAR,
        message         TEXT,
        accepted        BOOLEAN DEFAULT NULL,
        accepted_at     TIMESTAMP,
        metadata        JSON
    );
    """,
    # ── Confidence Change Log (OHM-733) ──────────────────────────────────
    # Append-only log of every confidence-affecting event on an edge.
    # ohm_edges.confidence becomes a cached/materialized value refreshed
    # from this log — the log is the source of truth, not the column.
    """
    CREATE TABLE IF NOT EXISTS ohm_confidence_log (
        id              VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        edge_id         VARCHAR NOT NULL,
        agent           VARCHAR NOT NULL,
        old_value       DOUBLE,
        new_value       DOUBLE NOT NULL,
        reason          VARCHAR NOT NULL,
        challenge_id    VARCHAR,
        metadata        JSON,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_conf_log_edge ON ohm_confidence_log(edge_id, created_at DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_conf_log_agent ON ohm_confidence_log(agent);
    """,
]

# ── Schema Version ──────────────────────────────────────────────────────────

SCHEMA_VERSION = "0.46.0"

# ── Migrations ──────────────────────────────────────────────────────────────
# Each migration is (version, description, list_of_sql_statements).
# Applied incrementally: if current version < migration version, apply it.

MIGRATIONS: list[tuple[str, str, list[str]]] = [
    (
        "0.2.0",
        "add values/goals columns to agent_state",
        [
            "ALTER TABLE ohm_agent_state ADD COLUMN values TEXT",
            "ALTER TABLE ohm_agent_state ADD COLUMN goals TEXT",
        ],
    ),
    (
        "0.3.0",
        "add tags/metadata JSON columns and agent_config table",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN tags JSON",
            "ALTER TABLE ohm_nodes ADD COLUMN metadata JSON",
            "ALTER TABLE ohm_edges ADD COLUMN metadata JSON",
            "ALTER TABLE ohm_observations ADD COLUMN metadata JSON",
        ],
    ),
    (
        "0.4.0",
        "add agent relationship node types and edge types",
        [
            "",  # Node types and edge types are validated in Python, not DDL
        ],
    ),
    (
        "0.5.0",
        "add probability column to ohm_edges for supply chain / risk modeling",
        [
            "ALTER TABLE ohm_edges ADD COLUMN probability FLOAT",
        ],
    ),
    (
        "0.6.0",
        "add priority column to ohm_nodes, urgency column to ohm_edges, and sentiment observation type",
        ["ALTER TABLE ohm_nodes ADD COLUMN priority VARCHAR", "ALTER TABLE ohm_edges ADD COLUMN urgency VARCHAR", "ALTER TABLE ohm_observations ADD COLUMN sentiment VARCHAR"],
    ),
    (
        "0.7.0",
        "add ohm_outcomes table for source reliability tracking",
        [
            """CREATE TABLE IF NOT EXISTS ohm_outcomes (
            id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
            source_agent VARCHAR NOT NULL,
            claim_node  VARCHAR NOT NULL,
            outcome     BOOLEAN NOT NULL,
            recorded_by VARCHAR NOT NULL,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes       TEXT
        )""",
            "CREATE INDEX IF NOT EXISTS idx_outcomes_source ON ohm_outcomes(source_agent)",
            "CREATE INDEX IF NOT EXISTS idx_outcomes_claim ON ohm_outcomes(claim_node)",
        ],
    ),
    (
        "0.8.0",
        "add notes column to ohm_observations",
        [
            "ALTER TABLE ohm_observations ADD COLUMN notes TEXT",
        ],
    ),
    (
        "0.9.0",
        "add url column to ohm_nodes for external references",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN url TEXT",
        ],
    ),
    (
        "0.10.0",
        "add source_name and source_url to ohm_observations",
        [
            "ALTER TABLE ohm_observations ADD COLUMN source_name TEXT",
            "ALTER TABLE ohm_observations ADD COLUMN source_url TEXT",
        ],
    ),
    (
        "0.11.0",
        "add embedding column to ohm_nodes for semantic search (OHM-o9f)",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN embedding FLOAT[768]",
        ],
    ),
    (
        "0.12.0",
        "add deleted_at column to ohm_nodes, ohm_edges, ohm_observations for soft delete (OHM-cpi)",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN deleted_at TIMESTAMP",
            "ALTER TABLE ohm_edges ADD COLUMN deleted_at TIMESTAMP",
            "ALTER TABLE ohm_observations ADD COLUMN deleted_at TIMESTAMP",
        ],
    ),
    (
        "0.13.0",
        "add task_status column and task-related indexes for action item tracking",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN task_status VARCHAR",
            "ALTER TABLE ohm_nodes ADD COLUMN due_date TIMESTAMP",
            "ALTER TABLE ohm_nodes ADD COLUMN assigned_to VARCHAR",
            "CREATE INDEX IF NOT EXISTS idx_nodes_task_status ON ohm_nodes(task_status) WHERE task_status IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_nodes_assigned_to ON ohm_nodes(assigned_to) WHERE assigned_to IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_nodes_due_date ON ohm_nodes(due_date) WHERE due_date IS NOT NULL",
        ],
    ),
    (
        "0.14.0",
        "add graph_generation counter to ohm_meta for cache invalidation (OHM-omr)",
        [
            "INSERT INTO ohm_meta (key, value) SELECT 'graph_generation', '0' WHERE NOT EXISTS (SELECT 1 FROM ohm_meta WHERE key = 'graph_generation')",
        ],
    ),
    (
        "0.15.0",
        "add PERT distribution columns to ohm_edges for VoI (OHM-6mv.3)",
        [
            "ALTER TABLE ohm_edges ADD COLUMN probability_p05 FLOAT",
            "ALTER TABLE ohm_edges ADD COLUMN probability_p50 FLOAT",
            "ALTER TABLE ohm_edges ADD COLUMN probability_p95 FLOAT",
            "ALTER TABLE ohm_edges ADD COLUMN confidence_p05 FLOAT",
            "ALTER TABLE ohm_edges ADD COLUMN confidence_p50 FLOAT",
            "ALTER TABLE ohm_edges ADD COLUMN confidence_p95 FLOAT",
        ],
    ),
    (
        "0.16.0",
        "add decision node type with utility function fields (OHM-6mv.2)",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN utility_scale FLOAT",
            "ALTER TABLE ohm_nodes ADD COLUMN current_best_action VARCHAR",
            "ALTER TABLE ohm_nodes ADD COLUMN action_alternatives JSON",
        ],
    ),
    (
        "0.17.0",
        "add USD utility columns to decision nodes for VoI calibration (OHM-fh3e)",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS utility_usd_per_day FLOAT",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS utility_currency VARCHAR",
        ],
    ),
    (
        "0.18.0",
        "create ohm_change_feed/ohm_change_log if missing (OHM-y30o)",
        [
            "CREATE SEQUENCE IF NOT EXISTS seq_change_feed START 1",
            """CREATE TABLE IF NOT EXISTS ohm_change_feed (
            id          BIGINT PRIMARY KEY DEFAULT nextval('seq_change_feed'),
            table_name  VARCHAR NOT NULL,
            row_id      VARCHAR NOT NULL,
            operation   VARCHAR NOT NULL,
            agent_name  VARCHAR NOT NULL,
            old_data    JSON,
            new_data    JSON,
            occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
            """CREATE TABLE IF NOT EXISTS ohm_change_log (
            id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
            table_name  VARCHAR NOT NULL,
            row_id      VARCHAR NOT NULL,
            operation   VARCHAR NOT NULL,
            agent_name  VARCHAR NOT NULL,
            layer       VARCHAR,
            snapshot_id VARCHAR,
            changed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            change_data JSON
        )""",
            "CREATE INDEX IF NOT EXISTS idx_feed_agent ON ohm_change_feed(agent_name)",
            "CREATE INDEX IF NOT EXISTS idx_feed_time ON ohm_change_feed(occurred_at)",
        ],
    ),
    (
        "0.19.0",
        "add scale column to ohm_observations for value normalization (OHM-33)",
        [
            "ALTER TABLE ohm_observations ADD COLUMN scale VARCHAR DEFAULT 'unknown'",
            "CREATE INDEX IF NOT EXISTS idx_obs_scale ON ohm_observations(scale)",
        ],
    ),
    (
        "0.20.0",
        "add discovery queue table for structure learning candidate edges (OHM-od01.4)",
        [
            """CREATE TABLE IF NOT EXISTS ohm_discovery_queue (
            id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
            from_node    VARCHAR NOT NULL,
            to_node      VARCHAR NOT NULL,
            edge_type    VARCHAR NOT NULL,
            layer        VARCHAR NOT NULL DEFAULT 'L3',
            confidence   FLOAT,
            provenance   VARCHAR NOT NULL DEFAULT 'structure_learning',
            method       VARCHAR NOT NULL,
            p_value      FLOAT,
            metadata     VARCHAR,
            status       VARCHAR NOT NULL DEFAULT 'pending',
            reviewed_by  VARCHAR,
            reviewed_at  TIMESTAMP,
            review_notes TEXT,
            created_by   VARCHAR NOT NULL DEFAULT 'system',
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
            "CREATE INDEX IF NOT EXISTS idx_discovery_queue_status ON ohm_discovery_queue(status)",
        ],
    ),
    (
        "0.21.0",
        "add hook registry table for staged ingestion pipeline (OHM-aznh)",
        [
            """CREATE TABLE IF NOT EXISTS ohm_hooks (
            id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
            event       VARCHAR NOT NULL,
            command     VARCHAR NOT NULL,
            timeout_ms  INTEGER DEFAULT 5000,
            enabled     BOOLEAN DEFAULT TRUE,
            created_by  VARCHAR NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
            "CREATE INDEX IF NOT EXISTS idx_hooks_event_enabled ON ohm_hooks(event, enabled)",
        ],
    ),
    (
        "0.22.0",
        "add hook invocation log for audit trail (OHM-aznh.7)",
        [
            """CREATE TABLE IF NOT EXISTS ohm_hook_log (
            id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
            hook_id      VARCHAR NOT NULL,
            event        VARCHAR NOT NULL,
            payload      JSON,
            exit_code    INTEGER,
            stdout       TEXT,
            stderr       TEXT,
            duration_ms  FLOAT,
            timed_out    BOOLEAN DEFAULT FALSE,
            triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
            "CREATE INDEX IF NOT EXISTS idx_hook_log_hook ON ohm_hook_log(hook_id)",
            "CREATE INDEX IF NOT EXISTS idx_hook_log_time ON ohm_hook_log(triggered_at)",
        ],
    ),
    (
        "0.23.0",
        "add alias resolution and content hashing tables (OHM-g0kv)",
        [
            """CREATE TABLE IF NOT EXISTS ohm_aliases (
            id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
            alias_norm   VARCHAR NOT NULL,
            node_id      VARCHAR NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
            "CREATE INDEX IF NOT EXISTS idx_aliases_norm ON ohm_aliases(alias_norm)",
            "CREATE INDEX IF NOT EXISTS idx_aliases_node ON ohm_aliases(node_id)",
            """CREATE TABLE IF NOT EXISTS ohm_content_hashes (
            id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
            node_id      VARCHAR NOT NULL,
            content_hash VARCHAR NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_content_hash_node ON ohm_content_hashes(node_id)",
            "CREATE INDEX IF NOT EXISTS idx_content_hash_hash ON ohm_content_hashes(content_hash)",
        ],
    ),
    (
        "0.24.0",
        "add infrastructure, service, release node types; RUNS_ON, HOSTS, VERSION_OF, LOCATED_IN (L1) and UPSTREAM_OF (L2) edge types; health_check observation type; healthcheck provenance; tags/metadata on create_node (OHM-infra)",
        [
            "",  # Node types and edge types are validated in Python, not DDL
        ],
    ),
    (
        "0.25.0",
        "OHM-xdd4: temporal decay — add half_life_days, valid_from, valid_to, supersedes_obs_id to observations",
        [
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS half_life_days FLOAT",
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS valid_from TIMESTAMP",
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS valid_to TIMESTAMP",
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS supersedes_obs_id VARCHAR",
            # Backfill: existing observations are currently active with valid_from = created_at
            "UPDATE ohm_observations SET valid_from = created_at WHERE valid_from IS NULL",
            # Index for supersession chain traversal
            "CREATE INDEX IF NOT EXISTS idx_obs_supersedes ON ohm_observations(supersedes_obs_id)",
            # Index for active observations (valid_to IS NULL)
            "CREATE INDEX IF NOT EXISTS idx_obs_valid_to ON ohm_observations(valid_to)",
        ],
    ),
    (
        "0.26.0",
        "OHM-24g9: Weibull shape parameter — add weibull_shape column to observations",
        [
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS weibull_shape FLOAT",
            # Backfill: set default Weibull shape per obs_type
            "UPDATE ohm_observations SET weibull_shape = 1.0 WHERE weibull_shape IS NULL AND type = 'measurement'",
            "UPDATE ohm_observations SET weibull_shape = 1.5 WHERE weibull_shape IS NULL AND type = 'sentiment'",
            "UPDATE ohm_observations SET weibull_shape = 0.7 WHERE weibull_shape IS NULL AND type = 'verification'",
            "UPDATE ohm_observations SET weibull_shape = 0.0 WHERE weibull_shape IS NULL AND type = 'outcome'",
            "UPDATE ohm_observations SET weibull_shape = 1.0 WHERE weibull_shape IS NULL AND type = 'source'",
            "UPDATE ohm_observations SET weibull_shape = -1.0 WHERE weibull_shape IS NULL AND type = 'pattern'",
            # Remaining types get exponential (κ=1.0)
            "UPDATE ohm_observations SET weibull_shape = 1.0 WHERE weibull_shape IS NULL",
        ],
    ),
    (
        "0.27.0",
        "OHM-od01.4: Causal discovery — add p_value and metadata columns to discovery queue",
        [
            "ALTER TABLE ohm_discovery_queue ADD COLUMN IF NOT EXISTS p_value FLOAT",
            "ALTER TABLE ohm_discovery_queue ADD COLUMN IF NOT EXISTS metadata VARCHAR",
        ],
    ),
    (
        "0.28.0",
        "ADR-026: Myth Compression Framework — add compression_degree, compression_type, beneficiary, revisability to observations",
        [
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS compression_degree FLOAT",
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS compression_type VARCHAR",
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS beneficiary JSON",
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS revisability FLOAT",
            "CREATE INDEX IF NOT EXISTS idx_obs_compression ON ohm_observations(compression_degree) WHERE compression_degree IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_obs_revisability ON ohm_observations(revisability) WHERE revisability IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_obs_comp_type ON ohm_observations(compression_type) WHERE compression_type IS NOT NULL",
        ],
    ),
    (
        "0.29.0",
        "ADR-027: BOS ODPS data product catalog — add ohm_data_products table",
        [
            """
            CREATE TABLE IF NOT EXISTS ohm_data_products (
                internal_id      VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
                customer_id      VARCHAR,
                product_id       VARCHAR NOT NULL,
                name             VARCHAR NOT NULL,
                language         VARCHAR DEFAULT 'en',
                visibility       VARCHAR DEFAULT 'private',
                status           VARCHAR DEFAULT 'draft',
                type             VARCHAR NOT NULL,
                value_proposition VARCHAR,
                description      VARCHAR,
                producer_agent   VARCHAR,
                output_port_type VARCHAR,
                access_format    VARCHAR,
                access_url       VARCHAR,
                authentication_method VARCHAR,
                output_file_formats VARCHAR,
                ohm_node_id      VARCHAR,
                confidence       REAL,
                source_reliability REAL,
                product_version  VARCHAR,
                created          VARCHAR,
                updated          VARCHAR,
                odps_yaml        TEXT,
                created_by       VARCHAR,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_at       TIMESTAMP,
                UNIQUE(customer_id, product_id, language)
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_data_products_producer ON ohm_data_products(producer_agent);",
            "CREATE INDEX IF NOT EXISTS idx_data_products_type ON ohm_data_products(type);",
            "CREATE INDEX IF NOT EXISTS idx_data_products_status ON ohm_data_products(status);",
            "CREATE INDEX IF NOT EXISTS idx_data_products_customer ON ohm_data_products(customer_id);",
        ],
    ),
    (
        "0.30.0",
        "ADR-028: Source tier — add source_tier column to ohm_nodes/ohm_edges",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS source_tier VARCHAR;",
            "ALTER TABLE ohm_edges ADD COLUMN IF NOT EXISTS source_tier VARCHAR;",
            "CREATE INDEX IF NOT EXISTS idx_nodes_source_tier ON ohm_nodes(source_tier);",
            "CREATE INDEX IF NOT EXISTS idx_edges_source_tier ON ohm_edges(source_tier);",
        ],
    ),
    (
        "0.31.0",
        "ADR-032: HD fingerprint — add hd_fingerprint column to ohm_nodes",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS hd_fingerprint BLOB;",
            "CREATE INDEX IF NOT EXISTS idx_ohm_nodes_hd_fingerprint ON ohm_nodes(id) WHERE hd_fingerprint IS NOT NULL;",
        ],
    ),
    (
        "0.32.0",
        "ADR-033: Source diversity — add source_author, source_institution, data_origin columns",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS source_author VARCHAR;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS source_institution VARCHAR;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS data_origin VARCHAR;",
        ],
    ),
    (
        "0.33.0",
        "ADR-034/035/036/037: emerging concepts + TELOS signing + suggestions lifecycle + read scopes",
        [
            # tlqz
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS emerging_concept_score JSON;",
            # enwb
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS write_signature VARCHAR;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS signing_key_id VARCHAR;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS signed_at TIMESTAMP;",
            "ALTER TABLE ohm_edges ADD COLUMN IF NOT EXISTS write_signature VARCHAR;",
            "ALTER TABLE ohm_edges ADD COLUMN IF NOT EXISTS signing_key_id VARCHAR;",
            "ALTER TABLE ohm_edges ADD COLUMN IF NOT EXISTS signed_at TIMESTAMP;",
            "CREATE INDEX IF NOT EXISTS idx_ohm_nodes_signing_key_id ON ohm_nodes(signing_key_id) WHERE signing_key_id IS NOT NULL;",
            "CREATE INDEX IF NOT EXISTS idx_ohm_edges_signing_key_id ON ohm_edges(signing_key_id) WHERE signing_key_id IS NOT NULL;",
            # xtzk
            """
            CREATE TABLE IF NOT EXISTS ohm_suggestions (
                id              VARCHAR PRIMARY KEY,
                suggestion_type VARCHAR NOT NULL,
                from_node       VARCHAR,
                to_node         VARCHAR,
                target_node     VARCHAR,
                suggested_edge_type VARCHAR,
                suggested_layer VARCHAR,
                confidence      FLOAT DEFAULT 0.5,
                status          VARCHAR DEFAULT 'ripe',
                suggested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                evidence_count  INTEGER DEFAULT 1,
                ripeness_score  FLOAT DEFAULT 0.0,
                last_ripened_at TIMESTAMP,
                source_method   VARCHAR,
                source_agent    VARCHAR,
                metadata        JSON,
                reviewed_by     VARCHAR,
                reviewed_at     TIMESTAMP,
                review_notes    TEXT,
                created_by      VARCHAR NOT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_at      TIMESTAMP
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_ohm_suggestions_status ON ohm_suggestions(status) WHERE deleted_at IS NULL;",
            "CREATE INDEX IF NOT EXISTS idx_ohm_suggestions_target ON ohm_suggestions(target_node) WHERE deleted_at IS NULL;",
            # ybyb
            "ALTER TABLE ohm_agent_config ADD COLUMN IF NOT EXISTS read_scope JSON;",
        ],
    ),
    (
        "0.34.0",
        "Hypothesis-tree primitives for iterative research (OHM-ss22)",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS hypothesis_status VARCHAR;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS artifact_ref VARCHAR;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS dev_metric FLOAT;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS test_metric FLOAT;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS parent_hypothesis_id VARCHAR;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS project_id VARCHAR;",
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS worktree_ref VARCHAR;",
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS evaluation_script VARCHAR;",
            "ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS held_out BOOLEAN DEFAULT FALSE;",
            "CREATE INDEX IF NOT EXISTS idx_nodes_hypothesis_status ON ohm_nodes(hypothesis_status);",
            "CREATE INDEX IF NOT EXISTS idx_nodes_project_id ON ohm_nodes(project_id);",
            "CREATE INDEX IF NOT EXISTS idx_nodes_parent_hypothesis ON ohm_nodes(parent_hypothesis_id);",
            "CREATE INDEX IF NOT EXISTS idx_obs_held_out ON ohm_observations(held_out);",
        ],
    ),
    (
        "0.35.0",
        "OHM-wx42: semantic-layer metric action log for rate-limited auto actions",
        [
            """
            CREATE TABLE IF NOT EXISTS ohm_metric_action_log (
                id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
                metric       VARCHAR NOT NULL,
                threshold    VARCHAR NOT NULL,
                action_type  VARCHAR NOT NULL,
                created_task_id VARCHAR,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_metric_action_log_lookup ON ohm_metric_action_log(metric, threshold, action_type, created_at);",
            "CREATE INDEX IF NOT EXISTS idx_metric_action_log_created_at ON ohm_metric_action_log(created_at);",
        ],
    ),
    (
        "0.36.0",
        "OHM-f5iq: outcome feedback loop — task expected_claim, success_criteria, outcome, outcome_notes",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS expected_claim VARCHAR;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS success_criteria TEXT;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS outcome VARCHAR;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS outcome_notes TEXT;",
        ],
    ),
    (
        "0.37.0",
        "OHM-iuoz: feedback-graph node and edge types for scenario/action/intervention",
        [
            # No DDL needed — node types and edge types are validated in
            # application code via VALID_NODE_TYPES / LAYER_EDGE_TYPES.
            # This migration exists to bump the schema version so agents
            # can detect that the feedback-graph types are available.
            "SELECT 1;",
        ],
    ),
    (
        "0.38.0",
        "OHM-as17: AND-gate governance — gate_type, gate_status on nodes, constraint_expr on edges",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS gate_type VARCHAR;",
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS gate_status VARCHAR;",
            "ALTER TABLE ohm_edges ADD COLUMN IF NOT EXISTS constraint_expr TEXT;",
            "CREATE INDEX IF NOT EXISTS idx_nodes_gate_type ON ohm_nodes(gate_type) WHERE gate_type IS NOT NULL;",
            "CREATE INDEX IF NOT EXISTS idx_nodes_gate_status ON ohm_nodes(gate_status) WHERE gate_status IS NOT NULL;",
        ],
    ),
    (
        "0.39.0",
        "OHM-jdfq: nudge log table for epistemic quality analytics",
        [
            "CREATE TABLE IF NOT EXISTS ohm_nudge_log ("
            "id VARCHAR PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "agent VARCHAR NOT NULL, action VARCHAR NOT NULL, nudge_type VARCHAR NOT NULL, "
            "severity VARCHAR DEFAULT 'info', target_id VARCHAR, message TEXT, "
            "accepted BOOLEAN DEFAULT NULL, accepted_at TIMESTAMP, metadata JSON);",
            "CREATE INDEX IF NOT EXISTS idx_nudge_log_agent ON ohm_nudge_log(agent);",
            "CREATE INDEX IF NOT EXISTS idx_nudge_log_type ON ohm_nudge_log(nudge_type);",
            "CREATE INDEX IF NOT EXISTS idx_nudge_log_created ON ohm_nudge_log(created_at);",
        ],
    ),
    # OHM-vl8o: domain DDL hook. No new core OHM tables in this migration —
    # domain tables are created by initialize_schema() from the SchemaConfig.
    # The migration entry is a no-op that bumps the version so existing
    # deployments can detect they are on a build that knows about the
    # SchemaConfig.domain_tables field.
    (
        "0.40.0",
        "OHM-vl8o: domain DDL hook (SchemaConfig.domain_tables) — version bump, no schema change",
        [],
    ),
    (
        "0.41.0",
        "OHM-yiui: add claimed_by and verified_by to ohm_outcomes so verification credit flows to the source, not the verifier",
        [
            "ALTER TABLE ohm_outcomes ADD COLUMN IF NOT EXISTS claimed_by VARCHAR",
            "ALTER TABLE ohm_outcomes ADD COLUMN IF NOT EXISTS verified_by VARCHAR",
            # Create indexes BEFORE running UPDATE backfills. DuckDB refuses
            # to create an index when the transaction has outstanding UPDATEs.
            "CREATE INDEX IF NOT EXISTS idx_outcomes_claimed_by ON ohm_outcomes(claimed_by)",
            "CREATE INDEX IF NOT EXISTS idx_outcomes_verified_by ON ohm_outcomes(verified_by)",
            # Backfill verified_by from the existing recorded_by column.
            "UPDATE ohm_outcomes SET verified_by = recorded_by WHERE verified_by IS NULL",
            # Backfill claimed_by from the originating edge's created_by.
            # The claim_node is the from_node of the edge that made the
            # claim; we look up the oldest L3 edge with that from_node
            # and credit its created_by. If no edge is found, fall back
            # to the existing source_agent (which is usually the same
            # agent but was caller-supplied and may be wrong).
            "UPDATE ohm_outcomes SET claimed_by = (  SELECT e.created_by FROM ohm_edges e   WHERE e.from_node = ohm_outcomes.claim_node     AND e.deleted_at IS NULL   ORDER BY e.created_at ASC LIMIT 1) WHERE claimed_by IS NULL",
            "UPDATE ohm_outcomes SET claimed_by = source_agent WHERE claimed_by IS NULL",
        ],
    ),
    (
        "0.42.0",
        "OHM-m32a: add corroboration_count to ohm_edges for cross-graph corroboration tracking",
        [
            "ALTER TABLE ohm_edges ADD COLUMN IF NOT EXISTS corroboration_count INTEGER DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_edges_corroboration ON ohm_edges(corroboration_count);",
        ],
    ),
    (
        "0.43.0",
        "OHM-avkj: add domain column to ohm_outcomes for domain-aware source reliability",
        [
            "ALTER TABLE ohm_outcomes ADD COLUMN IF NOT EXISTS domain VARCHAR DEFAULT '*'",
            # Create index BEFORE UPDATE backfills (DuckDB: no index creation with outstanding updates).
            "CREATE INDEX IF NOT EXISTS idx_outcomes_domain ON ohm_outcomes(domain)",
            # Backfill domain from the claim node's provenance
            "UPDATE ohm_outcomes SET domain = (  SELECT n.provenance FROM ohm_nodes n   WHERE n.id = ohm_outcomes.claim_node AND n.deleted_at IS NULL) WHERE domain = '*' OR domain IS NULL",
            "UPDATE ohm_outcomes SET domain = '*' WHERE domain IS NULL",
        ],
    ),
    (
        "0.44.0",
        "OHM-ivlt: add node_path column to ohm_nodes for UNS hierarchical addressing",
        [
            "ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS node_path VARCHAR",
            "CREATE INDEX IF NOT EXISTS idx_nodes_path ON ohm_nodes(node_path)",
        ],
    ),
    (
        "0.45.0",
        "OHM-741: widen confidence/probability columns from FLOAT to DOUBLE to eliminate IEEE 754 float32 precision noise (0.85 stored as 0.8500000238418579)",
        [
            # DuckDB refuses ALTER COLUMN TYPE while indexes reference the
            # table, so drop every index on ohm_nodes / ohm_edges first.
            "DROP INDEX IF EXISTS idx_nodes_type",
            "DROP INDEX IF EXISTS idx_nodes_created_by",
            "DROP INDEX IF EXISTS idx_nodes_visibility",
            "DROP INDEX IF EXISTS idx_nodes_provenance",
            "DROP INDEX IF EXISTS idx_nodes_task_status",
            "DROP INDEX IF EXISTS idx_nodes_assigned_to",
            "DROP INDEX IF EXISTS idx_nodes_due_date",
            "DROP INDEX IF EXISTS idx_nodes_source_tier",
            "DROP INDEX IF EXISTS idx_nodes_hypothesis_status",
            "DROP INDEX IF EXISTS idx_nodes_project_id",
            "DROP INDEX IF EXISTS idx_nodes_parent_hypothesis",
            "DROP INDEX IF EXISTS idx_nodes_gate_type",
            "DROP INDEX IF EXISTS idx_nodes_gate_status",
            "DROP INDEX IF EXISTS idx_nodes_path",
            "DROP INDEX IF EXISTS idx_edges_from",
            "DROP INDEX IF EXISTS idx_edges_to",
            "DROP INDEX IF EXISTS idx_edges_layer",
            "DROP INDEX IF EXISTS idx_edges_type",
            "DROP INDEX IF EXISTS idx_edges_created_by",
            "DROP INDEX IF EXISTS idx_edges_challenge_of",
            "DROP INDEX IF EXISTS idx_edges_traversal",
            "DROP INDEX IF EXISTS idx_edges_source_tier",
            "DROP INDEX IF EXISTS idx_edges_corroboration",
            # Widen FLOAT → DOUBLE
            "ALTER TABLE ohm_nodes ALTER COLUMN confidence TYPE DOUBLE",
            "ALTER TABLE ohm_edges ALTER COLUMN confidence TYPE DOUBLE",
            "ALTER TABLE ohm_edges ALTER COLUMN probability TYPE DOUBLE",
            "ALTER TABLE ohm_edges ALTER COLUMN probability_p05 TYPE DOUBLE",
            "ALTER TABLE ohm_edges ALTER COLUMN probability_p50 TYPE DOUBLE",
            "ALTER TABLE ohm_edges ALTER COLUMN probability_p95 TYPE DOUBLE",
            "ALTER TABLE ohm_edges ALTER COLUMN confidence_p05 TYPE DOUBLE",
            "ALTER TABLE ohm_edges ALTER COLUMN confidence_p50 TYPE DOUBLE",
            "ALTER TABLE ohm_edges ALTER COLUMN confidence_p95 TYPE DOUBLE",
            # Recreate all dropped indexes (IF NOT EXISTS keeps it idempotent)
            "CREATE INDEX IF NOT EXISTS idx_edges_from ON ohm_edges(from_node)",
            "CREATE INDEX IF NOT EXISTS idx_edges_to ON ohm_edges(to_node)",
            "CREATE INDEX IF NOT EXISTS idx_edges_layer ON ohm_edges(layer)",
            "CREATE INDEX IF NOT EXISTS idx_edges_type ON ohm_edges(edge_type)",
            "CREATE INDEX IF NOT EXISTS idx_edges_created_by ON ohm_edges(created_by)",
            "CREATE INDEX IF NOT EXISTS idx_edges_challenge_of ON ohm_edges(challenge_of)",
            "CREATE INDEX IF NOT EXISTS idx_edges_traversal ON ohm_edges(from_node, layer, edge_type)",
            "CREATE INDEX IF NOT EXISTS idx_edges_source_tier ON ohm_edges(source_tier)",
            "CREATE INDEX IF NOT EXISTS idx_edges_corroboration ON ohm_edges(corroboration_count)",
            "CREATE INDEX IF NOT EXISTS idx_nodes_type ON ohm_nodes(type)",
            "CREATE INDEX IF NOT EXISTS idx_nodes_created_by ON ohm_nodes(created_by)",
            "CREATE INDEX IF NOT EXISTS idx_nodes_visibility ON ohm_nodes(visibility)",
            "CREATE INDEX IF NOT EXISTS idx_nodes_provenance ON ohm_nodes(provenance)",
            "CREATE INDEX IF NOT EXISTS idx_nodes_task_status ON ohm_nodes(task_status) WHERE task_status IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_nodes_assigned_to ON ohm_nodes(assigned_to) WHERE assigned_to IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_nodes_due_date ON ohm_nodes(due_date) WHERE due_date IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_nodes_source_tier ON ohm_nodes(source_tier)",
            "CREATE INDEX IF NOT EXISTS idx_nodes_hypothesis_status ON ohm_nodes(hypothesis_status)",
            "CREATE INDEX IF NOT EXISTS idx_nodes_project_id ON ohm_nodes(project_id)",
            "CREATE INDEX IF NOT EXISTS idx_nodes_parent_hypothesis ON ohm_nodes(parent_hypothesis_id)",
            "CREATE INDEX IF NOT EXISTS idx_nodes_gate_type ON ohm_nodes(gate_type) WHERE gate_type IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_nodes_gate_status ON ohm_nodes(gate_status) WHERE gate_status IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_nodes_path ON ohm_nodes(node_path)",
        ],
    ),
    (
        "0.46.0",
        "OHM-733: append-only confidence change log table + indexes for multi-writer federation",
        [
            "CREATE TABLE IF NOT EXISTS ohm_confidence_log ("
            "id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(), "
            "edge_id VARCHAR NOT NULL, agent VARCHAR NOT NULL, "
            "old_value DOUBLE, new_value DOUBLE NOT NULL, "
            "reason VARCHAR NOT NULL, challenge_id VARCHAR, "
            "metadata JSON, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            "CREATE INDEX IF NOT EXISTS idx_conf_log_edge ON ohm_confidence_log(edge_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_conf_log_agent ON ohm_confidence_log(agent)",
        ],
    ),
]

INDEX_DDL: list[str] = [
    # Edge traversal indexes
    "CREATE INDEX IF NOT EXISTS idx_edges_from ON ohm_edges(from_node);",
    "CREATE INDEX IF NOT EXISTS idx_edges_to ON ohm_edges(to_node);",
    "CREATE INDEX IF NOT EXISTS idx_edges_layer ON ohm_edges(layer);",
    "CREATE INDEX IF NOT EXISTS idx_edges_type ON ohm_edges(edge_type);",
    "CREATE INDEX IF NOT EXISTS idx_edges_created_by ON ohm_edges(created_by);",
    "CREATE INDEX IF NOT EXISTS idx_edges_challenge_of ON ohm_edges(challenge_of);",
    # Node lookup indexes
    "CREATE INDEX IF NOT EXISTS idx_nodes_type ON ohm_nodes(type);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_created_by ON ohm_nodes(created_by);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_visibility ON ohm_nodes(visibility);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_provenance ON ohm_nodes(provenance);",
    # Observation indexes
    "CREATE INDEX IF NOT EXISTS idx_obs_node ON ohm_observations(node_id);",
    "CREATE INDEX IF NOT EXISTS idx_obs_edge ON ohm_observations(edge_id);",
    "CREATE INDEX IF NOT EXISTS idx_obs_type ON ohm_observations(type);",
    "CREATE INDEX IF NOT EXISTS idx_obs_created_by ON ohm_observations(created_by);",
    "CREATE INDEX IF NOT EXISTS idx_obs_scale ON ohm_observations(scale);",
    # Change feed index
    "CREATE INDEX IF NOT EXISTS idx_feed_agent ON ohm_change_feed(agent_name);",
    "CREATE INDEX IF NOT EXISTS idx_feed_time ON ohm_change_feed(occurred_at);",
    # Composite index for CTE traversal (from_node + layer + edge_type)
    "CREATE INDEX IF NOT EXISTS idx_edges_traversal ON ohm_edges(from_node, layer, edge_type);",
    # Source reliability indexes
    "CREATE INDEX IF NOT EXISTS idx_outcomes_source ON ohm_outcomes(source_agent);",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_claim ON ohm_outcomes(claim_node);",
    # Hook registry index
    "CREATE INDEX IF NOT EXISTS idx_hooks_event_enabled ON ohm_hooks(event, enabled);",
    # Data Products indexes (ADR-027)
    "CREATE INDEX IF NOT EXISTS idx_data_products_producer ON ohm_data_products(producer_agent);",
    "CREATE INDEX IF NOT EXISTS idx_data_products_type ON ohm_data_products(type);",
    "CREATE INDEX IF NOT EXISTS idx_data_products_status ON ohm_data_products(status);",
    "CREATE INDEX IF NOT EXISTS idx_data_products_customer ON ohm_data_products(customer_id);",
]


def initialize_schema(conn: "DuckDBPyConnection", schema: "SchemaConfig | None" = None) -> None:
    """Create all tables and indexes if they don't exist.

    Then applies any pending migrations based on the stored schema version.

    Args:
        conn: An active DuckDB connection.
        schema: Optional SchemaConfig for domain agent seeding (OHM-tss4.1.1)
            and domain table DDL (OHM-vl8o).
    """
    # Safety: checkpoint before DDL to flush any prior WAL state (OHM-8n9).
    # Without this, stale WAL entries from a prior session could conflict
    # with the DDL statements below.
    try:
        conn.execute("CHECKPOINT")
    except Exception:
        pass
    for ddl in DDL_STATEMENTS:
        conn.execute(ddl)
    for idx in INDEX_DDL:
        conn.execute(idx)
    # Set initial schema version if not present
    _ensure_meta_table(conn)
    # Apply migrations incrementally
    _apply_migrations(conn)
    # Create HNSW index on embedding column (if VSS extension loaded)
    _create_hnsw_index(conn)
    # Seed domain agents from schema config (OHM-tss4.1.1)
    if schema:
        _seed_domain_agents(conn, schema)
    # OHM-vl8o: domain-specific tables (TOPO needs topo_prospects, etc.)
    # Created after the base OHM DDL and migrations so domain tables can
    # reference ohm_nodes / ohm_edges if needed.
    if schema:
        _create_domain_tables(conn, schema)


def _clean_ddl_for_ducklake(ddl: str) -> str:
    """Strip DuckLake-incompatible features from a DDL statement.

    DuckLake does NOT support PRIMARY KEY, UNIQUE constraints, sequences,
    or indexes. This function strips those while keeping gen_random_uuid()
    (which DuckLake supports) and renames row_id (reserved by DuckLake).
    """
    import re

    # Skip sequences, indexes entirely
    if "CREATE SEQUENCE" in ddl or "CREATE INDEX" in ddl or "CREATE UNIQUE INDEX" in ddl:
        return ""
    # Skip per-daemon tables that use row_id (reserved by DuckLake)
    if "ohm_change_feed" in ddl or "ohm_metric_action_log" in ddl or "ohm_change_log" in ddl:
        return ""
    # Strip PRIMARY KEY (with optional column list in parentheses)
    cleaned = re.sub(r",?\s*PRIMARY\s+KEY\s*\([^)]*\)", "", ddl, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+PRIMARY\s+KEY", "", cleaned, flags=re.IGNORECASE)
    # Strip UNIQUE(...) table constraints and trailing comma
    cleaned = re.sub(r",?\s*UNIQUE\s*\([^)]*\)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+UNIQUE(?!\s*\()", "", cleaned, flags=re.IGNORECASE)
    # Replace sequence defaults — DuckLake doesn't support sequences
    cleaned = re.sub(r"DEFAULT\s+nextval\([^)]+\)", "DEFAULT NULL", cleaned, flags=re.IGNORECASE)
    # Rename row_id — reserved by DuckLake for internal use
    cleaned = cleaned.replace("row_id", "change_row_id")
    # Clean up trailing commas before closing paren
    cleaned = re.sub(r",\s*\)", "\n    )", cleaned)
    return cleaned


def initialize_schema_ducklake(conn: "DuckDBPyConnection", schema: "SchemaConfig | None" = None) -> None:
    """Create OHM tables in a DuckLake schema (OHM-734 federated mode).

    DuckLake does NOT support PRIMARY KEY or UNIQUE constraints, and has
    limited index support. This function strips those from the DDL and
    skips index creation. Uniqueness is enforced in application code
    (same as the existing DuckLake mirror tables in db.py).
    """
    for ddl in DDL_STATEMENTS:
        cleaned = _clean_ddl_for_ducklake(ddl)
        if not cleaned:
            continue
        try:
            conn.execute(cleaned)
        except Exception as e:
            err_msg = str(e).lower()
            if "already exists" in err_msg or "duplicate" in err_msg:
                pass
            else:
                raise

    # Skip indexes — DuckLake has limited index support
    # Skip HNSW embedding index — not available in DuckLake
    _ensure_meta_table(conn)
    # Apply migrations — using the same DDL cleaning as base tables
    _apply_migrations_ducklake(conn)
    if schema:
        _seed_domain_agents(conn, schema)
        _create_domain_tables(conn, schema)


def _ensure_meta_table(conn: "DuckDBPyConnection") -> None:
    """Ensure the ohm_meta table exists and has a schema_version entry."""
    # Table is created by DDL_STATEMENTS, but ensure version row exists
    existing = conn.execute("SELECT COUNT(*) FROM ohm_meta WHERE key = 'schema_version'").fetchone()
    if existing is None or existing[0] == 0:
        conn.execute(
            "INSERT INTO ohm_meta (key, value) VALUES ('schema_version', ?)",
            ["0.1.0"],  # Base version before migrations
        )


def _version_tuple(v: str) -> tuple[int, ...]:
    """Convert a version string to a comparable tuple."""
    return tuple(int(x) for x in v.split("."))


def _apply_migrations(conn: "DuckDBPyConnection") -> None:
    """Apply pending migrations based on the current schema version.

    Safety measures against WAL corruption (OHM-b5a):
    1. Checkpoint before migrations to flush pending WAL entries
    2. Each migration runs in its own transaction
    3. Checkpoint after each migration to commit schema changes to disk
    """
    current = conn.execute("SELECT value FROM ohm_meta WHERE key = 'schema_version'").fetchone()
    current_version = current[0] if current else "0.1.0"

    current_key = _version_tuple(current_version)

    for version, description, statements in MIGRATIONS:
        if current_key < _version_tuple(version):
            # Checkpoint before migration to flush any pending WAL entries
            # This prevents WAL replay failures if the daemon is restarted
            # during or after migration.
            try:
                conn.execute("PRAGMA checkpoint")
            except Exception:
                pass

            # Run migration statements in a transaction for atomicity
            try:
                conn.execute("BEGIN TRANSACTION")
                for stmt in statements:
                    try:
                        conn.execute(stmt)
                    except Exception as e:
                        # DuckDB ALTER TABLE ADD COLUMN fails if column exists.
                        # Only swallow CatalogException (duplicate column/table/index).
                        # Re-raise everything else (disk full, type mismatch, etc.) (OHM-exur).
                        err_msg = str(e).lower()
                        if any(s in err_msg for s in ("already exists", "duplicate", "already a column", "column with name")):
                            pass  # Idempotent — column/table/index already exists
                        elif "not implemented" in err_msg and "index" in err_msg:
                            pass  # DuckDB version doesn't support this index type (e.g., partial indexes) — safe to skip
                        else:
                            from ohm.framework.exceptions import MigrationError

                            raise MigrationError(f"Migration {version} failed: {e}") from e
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise  # Re-raise — migration failure should be visible

            # Update version and checkpoint to persist schema change
            conn.execute(
                "UPDATE ohm_meta SET value = ? WHERE key = 'schema_version'",
                [version],
            )
            try:
                conn.execute("PRAGMA checkpoint")
            except Exception:
                pass

            current_key = _version_tuple(version)


def _apply_migrations_ducklake(conn: "DuckDBPyConnection") -> None:
    """Apply migrations for DuckLake-backed schemas (OHM-734).

    Uses the same _clean_ddl_for_ducklake helper as initialize_schema_ducklake
    to strip PK/UNIQUE/sequences from migration statements before executing.
    Statements that are entirely cleaned away (indexes, sequences) are skipped.
    The version is bumped so the schema is marked current.
    """
    current = conn.execute("SELECT value FROM ohm_meta WHERE key = 'schema_version'").fetchone()
    current_version = current[0] if current else "0.1.0"
    current_key = _version_tuple(current_version)

    for version, description, statements in MIGRATIONS:
        if current_key < _version_tuple(version):
            for stmt in statements:
                if not stmt:
                    continue
                # Apply the same DDL cleaning as base tables
                cleaned = _clean_ddl_for_ducklake(stmt)
                if not cleaned:
                    continue
                try:
                    conn.execute(cleaned)
                except Exception as e:
                    err_msg = str(e).lower()
                    if any(
                        s in err_msg
                        for s in (
                            "already exists",
                            "duplicate",
                            "already a column",
                            "column with name",
                            "unsupported type",
                            "ducklake",
                            "not implemented",
                            "sequence",
                            "row_id",
                        )
                    ):
                        try:
                            conn.execute("ROLLBACK")
                        except Exception:
                            pass
                        logger.warning(
                            "Skipped DuckLake-incompatible migration %s statement: %s",
                            version,
                            str(e)[:200],
                        )
                    else:
                        from ohm.framework.exceptions import MigrationError

                        raise MigrationError(f"Migration {version} failed: {e}") from e

            conn.execute(
                "UPDATE ohm_meta SET value = ? WHERE key = 'schema_version'",
                [version],
            )
            current_key = _version_tuple(version)


def _create_hnsw_index(conn: "DuckDBPyConnection") -> None:
    """Create HNSW index on ohm_nodes.embedding if VSS extension is loaded.

    The index is created only if:
    1. The VSS extension is loaded
    2. The embedding column exists (added by migration 0.11.0)
    3. The index doesn't already exist

    Uses cosine distance metric for semantic similarity search.
    The index is created with experimental persistence enabled so it
    survives database restarts.
    """
    # Check if VSS extension is loaded
    try:
        vss_loaded = conn.execute("SELECT COUNT(*) FROM duckdb_extensions() WHERE loaded = true AND extension_name = 'vss'").fetchone()
        if vss_loaded is None or vss_loaded[0] == 0:
            return  # VSS not available — skip index creation
    except Exception:
        return

    # Check if embedding column exists
    try:
        has_embedding = conn.execute("SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'ohm_nodes' AND column_name = 'embedding'").fetchone()
        if has_embedding is None or has_embedding[0] == 0:
            return  # Column doesn't exist yet — skip
    except Exception:
        return

    # Create HNSW index (idempotent — IF NOT EXISTS)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_embedding ON ohm_nodes USING HNSW (embedding) WITH (metric = 'cosine')")
    except Exception:
        pass  # Index creation may fail if no data yet — safe to ignore


def _seed_agent_configs(conn: "DuckDBPyConnection") -> None:
    """Seed default agent configurations if the table is empty.

    OHM is deployment-agnostic — no agents are pre-configured.
    Each deployment registers its own agents via the SDK's
    register_agent() method. This function is a no-op placeholder
    for future deployment-specific seeding scripts.
    """
    pass  # OHM is generic — no hardcoded agent configs


def _seed_domain_agents(conn: "DuckDBPyConnection", schema: "SchemaConfig | None") -> None:
    """Seed domain-specific agent role nodes from schema config (OHM-tss4.1.1).

    Domain templates can declare a ``seed_agents`` list containing agent
    role definitions. Each entry creates:
    - An agent node (type='agent') with the role name as ID
    - An agent state row (ohm_agent_state) with role metadata

    Args:
        conn: Active DuckDB connection.
        schema: SchemaConfig with optional seed_agents list.
    """
    if schema is None or not schema.seed_agents:
        return

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()

    for agent_def in schema.seed_agents:
        agent_name = agent_def.get("agent_name")
        if not agent_name:
            continue

        node_id = f"agent::{agent_name}"

        existing = conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]).fetchone()

        if not existing:
            conn.execute(
                """
                INSERT INTO ohm_nodes (id, type, label, created_by, created_at, visibility, provenance)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    node_id,
                    "agent",
                    agent_def.get("label", agent_name),
                    "ohmd",
                    now,
                    "public",
                    "system-seed",
                ],
            )

        existing_state = conn.execute("SELECT agent_name FROM ohm_agent_state WHERE agent_name = ?", [agent_name]).fetchone()

        if not existing_state:
            conn.execute(
                """
                INSERT INTO ohm_agent_state
                (agent_name, current_focus, active_patterns, last_sync, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    agent_name,
                    agent_def.get("current_focus", ""),
                    json.dumps(agent_def.get("active_patterns", [])),
                    now,
                    now,
                ],
            )

    logger.info("Seeded %d domain agents for schema '%s'", len(schema.seed_agents), schema.name)


def _create_domain_tables(conn: "DuckDBPyConnection", schema: "SchemaConfig | None") -> None:
    """Create domain-specific tables from schema config (OHM-vl8o).

    Iterates ``schema.domain_tables`` (a tuple of :class:`DomainTable` already
    sorted by ``ordering``) and creates each one in declared order. Each
    table is created with ``CREATE TABLE IF NOT EXISTS`` so the operation is
    idempotent — re-running ``initialize_schema()`` is a no-op for tables
    that already exist.

    Per-table initial seed rows (``initial_data``) are inserted only on first
    creation: if the table has zero rows after the CREATE, the seed rows
    are inserted; otherwise they're skipped. This is the right model for
    "create-if-missing, don't overwrite user data" — operators may add
    rows between deployments and we must not clobber them.

    Per-table migration version is tracked in ``ohm_meta`` under the key
    ``domain_tables:<name>:ordering``. This lets later code reason about
    which tables have been provisioned without re-running CREATE.

    Args:
        conn: Active DuckDB connection.
        schema: SchemaConfig with optional ``domain_tables`` list.
    """
    if schema is None or not schema.domain_tables:
        return

    for dt in schema.domain_tables:
        # Build CREATE TABLE statement.
        col_lines = []
        for col_name, col_type in dt.columns:
            col_lines.append(f"    {col_name} {col_type}")
        if dt.primary_key is not None:
            col_lines.append(f"    PRIMARY KEY ({dt.primary_key})")
        create_sql = f"CREATE TABLE IF NOT EXISTS {dt.name} (\n" + ",\n".join(col_lines) + "\n);"
        try:
            conn.execute(create_sql)
        except Exception as e:
            # Defensive: surface domain DDL errors with context.
            err = str(e).lower()
            if "already exists" in err:
                pass  # Race with another process — fine, table is there.
            else:
                raise RuntimeError(f"Failed to create domain table '{dt.name}' (ordering={dt.ordering}): {e}") from e

        # Create indexes.
        for idx_name, idx_cols in dt.indexes:
            cols_csv = ", ".join(idx_cols)
            try:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {dt.name}({cols_csv})")
            except Exception as e:
                err = str(e).lower()
                if "already exists" in err:
                    pass
                elif "not found" in err or "binder error" in err:
                    logger.debug(
                        "Skipping index '%s' on '%s' (column not found, migration may be pending): %s",
                        idx_name,
                        dt.name,
                        e,
                    )
                else:
                    raise RuntimeError(f"Failed to create index '{idx_name}' on '{dt.name}': {e}") from e

        # Seed initial data — only if the table is empty.
        if dt.initial_data:
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {dt.name}").fetchone()
            except Exception:
                count = None
            if count is not None and count[0] == 0:
                # Build parameterized INSERT.
                col_names = [c[0] for c in dt.columns]
                placeholders = ", ".join(["?"] * len(col_names))
                col_list = ", ".join(col_names)
                insert_sql = f"INSERT INTO {dt.name} ({col_list}) VALUES ({placeholders})"
                for row in dt.initial_data:
                    values = [row.get(c) for c in col_names]
                    try:
                        conn.execute(insert_sql, values)
                    except Exception as e:
                        # Seed failures should not be fatal (e.g. uniqueness
                        # violation on rerun with same key) — log and move on.
                        logger.warning(
                            "Domain table seed insert failed for '%s' (row=%r): %s",
                            dt.name,
                            row,
                            e,
                        )

        # Record the provisioning version in ohm_meta.
        try:
            meta_key = f"domain_tables:{dt.name}:ordering"
            existing = conn.execute("SELECT value FROM ohm_meta WHERE key = ?", [meta_key]).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO ohm_meta (key, value) VALUES (?, ?)",
                    [meta_key, str(dt.ordering)],
                )
        except Exception as e:
            logger.warning(
                "Failed to record domain table version for '%s' in ohm_meta: %s",
                dt.name,
                e,
            )

    logger.info(
        "Created %d domain tables for schema '%s'",
        len(schema.domain_tables),
        schema.name,
    )

    # OHM-dh9l.1: migrate TOPO temporal tables from pilot to target column vocabulary.
    if schema.name == "topo":
        _migrate_topo_temporal_tables(conn)


def _column_exists(conn: "DuckDBPyConnection", table_name: str, col_name: str) -> bool:
    """Return True if *col_name* exists on *table_name* (OHM-dh9l.1)."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            [table_name, col_name],
        ).fetchone()
        return row is not None and row[0] > 0
    except Exception:
        return False


def _rename_column_if_exists(conn: "DuckDBPyConnection", table: str, old_col: str, new_col: str) -> None:
    """Rename *old_col* to *new_col* on *table* if the old column exists and the new one does not."""
    if _column_exists(conn, table, old_col) and not _column_exists(conn, table, new_col):
        try:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}")
            logger.info("Renamed %s.%s → %s", table, old_col, new_col)
        except Exception as e:
            logger.warning("Failed to rename %s.%s → %s: %s", table, old_col, new_col, e)


def _add_column_if_not_exists(conn: "DuckDBPyConnection", table: str, col_name: str, col_type: str) -> None:
    """Add *col_name* to *table* if it does not already exist."""
    if not _column_exists(conn, table, col_name):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
            logger.info("Added column %s.%s (%s)", table, col_name, col_type)
        except Exception as e:
            err = str(e).lower()
            if "already exists" in err or "column with name" in err:
                pass
            else:
                logger.warning("Failed to add column %s.%s: %s", table, col_name, e)


def _drop_index_if_exists(conn: "DuckDBPyConnection", index_name: str) -> None:
    """Drop *index_name* if it exists (idempotent)."""
    try:
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")
    except Exception:
        pass


def _migrate_topo_temporal_tables(conn: "DuckDBPyConnection") -> None:
    """Migrate TOPO temporal tables from pilot to target column vocabulary (OHM-dh9l.1).

    The pilot (OHM-dm2b) shipped with a simplified column vocabulary:
    ``event_type``/``severity``/``start_time``/``end_time``/``horizon_start``/
    ``horizon_end``/``link_type``. The target vocabulary (ADR-041) renames
    these and adds new columns for richer temporal semantics.

    This migration is idempotent: it detects the old pilot schema by the
    presence of the ``event_type`` column on ``topo_events`` and renames/adds
    columns only when the old schema is found. Re-running on an already-
    migrated or fresh database is a no-op.
    """
    # Gate: only run if topo_events exists and has the old event_type column.
    try:
        row = conn.execute("SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'topo_events' AND column_name = 'event_type'").fetchone()
    except Exception:
        return
    if row is None or row[0] == 0:
        return  # Fresh schema or already migrated

    logger.info("Migrating TOPO temporal tables to target column vocabulary (OHM-dh9l.1)")

    # --- topo_plans: horizon_start → start_ts, horizon_end → end_ts ---
    _rename_column_if_exists(conn, "topo_plans", "horizon_start", "start_ts")
    _rename_column_if_exists(conn, "topo_plans", "horizon_end", "end_ts")
    _add_column_if_not_exists(conn, "topo_plans", "label", "VARCHAR")
    _add_column_if_not_exists(conn, "topo_plans", "horizon", "VARCHAR")
    # The old idx_topo_plans_horizon was on (horizon_start, horizon_end).
    # After rename it's on (start_ts, end_ts). The new idx_topo_plans_horizon
    # indexes the `horizon` column — drop the old index so the new one can
    # be created by _create_domain_tables().
    _drop_index_if_exists(conn, "idx_topo_plans_horizon")

    # --- topo_events: event_type → event_class, severity → operating_state,
    #     start_time → start_ts, end_time → end_ts ---
    _rename_column_if_exists(conn, "topo_events", "event_type", "event_class")
    _rename_column_if_exists(conn, "topo_events", "severity", "operating_state")
    _rename_column_if_exists(conn, "topo_events", "start_time", "start_ts")
    _rename_column_if_exists(conn, "topo_events", "end_time", "end_ts")
    for col, typ in (
        ("node_path", "VARCHAR"),
        ("horizon", "VARCHAR"),
        ("title", "VARCHAR"),
        ("source_refs", "JSON"),
        ("l3_context", "JSON"),
        ("flow_impact", "JSON"),
        ("forecast_basis", "JSON"),
        ("decision_metadata", "JSON"),
        ("confidence", "DOUBLE"),
        ("authority", "VARCHAR"),
        ("revision", "INTEGER DEFAULT 1"),
    ):
        _add_column_if_not_exists(conn, "topo_events", col, typ)
    # Drop old indexes that are no longer in the target spec.
    _drop_index_if_exists(conn, "idx_topo_events_type")
    _drop_index_if_exists(conn, "idx_topo_events_time")

    # --- topo_event_links: link_type → edge_type ---
    _rename_column_if_exists(conn, "topo_event_links", "link_type", "edge_type")
    _add_column_if_not_exists(conn, "topo_event_links", "layer", "VARCHAR DEFAULT 'L1'")
    _add_column_if_not_exists(conn, "topo_event_links", "confidence", "DOUBLE DEFAULT 1.0")
    _add_column_if_not_exists(conn, "topo_event_links", "revision", "INTEGER DEFAULT 1")
    # Drop old index that is no longer in the target spec.
    _drop_index_if_exists(conn, "idx_topo_elinks_type")

    logger.info("TOPO temporal table migration complete (OHM-dh9l.1)")


def get_schema_version(conn: "DuckDBPyConnection") -> str:
    """Return the current schema version from the database.

    Args:
        conn: An active DuckDB connection.

    Returns:
        The schema version string (e.g., '0.3.0'), or '0.0.0' if
        the ohm_meta table doesn't exist yet.
    """
    try:
        result = conn.execute("SELECT value FROM ohm_meta WHERE key = 'schema_version'").fetchone()
        return result[0] if result else "0.0.0"
    except Exception:
        # Table doesn't exist yet — database hasn't been initialized
        return "0.0.0"


def validate_edge_type(layer: str, edge_type: str) -> bool:
    """Check that *edge_type* is valid for the given *layer*.

    Returns True if valid, False otherwise.
    """
    allowed = LAYER_EDGE_TYPES.get(layer)
    if allowed is None:
        return False
    return edge_type in allowed


def validate_node_type(node_type: str) -> bool:
    """Check that *node_type* is a known type (case-insensitive).

    Per OHM-ue9k, validation is case-insensitive against the canonical
    lowercase type set: ``"METRIC"`` validates the same as ``"metric"``.
    This is a backward-compatible change — all existing lowercase
    callers see identical behavior. Legacy TOPO stores that used
    UPPERCASE node types now pass validation without a one-shot
    rename, while new writes are normalized to lowercase via
    :func:`normalize_node_type` to keep the canonical form
    consistent across the graph.
    """
    if not node_type:
        return False
    if node_type in VALID_NODE_TYPES:
        return True
    return node_type.lower() in VALID_NODE_TYPES


def normalize_node_type(node_type: str) -> str:
    """Return the canonical lowercase form of *node_type* (OHM-ue9k).

    If *node_type* (case-insensitive) matches a known type, returns
    the canonical lowercase name. Otherwise returns *node_type*
    unchanged — the caller is responsible for further validation.

    This is the canonicalization step that downstream writes should
    call before persisting node types. It is intentionally permissive:
    an unknown type is returned as-is so the caller can produce a
    clear error message ("unknown node type 'X'") rather than
    silently mangling the input.
    """
    if not node_type:
        return node_type
    if node_type in VALID_NODE_TYPES:
        return node_type
    lower = node_type.lower()
    if lower in VALID_NODE_TYPES:
        return lower
    return node_type


def requires_cross_link(node_type: str) -> bool:
    """Return True if a node of *node_type* must include a `connects_to` reference.

    Per OHM-tjzh / ADR-018: synthesis-like node types cannot stand alone. They
    must be anchored to existing graph structure via a same-body edge or by
    referencing an existing node through `connects_to`. Bare creation of these
    types produces dead-end nodes that cannot be navigated to, challenged, or
    used in Bayesian inference.

    Exempt types (source, concept, entity) are explicitly allowed as stubs
    because they represent foundational or external references.
    """
    if node_type in EXEMPT_CROSS_LINK_NODE_TYPES:
        return False
    return node_type in MUST_HAVE_EDGE_NODE_TYPES


def generate_node_id(label: str, node_type: str | None = None) -> str:
    """Generate a human-readable node ID from a label.

    Converts to lowercase, replaces spaces and special characters with
    underscores, transliterates unicode to ASCII, and appends a short
    suffix for uniqueness.

    When *node_type* is provided, the returned ID is prefixed according
    to the contract's ``type_prefixes`` mapping (e.g. ``hypothesis-``,
    ``experiment-``). Unknown node types fall back to an unprefixed ID
    for backward compatibility.

    Examples:
        'AND→OR conversion' → 'and-or-conversion_a1b2c3'
        'Café étude'        → 'cafe-etude_d4e5f6'
        'Main Hypothesis'   → 'hypothesis-main-hypothesis_a1b2c3'
    """
    import unicodedata
    import re

    # Normalize unicode: decompose accented chars, then strip diacritics
    normalized = unicodedata.normalize("NFKD", label)
    ascii_label = normalized.encode("ascii", "ignore").decode("ascii")

    # Replace any remaining non-alphanumeric chars with underscores
    base = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_label).strip("_").lower()
    if not base:
        base = "node"

    # Collapse multiple underscores from punctuation stripping
    base = re.sub(r"_+", "_", base)

    suffix = uuid.uuid4().hex[:6]

    prefix = ""
    if node_type:
        from ohm.server.contract import NamingConventions  # local import avoids cycle

        prefix = NamingConventions().type_prefixes.get(node_type, "")
    if prefix:
        return f"{prefix}{base}_{suffix}"
    return f"{base}_{suffix}"


# Compatibility: single-string schema for modules that expect SCHEMA_SQL
SCHEMA_SQL = "\n".join(DDL_STATEMENTS + INDEX_DDL)

# Compatibility exports for server.py
EDGE_TYPES = {k: list(v) for k, v in LAYER_EDGE_TYPES.items()}
NODE_TYPES = sorted(VALID_NODE_TYPES)
