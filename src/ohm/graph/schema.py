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
    }
)

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
VALID_GATE_STATUSES = frozenset({
    "intact",       # Gate is functioning as designed
    "converted",    # AND-gate has been converted to OR-gate (strategic shift)
    "compromised",  # One or more inputs have failed but gate hasn't fully collapsed
    "failed",       # Gate has collapsed — all inputs lost
    # OHM-8dg4 reconciliation: Metis design-note aliases
    "open",         # Alias for 'intact' — gate is open and processing
    "closed",       # Alias for 'converted' — gate has been deliberately closed
    "stuck",        # Alias for 'compromised' — gate is stuck waiting for input
})

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
        """Check that *node_type* is valid for this schema."""
        return node_type in self.node_types

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

        return cls(
            name="topo",
            node_types=topo_node_types,
            layer_descriptions=topo_layer_descriptions,
            observation_types=topo_observation_types,
            observation_sources=topo_observation_sources,
            provenances=topo_provenances,
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
        confidence    FLOAT DEFAULT 1.0,
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
        gate_type        VARCHAR,       -- AND-gate governance: 'AND', 'OR', or NULL (OHM-as17)
        gate_status      VARCHAR,       -- AND-gate status: 'intact', 'converted', 'compromised' (OHM-as17)
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
        confidence      FLOAT,
        probability     FLOAT,
        probability_p05 FLOAT,
        probability_p50 FLOAT,
        probability_p95 FLOAT,
        confidence_p05  FLOAT,
        confidence_p50  FLOAT,
        confidence_p95  FLOAT,
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
        notes        TEXT
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
]

# ── Schema Version ──────────────────────────────────────────────────────────

SCHEMA_VERSION = "0.38.0"

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
]

# ── Indexes ─────────────────────────────────────────────────────────────────

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
        schema: Optional SchemaConfig for domain agent seeding (OHM-tss4.1.1).
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


def _ensure_meta_table(conn: "DuckDBPyConnection") -> None:
    """Ensure the ohm_meta table exists and has a schema_version entry."""
    # Table is created by DDL_STATEMENTS, but ensure version row exists
    existing = conn.execute("SELECT COUNT(*) FROM ohm_meta WHERE key = 'schema_version'").fetchone()
    if existing is None or existing[0] == 0:
        conn.execute(
            "INSERT INTO ohm_meta (key, value) VALUES ('schema_version', ?)",
            ["0.1.0"],  # Base version before migrations
        )


def _apply_migrations(conn: "DuckDBPyConnection") -> None:
    """Apply pending migrations based on the current schema version.

    Safety measures against WAL corruption (OHM-b5a):
    1. Checkpoint before migrations to flush pending WAL entries
    2. Each migration runs in its own transaction
    3. Checkpoint after each migration to commit schema changes to disk
    """
    current = conn.execute("SELECT value FROM ohm_meta WHERE key = 'schema_version'").fetchone()
    current_version = current[0] if current else "0.1.0"

    def _version_tuple(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split("."))

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
    """Check that *node_type* is a known type."""
    return node_type in VALID_NODE_TYPES


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
