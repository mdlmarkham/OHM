#"""Hypothesis-tree query helpers for OHM.

Implements the query patterns defined in OHM-ss22:

    1. find_active_hypotheses — Filter hypotheses by project and status
    2. experiments_for_node — Find experiments that evidence a node
    3. experiments_for_edge — Find experiments that evidence an edge
    4. best_verified_artifact_path — Pick the highest-scoring verified artifact
    5. propagate_experiment_result — Update ancestor confidence from child evidence

All functions use standard SQL recursive CTEs and follow the existing
query patterns in src/ohm/graph/queries/__init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def _rows_to_dicts(result: Any) -> list[dict[str, Any]]:
    """Convert DuckDB query result to list of dicts using column descriptions."""
    if not result:
        return []
    columns = [desc[0] for desc in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def find_active_hypotheses(
    conn: DuckDBPyConnection,
    *,
    project_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Find active hypothesis nodes.

    Returns all hypothesis nodes that are not pruned or superseded.
    Optionally filters by project_id and/or status.

    Args:
        conn: Database connection.
        project_id: Optional project identifier to filter by.
        status: Optional status to filter by (e.g., 'proposed', 'tested', 'verified').

    Returns:
        List of hypothesis node records, each as a dict.
    """
    from ohm.validation import validate_identifier

    conditions: list[str] = []
    params: list[Any] = []

    # Base filter: hypothesis type and not pruned/superseded
    conditions.append("n.type = 'hypothesis'")
    conditions.append("n.deleted_at IS NULL")
    conditions.append("(n.hypothesis_status IS NULL OR n.hypothesis_status NOT IN ('pruned', 'superseded'))")

    if project_id is not None:
        project_id = validate_identifier(project_id, name="project_id")
        conditions.append("n.project_id = ?")
        params.append(project_id)

    if status is not None:
        status = validate_identifier(status, name="status")
        conditions.append("n.hypothesis_status = ?")
        params.append(status)

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    query = f"""
        SELECT
            n.id, n.label, n.type, n.content, n.created_by, n.created_at,
            n.hypothesis_status, n.project_id, n.parent_hypothesis_id,
            n.artifact_ref, n.dev_metric, n.test_metric,
            n.confidence, n.visibility, n.provenance, n.tags, n.metadata
        FROM ohm_nodes n
        {where_clause}
        ORDER BY n.created_at DESC
    """

    result = conn.execute(query, params)
    return _rows_to_dicts(result)


def experiments_for_node(
    conn: DuckDBPyConnection,
    node_id: str,
) -> list[dict[str, Any]]:
    """Find experiments that provide evidence for a node.

    Returns all experiment nodes that have SUPPORTS_EVIDENCE or
    CONTRADICTS_EVIDENCE edges pointing to the given node.

    Args:
        conn: Database connection.
        node_id: The node ID to find evidence for.

    Returns:
        List of experiment node records with their evidence edges.
    """
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    query = """
        SELECT DISTINCT
            n.id, n.label, n.type, n.content, n.created_by, n.created_at,
            n.artifact_ref, n.dev_metric, n.test_metric,
            n.confidence, n.visibility, n.provenance, n.tags, n.metadata,
            e.id AS edge_id, e.edge_type, e.confidence AS edge_confidence,
            e.created_by AS edge_created_by, e.created_at AS edge_created_at
        FROM ohm_nodes n
        JOIN ohm_edges e ON e.from_node = n.id
        WHERE n.type = 'experiment'
          AND n.deleted_at IS NULL
          AND e.deleted_at IS NULL
          AND e.to_node = ?
          AND e.edge_type IN ('SUPPORTS_EVIDENCE', 'CONTRADICTS_EVIDENCE')
        ORDER BY n.created_at DESC
    """

    result = conn.execute(query, [node_id])
    return _rows_to_dicts(result)


def experiments_for_edge(
    conn: DuckDBPyConnection,
    edge_id: str,
) -> list[dict[str, Any]]:
    """Find experiments that provide evidence for an edge.

    Returns all experiment nodes that have SUPPORTS_EVIDENCE or
    CONTRADICTS_EVIDENCE edges pointing to the given edge.

    Args:
        conn: Database connection.
        edge_id: The edge ID to find evidence for.

    Returns:
        List of experiment node records with their evidence edges.

    Raises:
        NotImplementedError: If edge support is not yet implemented in the schema.
    """
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")

    # Check if the schema supports edge observations
    # This would require a join through ohm_observations.edge_id
    obs_table = conn.execute(
        "SELECT name FROM duckdb_tables() WHERE name = 'ohm_observations'"
    ).fetchone()

    if not obs_table:
        raise NotImplementedError(
            "Edge observations not supported: ohm_observations table missing or does not have edge_id column"
        )

    # Check if edge_id column exists
    columns = conn.execute(
        "SELECT column_name FROM duckdb_columns() WHERE table_name = 'ohm_observations' AND column_name = 'edge_id'"
    ).fetchone()

    if not columns:
        raise NotImplementedError(
            "Edge observations not supported: ohm_observations.edge_id column missing"
        )

    query = """
        SELECT DISTINCT
            n.id, n.label, n.type, n.content, n.created_by, n.created_at,
            n.artifact_ref, n.dev_metric, n.test_metric,
            n.confidence, n.visibility, n.provenance, n.tags, n.metadata,
            e.id AS edge_id, e.edge_type, e.confidence AS edge_confidence,
            e.created_by AS edge_created_by, e.created_at AS edge_created_at
        FROM ohm_nodes n
        JOIN ohm_edges e ON e.from_node = n.id
        JOIN ohm_observations o ON o.edge_id = e.id
        WHERE n.type = 'experiment'
          AND n.deleted_at IS NULL
          AND e.deleted_at IS NULL
          AND o.deleted_at IS NULL
          AND o.edge_id = ?
          AND e.edge_type IN ('SUPPORTS_EVIDENCE', 'CONTRADICTS_EVIDENCE')
        ORDER BY n.created_at DESC
    """

    result = conn.execute(query, [edge_id])
    return _rows_to_dicts(result)


def best_verified_artifact_path(
    conn: DuckDBPyConnection,
    hypothesis_id: str,
) -> dict[str, Any] | None:
    """Find the best verified artifact for a hypothesis.

    Follows TESTS edges from the hypothesis to experiments, and returns
    the experiment with the highest test_metric where hypothesis_status='verified'.

    Args:
        conn: Database connection.
        hypothesis_id: The hypothesis ID to find artifacts for.

    Returns:
        Dict containing experiment_id, artifact_ref, test_metric, and dev_metric,
        or None if no verified artifacts exist.
    """
    from ohm.validation import validate_identifier

    hypothesis_id = validate_identifier(hypothesis_id, name="hypothesis_id")

    query = """
        SELECT
            e.from_node AS experiment_id,
            n.artifact_ref,
            n.test_metric,
            n.dev_metric
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = e.from_node
        WHERE e.to_node = ?
          AND e.edge_type = 'TESTS'
          AND e.deleted_at IS NULL
          AND n.deleted_at IS NULL
          AND n.type = 'experiment'
          AND n.hypothesis_status = 'verified'
        ORDER BY n.test_metric DESC
        LIMIT 1
    """

    result = conn.execute(query, [hypothesis_id])
    row = result.fetchone()

    if not row:
        return None

    return {
        "experiment_id": row[0],
        "artifact_ref": row[1],
        "test_metric": row[2],
        "dev_metric": row[3],
    }


def propagate_experiment_result(
    conn: DuckDBPyConnection,
    parent_hypothesis_id: str,
) -> dict[str, Any]:
    """Update ancestor hypothesis confidence from child experiment results.

    Walks REFINES edges from child to parent hypotheses, averaging the
    test_metric values of all child experiments to compute a new
    confidence for the parent hypothesis.

    Args:
        conn: Database connection.
        parent_hypothesis_id: The parent hypothesis to update.

    Returns:
        Dict containing the updated hypothesis record and statistics about
        the propagation (child_count, average_test_metric, old_confidence, new_confidence).
    """
    from ohm.validation import validate_identifier

    parent_hypothesis_id = validate_identifier(
        parent_hypothesis_id, name="parent_hypothesis_id"
    )

    # Find all child hypotheses via REFINES edges
    child_query = """
        WITH RECURSIVE child_hypotheses AS (
            SELECT
                e.to_node AS hypothesis_id,
                1 AS depth
            FROM ohm_edges e
            WHERE e.from_node = ?
              AND e.edge_type = 'REFINES'
              AND e.deleted_at IS NULL

            UNION ALL

            SELECT
                e.to_node AS hypothesis_id,
                ch.depth + 1 AS depth
            FROM child_hypotheses ch
            JOIN ohm_edges e ON e.from_node = ch.hypothesis_id
            WHERE e.edge_type = 'REFINES'
              AND e.deleted_at IS NULL
              AND ch.depth < 10  -- Prevent infinite recursion
        )
        SELECT DISTINCT hypothesis_id
        FROM child_hypotheses
    """

    child_result = conn.execute(child_query, [parent_hypothesis_id])
    child_ids = [row[0] for row in child_result.fetchall()]

    if not child_ids:
        return {
            "status": "no_children",
            "parent_hypothesis_id": parent_hypothesis_id,
            "child_count": 0,
        }

    # Find all experiments that TEST the child hypotheses
    placeholders = ",".join(["?"] * len(child_ids))
    experiment_query = f"""
        SELECT
            e.from_node AS experiment_id,
            n.test_metric
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = e.from_node
        WHERE e.to_node IN ({placeholders})
          AND e.edge_type = 'TESTS'
          AND e.deleted_at IS NULL
          AND n.deleted_at IS NULL
          AND n.type = 'experiment'
          AND n.test_metric IS NOT NULL
    """

    experiment_result = conn.execute(experiment_query, child_ids)
    experiment_metrics = [row[1] for row in experiment_result.fetchall()]

    if not experiment_metrics:
        return {
            "status": "no_experiments",
            "parent_hypothesis_id": parent_hypothesis_id,
            "child_count": len(child_ids),
        }

    # Calculate new confidence as the average of child test_metrics
    average_test_metric = sum(experiment_metrics) / len(experiment_metrics)
    new_confidence = min(1.0, average_test_metric)  # Cap at 1.0

    # Get current parent hypothesis record
    parent_query = """
        SELECT id, label, confidence
        FROM ohm_nodes
        WHERE id = ? AND deleted_at IS NULL
    """

    parent_result = conn.execute(parent_query, [parent_hypothesis_id])
    parent_row = parent_result.fetchone()

    if not parent_row:
        return {
            "status": "parent_not_found",
            "parent_hypothesis_id": parent_hypothesis_id,
        }

    old_confidence = parent_row[2]

    # Update parent hypothesis confidence
    update_query = """
        UPDATE ohm_nodes
        SET confidence = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """

    conn.execute(update_query, [new_confidence, parent_hypothesis_id])

    # Return updated parent record
    updated_parent = _rows_to_dicts(
        conn.execute(
            "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [parent_hypothesis_id],
        )
    )[0]

    return {
        "status": "updated",
        "parent_hypothesis": updated_parent,
        "child_count": len(child_ids),
        "average_test_metric": average_test_metric,
        "old_confidence": old_confidence,
        "new_confidence": new_confidence,
    }
