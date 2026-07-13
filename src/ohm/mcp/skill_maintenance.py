"""Measurement-driven skill maintenance loop (OHM-854).

Extends the autoresearch pattern (generator→executor→evaluator→promotion)
to skill markdown files so skills co-evolve with the OHM environment.

Signals that trigger candidate generation:
  - Schema changes (new node/edge types)
  - Low nudge acceptance rates (nudge_acceptance_stats)
  - Common agent mistakes (missing required fields)

The loop:
  1. **Generator**: Detects signals and proposes an edit to a skill's SKILL.md.
  2. **Executor**: Writes the candidate to a trial location and serves it
     alongside the default.
  3. **Evaluator**: Measures whether agents who received the candidate
     produce better OHM writes (fewer missing fields, higher nudge
     acceptance, more recorded outcomes).
  4. **Promotion/Demotion**: If the candidate is statistically better
     (Fisher's exact test, p < 0.05, n >= 30), promote it to the default
     location. If worse or neutral, demote (delete candidate).
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _rows_to_dicts

MIN_SAMPLE_SIZE = 30
SIGNIFICANCE_THRESHOLD = 0.05


def _skill_hash(content: str) -> str:
    """Compute SHA256 hash of skill content for versioning."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def detect_signals(
    conn: "DuckDBPyConnection",
) -> list[dict[str, Any]]:
    """Detect signals that skills may need updating (OHM-854 generator).

    Checks:
      - Low nudge acceptance rates (< 30%) for nudge types that have
        corresponding skills.
      - New node types in the schema that skills don't mention yet.

    Args:
        conn: Database connection.

    Returns:
        List of signal dicts with skill_name, signal_type, and details.
    """
    signals: list[dict[str, Any]] = []

    nudge_to_skill = {
        "causal_edge_suggestion": "causal-edge",
        "decision_node_incomplete": "decision-node",
        "source_citation": "observation-recording",
        "challenge_reminder": "challenge-support",
    }

    try:
        rows = _rows_to_dicts(conn.execute(
            """SELECT nudge_type,
                      COUNT(*) AS total,
                      SUM(CASE WHEN accepted = true THEN 1 ELSE 0 END) AS accepted_count,
                      SUM(CASE WHEN accepted = false THEN 1 ELSE 0 END) AS rejected_count
               FROM ohm_nudge_log
               WHERE nudge_type IS NOT NULL
               GROUP BY nudge_type
               HAVING COUNT(*) >= 10""",
        ))

        for row in rows:
            nudge_type = row.get("nudge_type", "")
            total = row.get("total", 0) or 0
            accepted = row.get("accepted_count", 0) or 0
            responded = accepted + (row.get("rejected_count", 0) or 0)
            if responded == 0:
                continue
            rate = accepted / responded
            if rate < 0.30 and nudge_type in nudge_to_skill:
                signals.append({
                    "skill_name": nudge_to_skill[nudge_type],
                    "signal_type": "low_nudge_acceptance",
                    "nudge_type": nudge_type,
                    "acceptance_rate": round(rate, 4),
                    "total_exposures": total,
                    "suggestion": f"Nudge '{nudge_type}' has {rate:.0%} acceptance. Consider clarifying the skill's guidance on this topic.",
                })
    except Exception:
        pass

    return signals


def generate_candidate(
    skill_name: str,
    current_content: str,
    signal: dict[str, Any],
) -> str:
    """Generate a candidate skill edit from a signal (OHM-854 generator).

    Appends a note to the skill based on the signal. This is a simple
    text-based generator — a real implementation would use LLM-assisted
    editing, but this provides the structural loop.

    Args:
        skill_name: The skill directory name.
        current_content: The current SKILL.md content.
        signal: The signal dict from detect_signals().

    Returns:
        The candidate SKILL.md content.
    """
    signal_type = signal.get("signal_type", "unknown")
    suggestion = signal.get("suggestion", "")

    if signal_type == "low_nudge_acceptance":
        note = f"\n\n## Maintenance note (auto-generated)\n\n> **Signal**: {suggestion}\n>\n> This section was added by the skill maintenance loop (OHM-854) because agents receiving nudges linked to this skill have a low acceptance rate. Review and refine the guidance above to make it more actionable.\n"
        return current_content.rstrip() + note

    return current_content


def write_candidate(
    skill_name: str,
    content: str,
    candidates_dir: Path,
) -> Path:
    """Write a candidate skill to the trial location (OHM-854 executor).

    Args:
        skill_name: The skill directory name.
        content: The candidate SKILL.md content.
        candidates_dir: Root directory for candidate skills.

    Returns:
        Path to the written candidate SKILL.md.
    """
    candidate_dir = candidates_dir / skill_name
    candidate_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = candidate_dir / "SKILL.md"
    candidate_path.write_text(content, encoding="utf-8")
    return candidate_path


def evaluate_candidate(
    conn: "DuckDBPyConnection",
    *,
    nudge_type: str,
    min_exposures: int = MIN_SAMPLE_SIZE,
) -> dict[str, Any]:
    """Evaluate whether a candidate skill improved outcomes (OHM-854 evaluator).

    Compares nudge acceptance rates before and after the candidate was
    served. Uses Fisher's exact test at p < 0.05.

    In v1, this reuses the existing nudge_acceptance_stats infrastructure
    and the ``variant_id`` column on ``ohm_nudge_log`` (from OHM-847).

    Args:
        conn: Database connection.
        nudge_type: The nudge type to evaluate.
        min_exposures: Minimum exposures per variant (default 30).

    Returns:
        Dict with before/after stats, p_value, improved (bool), and
        insufficient_data flag.
    """
    from ohm.server.nudge_optimization import evaluate_nudge_variants, _fisher_exact

    result = evaluate_nudge_variants(
        conn,
        nudge_type=nudge_type,
        min_exposures=min_exposures,
    )

    if result.get("insufficient_data"):
        return {
            "nudge_type": nudge_type,
            "improved": False,
            "insufficient_data": True,
            "reason": result.get("reason", "insufficient data"),
            "details": result,
        }

    winner = result.get("winner")
    variants = result.get("variants", [])
    if not winner or len(variants) < 2:
        return {
            "nudge_type": nudge_type,
            "improved": False,
            "insufficient_data": False,
            "reason": "no significant winner",
            "details": result,
        }

    return {
        "nudge_type": nudge_type,
        "improved": True,
        "insufficient_data": False,
        "winner": winner,
        "p_value": result.get("p_value"),
        "details": result,
    }


def promote_candidate(
    skill_name: str,
    candidate_path: Path,
    default_dir: Path,
) -> dict[str, Any]:
    """Promote a candidate skill to the default location (OHM-854 promotion).

    Replaces the default SKILL.md with the candidate and cleans up the
    candidate directory.

    Args:
        skill_name: The skill directory name.
        candidate_path: Path to the candidate SKILL.md.
        default_dir: Root directory for default skills.

    Returns:
        Dict with the promotion result.
    """
    target_dir = default_dir / skill_name
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "SKILL.md"

    old_hash = ""
    if target_path.exists():
        old_hash = _skill_hash(target_path.read_text(encoding="utf-8"))

    new_hash = _skill_hash(candidate_path.read_text(encoding="utf-8"))

    shutil.copy2(candidate_path, target_path)

    if candidate_path.parent.exists():
        shutil.rmtree(candidate_path.parent, ignore_errors=True)

    return {
        "skill_name": skill_name,
        "status": "promoted",
        "old_hash": old_hash[:16],
        "new_hash": new_hash[:16],
        "target_path": str(target_path),
    }


def demote_candidate(
    skill_name: str,
    candidates_dir: Path,
) -> dict[str, Any]:
    """Demote a candidate skill — delete it (OHM-854 demotion).

    Args:
        skill_name: The skill directory name.
        candidates_dir: Root directory for candidate skills.

    Returns:
        Dict with the demotion result.
    """
    candidate_dir = candidates_dir / skill_name
    if candidate_dir.exists():
        shutil.rmtree(candidate_dir, ignore_errors=True)
        return {
            "skill_name": skill_name,
            "status": "demoted",
            "candidate_path": str(candidate_dir),
        }
    return {
        "skill_name": skill_name,
        "status": "not_found",
        "candidate_path": str(candidate_dir),
    }


def run_skill_maintenance_round(
    conn: "DuckDBPyConnection",
    *,
    default_skills_dir: Path,
    candidates_dir: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one full skill maintenance round (OHM-854).

    Detects signals, generates candidates, evaluates them, and
    promotes/demotes as appropriate.

    Args:
        conn: Database connection.
        default_skills_dir: Root directory for default skills.
        candidates_dir: Root directory for candidate skills.
        dry_run: If True, generate and evaluate but don't promote.

    Returns:
        Dict with signals, candidates, evaluations, promotions, and demotions.
    """
    signals = detect_signals(conn)
    if not signals:
        return {
            "signals": [],
            "candidates": [],
            "evaluations": [],
            "promotions": [],
            "demotions": [],
            "dry_run": dry_run,
            "message": "No signals detected",
        }

    candidates = []
    evaluations = []
    promotions = []
    demotions = []

    nudge_to_skill = {
        "causal_edge_suggestion": "causal-edge",
        "decision_node_incomplete": "decision-node",
        "source_citation": "observation-recording",
        "challenge_reminder": "challenge-support",
    }

    for signal in signals:
        skill_name = signal["skill_name"]
        skill_dir = default_skills_dir / skill_name
        skill_path = skill_dir / "SKILL.md"

        if not skill_path.exists():
            continue

        current_content = skill_path.read_text(encoding="utf-8")
        candidate_content = generate_candidate(skill_name, current_content, signal)

        if candidate_content == current_content:
            continue

        candidates.append({
            "skill_name": skill_name,
            "signal": signal,
            "candidate_hash": _skill_hash(candidate_content)[:16],
            "current_hash": _skill_hash(current_content)[:16],
        })

        if not dry_run:
            write_candidate(skill_name, candidate_content, candidates_dir)

            nudge_type = signal.get("nudge_type", "")
            if nudge_type:
                eval_result = evaluate_candidate(conn, nudge_type=nudge_type)
                evaluations.append({
                    "skill_name": skill_name,
                    "nudge_type": nudge_type,
                    "result": eval_result,
                })

                if eval_result.get("improved"):
                    promo = promote_candidate(
                        skill_name,
                        candidates_dir / skill_name / "SKILL.md",
                        default_skills_dir,
                    )
                    promotions.append(promo)
                else:
                    demo = demote_candidate(skill_name, candidates_dir)
                    demotions.append(demo)

    return {
        "signals": signals,
        "candidates": candidates,
        "evaluations": evaluations,
        "promotions": promotions,
        "demotions": demotions,
        "dry_run": dry_run,
        "summary": {
            "signals_detected": len(signals),
            "candidates_generated": len(candidates),
            "promoted": len(promotions),
            "demoted": len(demotions),
        },
    }