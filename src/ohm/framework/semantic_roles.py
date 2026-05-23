"""SemanticRoles — maps abstract inference roles to domain edge type lists.

Inference functions (build_bayesian_network, compute_voi, markov_absorbing_risk,
etc.) use these role-to-edge-type mappings instead of hardcoded strings. Domain
apps override individual roles without specifying all of them.

Reference: OHM-f2av / OHM-1ure
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class SemanticRoles:
    """Mapping from abstract inference roles to domain-specific edge type lists.

    OHM defaults match the current hardcoded edge types. Domain apps create
    overrides via ``SemanticRoles.defaults().merge(...)``::

        topo_roles = SemanticRoles.defaults().merge(
            state_transitions=["DEGRADES_TO", "FAILS_TO", "TRANSITIONS_TO"],
            causal=["CAUSES", "DEGRADES", "DEPENDS_ON"],
        )

    Fields:
        causal: Causal/influence edges used by compute_voi and compute_ate.
        bayesian: All edges used by build_bayesian_network (noisy-OR model).
        state_transitions: Markov transition edges used by markov_absorbing_risk
            and markov_expected_steps.
        negating: Edges with inverted probability semantics (ADR-009).
        evidential: Epistemic support/challenge edges.
    """

    causal: tuple[str, ...] = field(default=("CAUSES", "INFLUENCES", "ENABLES", "DEPENDS_ON"))
    bayesian: tuple[str, ...] = field(default=("CAUSES", "DEPENDS_ON", "THREATENS", "EXPECTED_LIKELIHOOD", "NEGATES"))
    state_transitions: tuple[str, ...] = field(default=("CAUSES", "TRANSITIONS_TO"))
    negating: tuple[str, ...] = field(default=("NEGATES",))
    evidential: tuple[str, ...] = field(default=("SUPPORTS", "CHALLENGED_BY"))

    # ── Factories ─────────────────────────────────────────────────────────────

    @classmethod
    def defaults(cls) -> "SemanticRoles":
        """Return the canonical OHM default roles."""
        return cls()

    # ── Merge ──────────────────────────────────────────────────────────────

    def merge(self, **overrides: Any) -> "SemanticRoles":
        """Return a new SemanticRoles with specified roles replaced.

        Accepts keyword arguments matching field names. Values may be lists or
        tuples — both are converted to tuples for hashability.

        Example::

            roles = SemanticRoles.defaults().merge(
                causal=["CAUSES", "DEGRADES", "DEPENDS_ON"],
            )
        """
        normalised: dict[str, tuple[str, ...]] = {}
        valid = {"causal", "bayesian", "state_transitions", "negating", "evidential"}
        for key, value in overrides.items():
            if key not in valid:
                raise ValueError(f"Unknown SemanticRoles field: {key!r}. Valid: {sorted(valid)}")
            normalised[key] = tuple(value)
        return replace(self, **normalised)

    # ── Convenience list accessors ────────────────────────────────────────────

    def causal_list(self) -> list[str]:
        return list(self.causal)

    def bayesian_list(self) -> list[str]:
        return list(self.bayesian)

    def state_transitions_list(self) -> list[str]:
        return list(self.state_transitions)

    def negating_list(self) -> list[str]:
        return list(self.negating)

    def evidential_list(self) -> list[str]:
        return list(self.evidential)
