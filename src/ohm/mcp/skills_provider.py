"""OHM Skills Provider — core and domain skills as MCP resources (OHM-849).

Exposes skill directories as MCP resources with ``skill://ohm/{name}/SKILL.md``
URIs. Each skill is a directory containing a ``SKILL.md`` plus optional
supporting files. A ``_manifest`` resource lists files, sizes, and SHA256
hashes for caching and update detection.

Core skills ship with OHM and apply to every deployment. Domain skills
are loaded from a tenant-configured path.

Skill evolution loop (generator→executor→evaluator→promotion) is
documented here and sequenced after #845-#848.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any


SKILL_URI_SCHEME = "skill"
SKILL_URI_PREFIX = "skill://ohm/"


class OhmSkillsProvider:
    """Serves skill directories as MCP resources.

    Args:
        core_skills_dir: Path to core skills directory (read-only, ships with OHM).
        domain_skills_dir: Optional path to domain/tenant skills directory.
    """

    def __init__(
        self,
        core_skills_dir: str | Path | None = None,
        domain_skills_dir: str | Path | None = None,
    ) -> None:
        if core_skills_dir is None:
            core_skills_dir = Path(__file__).parent.parent / "skills"
        self.core_dir = Path(core_skills_dir)
        self.domain_dir = Path(domain_skills_dir) if domain_skills_dir else None

    def list_resources(self) -> list[dict[str, Any]]:
        """List all available skill resources.

        Returns:
            List of resource dicts with uri, name, description, and mimeType.
        """
        resources: list[dict[str, Any]] = []
        for skill_name, skill_dir in self._iter_skill_dirs():
            uri = f"{SKILL_URI_PREFIX}{skill_name}/SKILL.md"
            skill_path = skill_dir / "SKILL.md"
            if skill_path.exists():
                resources.append({
                    "uri": uri,
                    "name": f"ohm-skill-{skill_name}",
                    "description": f"OHM skill: {skill_name}",
                    "mimeType": "text/markdown",
                })
            manifest_uri = f"{SKILL_URI_PREFIX}{skill_name}/_manifest"
            resources.append({
                "uri": manifest_uri,
                "name": f"ohm-skill-{skill_name}-manifest",
                "description": f"Manifest for OHM skill: {skill_name}",
                "mimeType": "application/json",
            })
        return resources

    def read_resource(self, uri: str) -> str:
        """Read a skill resource by URI.

        Args:
            uri: The skill:// URI to read.

        Returns:
            The resource content as a string.

        Raises:
            ValueError: If the URI is not a valid skill resource.
        """
        if not uri.startswith(SKILL_URI_PREFIX):
            raise ValueError(f"Unsupported URI scheme: {uri}")

        path_part = uri[len(SKILL_URI_PREFIX):]
        parts = path_part.split("/", 1)
        if len(parts) < 2:
            raise ValueError(f"Invalid skill URI: {uri}")

        skill_name = parts[0]
        resource_file = parts[1]

        skill_dir = self._find_skill_dir(skill_name)
        if skill_dir is None:
            raise ValueError(f"Skill not found: {skill_name}")

        if resource_file == "_manifest":
            return self._generate_manifest(skill_name, skill_dir)
        else:
            file_path = skill_dir / resource_file
            if not file_path.exists() or not file_path.is_file():
                raise ValueError(f"Resource not found: {resource_file}")
            if not str(file_path.resolve()).startswith(str(skill_dir.resolve())):
                raise ValueError(f"Path traversal detected: {resource_file}")
            return file_path.read_text(encoding="utf-8")

    def get_manifest(self, skill_name: str) -> dict[str, Any]:
        """Get the manifest for a skill.

        Args:
            skill_name: The skill directory name.

        Returns:
            Manifest dict with files, sizes, and SHA256 hashes.
        """
        skill_dir = self._find_skill_dir(skill_name)
        if skill_dir is None:
            raise ValueError(f"Skill not found: {skill_name}")
        return self._build_manifest(skill_name, skill_dir)

    def _iter_skill_dirs(self) -> list[tuple[str, Path]]:
        """Yield (skill_name, skill_dir) for all available skills."""
        skills: list[tuple[str, Path]] = []
        if self.core_dir.exists():
            for entry in sorted(self.core_dir.iterdir()):
                if entry.is_dir() and (entry / "SKILL.md").exists():
                    skills.append((entry.name, entry))
        if self.domain_dir and self.domain_dir.exists():
            for entry in sorted(self.domain_dir.iterdir()):
                if entry.is_dir() and (entry / "SKILL.md").exists():
                    skills.append((entry.name, entry))
        return skills

    def _find_skill_dir(self, skill_name: str) -> Path | None:
        """Find a skill directory by name, checking core then domain."""
        core_path = self.core_dir / skill_name
        if core_path.exists() and (core_path / "SKILL.md").exists():
            return core_path
        if self.domain_dir:
            domain_path = self.domain_dir / skill_name
            if domain_path.exists() and (domain_path / "SKILL.md").exists():
                return domain_path
        return None

    def _build_manifest(self, skill_name: str, skill_dir: Path) -> dict[str, Any]:
        """Build a manifest dict for a skill directory."""
        files: list[dict[str, Any]] = []
        for entry in sorted(skill_dir.rglob("*")):
            if entry.is_file() and not entry.name.startswith("."):
                rel = entry.relative_to(skill_dir)
                content = entry.read_bytes()
                files.append({
                    "path": str(rel).replace("\\", "/"),
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                })
        return {
            "skill_name": skill_name,
            "files": files,
            "file_count": len(files),
        }

    def _generate_manifest(self, skill_name: str, skill_dir: Path) -> str:
        """Generate the manifest as a JSON string."""
        import json
        return json.dumps(self._build_manifest(skill_name, skill_dir), indent=2)


def get_default_core_skills_dir() -> Path:
    """Return the default core skills directory path."""
    return Path(__file__).parent.parent / "skills"


def ensure_core_skills_exist() -> bool:
    """Ensure the core skills directory exists with at least the required skills.

    Creates the directory and minimum skill files if they don't exist.

    Returns:
        True if skills were created, False if they already existed.
    """
    skills_dir = get_default_core_skills_dir()
    if skills_dir.exists() and any(s.is_dir() for s in skills_dir.iterdir()):
        return False

    skills_dir.mkdir(parents=True, exist_ok=True)

    decision_skill = skills_dir / "decision-node" / "SKILL.md"
    decision_skill.parent.mkdir(parents=True, exist_ok=True)
    decision_skill.write_text("""# Skill: Decision Node

## When to use
Create a `decision` node when you need to choose between actions and the
choice depends on uncertain hypotheses.

## Required fields
- `utility_scale`: One of `best` (1.0), `neutral` (0.5), `worst` (0.0),
  or a numeric value 0-1.
- `action_alternatives`: JSON array of action names (e.g. `["build", "wait"]`).
- `current_best_action`: The currently recommended action.

## Linking hypotheses
Use `DECISION_DEPENDS_ON` edges (L3) to link the decision to hypothesis
nodes. The recommendation engine reads these edges to compute confidence
and suggest the best action.

## Autoresearch
Run `POST /decision/{id}/autoresearch` to automatically discover and
evaluate candidate hypothesis edges.

## Verification
Record outcomes on hypotheses via `record_outcome()`. The recommendation
engine weights verified hypotheses higher than untested ones.
""", encoding="utf-8")

    causal_skill = skills_dir / "causal-edge" / "SKILL.md"
    causal_skill.parent.mkdir(parents=True, exist_ok=True)
    causal_skill.write_text("""# Skill: Causal Edge

## When to use
Use causal edge types (`CAUSES`, `DEPENDS_ON`, `THREATENS`, `ENABLES`,
`INFLUENCES`) when the relationship represents a mechanistic or
probabilistic dependency that the Bayesian inference network should
traverse.

## Non-causal edges
Edges like `SUPPORTS`, `REFERENCES`, `MENTIONS`, `CONTAINS` do NOT flow
through the Bayesian network. Use them for structural relationships, not
causal claims.

## ADR-008: Two-stage sampling
Monte Carlo cascade simulation uses two-stage sampling:
1. Edge existence: sample `random() < confidence`
2. Effect propagation: sample `random() < probability`

Set both `confidence` (belief the edge exists) and `probability`
(likelihood the effect propagates) on causal edges.

## Cross-link requirement (ADR-018)
Synthesis-like nodes (pattern, idea, task, decision) must reference at
least one existing node via `connects_to` when created.
""", encoding="utf-8")

    observation_skill = skills_dir / "observation-recording" / "SKILL.md"
    observation_skill.parent.mkdir(parents=True, exist_ok=True)
    observation_skill.write_text("""# Skill: Observation Recording

## When to use
Record observations on nodes or edges to capture measurements, outcomes,
or assessments. Observations feed the verification and confidence decay
systems.

## Required fields
- `source_url`: Provenance URL for the observation (ADR-013).
- `sigma`: Uncertainty/standard deviation of the measurement.

## Observation types
- `measurement`: A quantitative reading.
- `experiment_result`: Outcome of a simulation or experiment.
- `assessment`: A qualitative evaluation.
- `forecast`: A forward-looking prediction (OHM-841).
- `pattern`: A detected pattern in data.

## Verification
Unverified edges decay with a 30-day half-life. Verified edges (with
recorded outcomes) decay with a 365-day half-life. Record outcomes via
`record_outcome(source_agent, claim_node, outcome)`.
""", encoding="utf-8")

    challenge_skill = skills_dir / "challenge-support" / "SKILL.md"
    challenge_skill.parent.mkdir(parents=True, exist_ok=True)
    challenge_skill.write_text("""# Skill: Challenge and Support

## When to challenge
Challenge an edge when you have evidence that contradicts it. Use
`CHALLENGED_BY` edges with a `reason` and `confidence` reflecting your
certainty.

## When to support
Use `SUPPORTS` edges to add corroborating evidence. High-confidence
support edges increase the target edge's compound confidence.

## NEGATES vs CHALLENGED_BY (ADR-009)
- `CHALLENGED_BY`: Expresses doubt — the challenged edge may still be
  partially correct.
- `NEGATES`: Expresses contradiction — the negated edge is wrong.

## Oppositional review
The system automatically flags CAUSES edges with homogeneous source_tier
or agent support for oppositional review. Check `oppositional_review` in
synthesis responses.
""", encoding="utf-8")

    ingest_skill = skills_dir / "ingest-document" / "SKILL.md"
    ingest_skill.parent.mkdir(parents=True, exist_ok=True)
    ingest_skill.write_text("""# Skill: Ingest Document

## When to use
Use the document ingestion pipeline to convert external documents
(PDFs, web pages, text) into source nodes with extracted claims.

## Pipeline
1. Ingest the document via `POST /documents/ingest`.
2. The pipeline extracts claims, entities, and relationships.
3. Review the resulting nodes and edges.
4. Link extracted claims to existing graph structure (ADR-018).

## Source tiers (ADR-028)
- `raw`: Unprocessed data, confidence ceiling 0.3.
- `unverified`: Single source, ceiling 0.5.
- `preliminary`: Early analysis, ceiling 0.7.
- `official`: Published but not peer-reviewed, ceiling 0.85.
- `verified`: Peer-reviewed or confirmed, ceiling 1.0.
""", encoding="utf-8")

    return True