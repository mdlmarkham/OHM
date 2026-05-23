"""
OHM Contract Layer — Schema-enforced validation and linting.

The contract layer formalizes the rules that govern how agents write to the
knowledge graph. Inspired by the LLM Wiki "agents.mmd" pattern, it provides:

1. **NamingConventions** — Rules for node IDs, labels, and edge formatting
2. **RequiredFields** — Metadata that must be present on specific node/edge types
3. **ContractConfig** — Combines SchemaConfig with contract rules
4. **lint()** — Validate the entire graph against the contract

This is the "constitution" for the knowledge graph — the contract between
agents and the shared memory layer that ensures consistency at scale.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any

from ohm.schema import SchemaConfig

logger = logging.getLogger(__name__)


# ── Naming Conventions ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class NamingConventions:
    """Rules for naming graph entities.

    Enforces consistent ID formats, label styles, and edge naming.
    These are the "naming convention" section of the contract.
    """

    # Node ID format: lowercase, hyphens, type-prefix
    # e.g., "concept-boolean-directionality", "pattern-and-or-conversion"
    node_id_pattern: str = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"

    # Label: title case, descriptive
    # e.g., "Boolean Directionality", "AND→OR Conversion"
    label_min_length: int = 3
    label_max_length: int = 200

    # Content: minimum length for meaningful notes
    content_min_length: int = 50

    # Edge type: UPPER_SNAKE_CASE
    edge_type_pattern: str = r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$"

    # Node type prefix: node IDs should start with their type
    # e.g., concept-*, pattern-*, event-*, task-*
    type_prefix_required: bool = True

    # Type prefix mapping (type → expected prefix)
    type_prefixes: dict[str, str] = field(
        default_factory=lambda: {
            "concept": "concept-",
            "pattern": "pattern-",
            "event": "event-",
            "task": "task-",
            "agent": "agent-",
            "source": "source-",
            "skill": "skill-",
            "value": "value-",
            "goal": "goal-",
            "idea": "idea-",
            "person": "person-",
            "institution": "institution-",
            "technology": "tech-",
        }
    )


# ── Required Fields ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RequiredFields:
    """Metadata fields that must be present on specific entity types.

    These are the "front matter schema" section of the contract.
    Missing required fields are lint violations.
    """

    # Fields required on ALL nodes
    node_required: frozenset[str] = frozenset(
        {
            "id",
            "label",
            "type",
            "created_by",
        }
    )

    # Fields required on concept/pattern nodes (knowledge layer)
    knowledge_required: frozenset[str] = frozenset(
        {
            "id",
            "label",
            "type",
            "content",
            "confidence",
            "created_by",
        }
    )

    # Fields required on task nodes
    task_required: frozenset[str] = frozenset(
        {
            "id",
            "label",
            "type",
            "priority",
            "task_status",
            "assigned_to",
        }
    )

    # Fields required on source nodes
    source_required: frozenset[str] = frozenset(
        {
            "id",
            "label",
            "type",
            "provenance",
            "created_by",
        }
    )

    # Fields required on ALL edges
    edge_required: frozenset[str] = frozenset(
        {
            "from_node",
            "to_node",
            "edge_type",
            "layer",
            "created_by",
        }
    )

    # Fields required on CAUSES/DEPENDS_ON edges (for Bayesian inference)
    causal_edge_required: frozenset[str] = frozenset(
        {
            "from_node",
            "to_node",
            "edge_type",
            "layer",
            "confidence",
            "created_by",
        }
    )

    # Recommended but not required (warnings, not errors)
    node_recommended: frozenset[str] = frozenset(
        {
            "tags",
            "provenance",
        }
    )

    edge_recommended: frozenset[str] = frozenset(
        {
            "confidence",
        }
    )


# ── Contract Configuration ──────────────────────────────────────────────────


@dataclass
class ContractConfig:
    """The full contract for the knowledge graph.

    Combines SchemaConfig (what types are valid) with NamingConventions
    (how to name things) and RequiredFields (what metadata must exist).

    This is the "constitution" — the contract between agents and the
    shared memory layer that ensures consistency at scale.
    """

    schema: SchemaConfig = field(default_factory=SchemaConfig)
    naming: NamingConventions = field(default_factory=NamingConventions)
    required: RequiredFields = field(default_factory=RequiredFields)

    # Agent-specific contract extensions
    # Per-agent naming overrides or additional required fields
    agent_contracts: dict[str, dict] = field(default_factory=dict)

    def for_agent(self, agent_name: str) -> "ContractConfig":
        """Return a contract with agent-specific extensions applied."""
        if agent_name not in self.agent_contracts:
            return self

        ext = self.agent_contracts[agent_name]
        # Merge agent extensions into a new contract
        return ContractConfig(
            schema=self.schema,
            naming=self.naming,
            required=self.required,
            agent_contracts=self.agent_contracts,
            _agent_extensions=ext,  # type: ignore
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the full contract configuration."""
        return {
            "schema": self.schema.to_dict(),
            "naming": {
                "node_id_pattern": self.naming.node_id_pattern,
                "type_prefix_required": self.naming.type_prefix_required,
                "type_prefixes": self.naming.type_prefixes,
                "label_min_length": self.naming.label_min_length,
                "content_min_length": self.naming.content_min_length,
                "edge_type_pattern": self.naming.edge_type_pattern,
            },
            "required": {
                "node_required": sorted(self.required.node_required),
                "knowledge_required": sorted(self.required.knowledge_required),
                "task_required": sorted(self.required.task_required),
                "source_required": sorted(self.required.source_required),
                "edge_required": sorted(self.required.edge_required),
                "causal_edge_required": sorted(self.required.causal_edge_required),
                "node_recommended": sorted(self.required.node_recommended),
                "edge_recommended": sorted(self.required.edge_recommended),
            },
            "agent_contracts": list(self.agent_contracts.keys()),
        }


# ── Lint Engine ─────────────────────────────────────────────────────────────


@dataclass
class LintViolation:
    """A single contract violation found during linting."""

    entity_type: str  # "node" or "edge"
    entity_id: str  # Node ID or edge ID
    rule: str  # Rule that was violated
    severity: str  # "error" (required) or "warning" (recommended)
    message: str  # Human-readable description
    field: str | None = None  # Specific field that's wrong


def lint_node(
    node: dict[str, Any],
    contract: ContractConfig,
) -> list[LintViolation]:
    """Lint a single node against the contract.

    Args:
        node: Node record from the database.
        contract: Contract configuration.

    Returns:
        List of violations (empty if node passes all checks).
    """
    violations = []
    node_id = node.get("id", "<unknown>")
    node_type = node.get("type", "")

    # 1. Required fields
    required_fields = contract.required.node_required
    if node_type in ("concept", "pattern"):
        required_fields = required_fields | contract.required.knowledge_required
    elif node_type == "task":
        required_fields = required_fields | contract.required.task_required
    elif node_type == "source":
        required_fields = required_fields | contract.required.source_required

    for req_field in required_fields:
        value = node.get(req_field)
        if value is None or value == "":
            violations.append(
                LintViolation(
                    entity_type="node",
                    entity_id=node_id,
                    rule="required_field",
                    severity="error",
                    message=f"Missing required field: {req_field}",
                    field=req_field,
                )
            )

    # 2. Recommended fields (warnings only)
    for rec_field in contract.required.node_recommended:
        value = node.get(rec_field)
        if value is None or value == "":
            violations.append(
                LintViolation(
                    entity_type="node",
                    entity_id=node_id,
                    rule="recommended_field",
                    severity="warning",
                    message=f"Missing recommended field: {rec_field}",
                    field=rec_field,
                )
            )

    # 3. Node ID naming convention
    if contract.naming.type_prefix_required and node_type in contract.naming.type_prefixes:
        expected_prefix = contract.naming.type_prefixes[node_type]
        if not node_id.startswith(expected_prefix):
            violations.append(
                LintViolation(
                    entity_type="node",
                    entity_id=node_id,
                    rule="type_prefix",
                    severity="warning",
                    message=f"Node ID '{node_id}' should start with '{expected_prefix}' for type '{node_type}'",
                    field="id",
                )
            )

    # 4. Node ID format
    if not re.match(contract.naming.node_id_pattern, node_id):
        violations.append(
            LintViolation(
                entity_type="node",
                entity_id=node_id,
                rule="node_id_format",
                severity="warning",
                message=f"Node ID '{node_id}' doesn't match pattern '{contract.naming.node_id_pattern}'",
                field="id",
            )
        )

    # 5. Label length
    label = node.get("label", "")
    if len(label) < contract.naming.label_min_length:
        violations.append(
            LintViolation(
                entity_type="node",
                entity_id=node_id,
                rule="label_length",
                severity="warning",
                message=f"Label too short ({len(label)} chars, min {contract.naming.label_min_length})",
                field="label",
            )
        )

    # 6. Content length for knowledge nodes
    if node_type in ("concept", "pattern"):
        content = node.get("content", "") or ""
        if len(content) < contract.naming.content_min_length:
            violations.append(
                LintViolation(
                    entity_type="node",
                    entity_id=node_id,
                    rule="content_length",
                    severity="warning",
                    message=f"Content too short ({len(content)} chars, min {contract.naming.content_min_length}) for {node_type} node",
                    field="content",
                )
            )

    # 7. Confidence bounds
    confidence = node.get("confidence")
    if confidence is not None:
        try:
            c = float(confidence)
            if c < 0.0 or c > 1.0:
                violations.append(
                    LintViolation(
                        entity_type="node",
                        entity_id=node_id,
                        rule="confidence_bounds",
                        severity="error",
                        message=f"Confidence {c} outside [0.0, 1.0]",
                        field="confidence",
                    )
                )
        except (TypeError, ValueError):
            violations.append(
                LintViolation(
                    entity_type="node",
                    entity_id=node_id,
                    rule="confidence_type",
                    severity="error",
                    message=f"Confidence '{confidence}' is not a number",
                    field="confidence",
                )
            )

    # 8. Valid node type
    if not contract.schema.validate_node_type(node_type):
        violations.append(
            LintViolation(
                entity_type="node",
                entity_id=node_id,
                rule="valid_node_type",
                severity="error",
                message=f"Unknown node type: '{node_type}'",
                field="type",
            )
        )

    # 9. Valid provenance
    provenance = node.get("provenance")
    if provenance and provenance not in contract.schema.provenances:
        violations.append(
            LintViolation(
                entity_type="node",
                entity_id=node_id,
                rule="valid_provenance",
                severity="warning",
                message=f"Unknown provenance: '{provenance}'",
                field="provenance",
            )
        )

    return violations


def lint_edge(
    edge: dict[str, Any],
    contract: ContractConfig,
) -> list[LintViolation]:
    """Lint a single edge against the contract."""
    violations = []
    edge_id = edge.get("id", "<unknown>")
    edge_type = edge.get("edge_type", "")
    layer = edge.get("layer", "")

    # 1. Required fields
    required_fields = contract.required.edge_required
    if edge_type in ("CAUSES", "DEPENDS_ON", "THREATENS", "EXPECTED_LIKELIHOOD"):
        required_fields = required_fields | contract.required.causal_edge_required

    for req_field in required_fields:
        value = edge.get(req_field)
        if value is None or value == "":
            violations.append(
                LintViolation(
                    entity_type="edge",
                    entity_id=edge_id,
                    rule="required_field",
                    severity="error",
                    message=f"Missing required field: {req_field}",
                    field=req_field,
                )
            )

    # 2. Recommended fields
    for rec_field in contract.required.edge_recommended:
        value = edge.get(rec_field)
        if value is None or value == "":
            violations.append(
                LintViolation(
                    entity_type="edge",
                    entity_id=edge_id,
                    rule="recommended_field",
                    severity="warning",
                    message=f"Missing recommended field: {rec_field}",
                    field=rec_field,
                )
            )

    # 3. Valid edge type for layer
    if layer and edge_type:
        if not contract.schema.validate_edge_type(layer, edge_type):
            violations.append(
                LintViolation(
                    entity_type="edge",
                    entity_id=edge_id,
                    rule="edge_type_for_layer",
                    severity="error",
                    message=f"Edge type '{edge_type}' not valid for layer '{layer}'",
                    field="edge_type",
                )
            )

    # 4. Confidence bounds
    confidence = edge.get("confidence")
    if confidence is not None:
        try:
            c = float(confidence)
            if c < 0.0 or c > 1.0:
                violations.append(
                    LintViolation(
                        entity_type="edge",
                        entity_id=edge_id,
                        rule="confidence_bounds",
                        severity="error",
                        message=f"Confidence {c} outside [0.0, 1.0]",
                        field="confidence",
                    )
                )
        except (TypeError, ValueError):
            violations.append(
                LintViolation(
                    entity_type="edge",
                    entity_id=edge_id,
                    rule="confidence_type",
                    severity="error",
                    message=f"Confidence '{confidence}' is not a number",
                    field="confidence",
                )
            )

    # 5. Probability bounds
    probability = edge.get("probability")
    if probability is not None:
        try:
            p = float(probability)
            if p < 0.0 or p > 1.0:
                violations.append(
                    LintViolation(
                        entity_type="edge",
                        entity_id=edge_id,
                        rule="probability_bounds",
                        severity="error",
                        message=f"Probability {p} outside [0.0, 1.0]",
                        field="probability",
                    )
                )
        except (TypeError, ValueError):
            violations.append(
                LintViolation(
                    entity_type="edge",
                    entity_id=edge_id,
                    rule="probability_type",
                    severity="error",
                    message=f"Probability '{probability}' is not a number",
                    field="probability",
                )
            )

    return violations


def lint_graph(
    conn,
    contract: ContractConfig | None = None,
    *,
    limit: int = 1000,
    node_types: list[str] | None = None,
) -> dict[str, Any]:
    """Lint the entire knowledge graph against the contract.

    Validates all nodes and edges for naming conventions, required fields,
    confidence bounds, and type validity. Returns a structured report.

    Args:
        conn: DuckDB connection.
        contract: Contract configuration (default: standard OHM contract).
        limit: Maximum entities to check per type.
        node_types: Filter to specific node types.

    Returns:
        Dict with violations, summary, and contract info.
    """
    if contract is None:
        contract = ContractConfig()

    all_violations: list[LintViolation] = []

    # Lint nodes
    type_filter = ""
    if node_types:
        placeholders = ",".join(["?"] * len(node_types))
        type_filter = f"AND type IN ({placeholders})"

    node_query = f"""
        SELECT id, label, type, content, confidence, provenance, tags,
               created_by, priority, task_status, assigned_to, visibility
        FROM ohm_nodes
        WHERE deleted_at IS NULL
        {type_filter}
        ORDER BY created_at DESC
        LIMIT ?
    """
    params: list[Any] = []
    if node_types:
        params.extend(node_types)
    params.append(limit)

    rows = conn.execute(node_query, params).fetchall()
    for row in rows:
        node = {
            "id": row[0],
            "label": row[1],
            "type": row[2],
            "content": row[3],
            "confidence": row[4],
            "provenance": row[5],
            "tags": row[6],
            "created_by": row[7],
            "priority": row[8],
            "task_status": row[9],
            "assigned_to": row[10],
            "visibility": row[11],
        }
        all_violations.extend(lint_node(node, contract))

    # Lint edges
    edge_query = """
        SELECT id, from_node, to_node, edge_type, layer, confidence,
               probability, created_by
        FROM ohm_edges
        WHERE deleted_at IS NULL
        ORDER BY created_at DESC
        LIMIT ?
    """
    edge_rows = conn.execute(edge_query, [limit]).fetchall()
    for row in edge_rows:
        edge = {
            "id": row[0],
            "from_node": row[1],
            "to_node": row[2],
            "edge_type": row[3],
            "layer": row[4],
            "confidence": row[5],
            "probability": row[6],
            "created_by": row[7],
        }
        all_violations.extend(lint_edge(edge, contract))

    # Build summary
    errors = [v for v in all_violations if v.severity == "error"]
    warnings = [v for v in all_violations if v.severity == "warning"]

    # Group by rule
    by_rule: dict[str, int] = {}
    for v in all_violations:
        by_rule[v.rule] = by_rule.get(v.rule, 0) + 1

    # Group by entity type
    by_entity: dict[str, int] = {}
    for v in all_violations:
        key = v.entity_type
        by_entity[key] = by_entity.get(key, 0) + 1

    return {
        "total_violations": len(all_violations),
        "errors": len(errors),
        "warnings": len(warnings),
        "by_rule": dict(sorted(by_rule.items(), key=lambda x: -x[1])),
        "by_entity": by_entity,
        "violations": [
            {
                "entity_type": v.entity_type,
                "entity_id": v.entity_id,
                "rule": v.rule,
                "severity": v.severity,
                "message": v.message,
                "field": v.field,
            }
            for v in all_violations[:100]  # Limit output
        ],
        "contract": contract.to_dict(),
        "checked": {
            "nodes": len(rows),
            "edges": len(edge_rows),
        },
        "pass": len(errors) == 0,
    }
