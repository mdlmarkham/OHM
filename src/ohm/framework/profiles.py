"""OHM Agent Profiles — multi-instance access for a single agent.

Loads a profile catalog (project-level ``.ohm/profiles.json`` or user-level
``~/.ohm/profiles.json``) describing the OHM stores an agent may connect to,
and resolves a selected profile into a live :class:`~ohm.framework.sdk.Graph`.

Usage:
    from ohm.framework.profiles import AgentProfiles, from_profile

    catalog = AgentProfiles.from_files()
    if catalog is not None:
        profile = catalog.select("devops")
        graph = from_profile(profile)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ohm.framework.sdk import Graph

_PROFILE_PATHS = [
    ".ohm/profiles.json",
    "~/.ohm/profiles.json",
]


def load_catalog() -> dict | None:
    """Find and load the agent profile catalog.

    Searches in priority order:
      1. ``.ohm/profiles.json`` (project-level, relative to cwd)
      2. ``~/.ohm/profiles.json`` (user-level)

    Returns the parsed catalog dict, or ``None`` if no catalog file exists.
    """
    for path_str in _PROFILE_PATHS:
        path = Path(path_str).expanduser()
        try:
            if not path.exists():
                continue
        except OSError:
            continue
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
    return None


@dataclass
class Profile:
    """A single OHM connection profile.

    A profile describes one OHM store an agent may access: the daemon URL
    (or core store), tenant routing, credentials, domain template, and
    tool/permission bounds.

    Attributes:
        name: Profile key in the catalog (e.g. ``"devops"``).
        label: Human-readable label.
        ohm_url: Daemon base URL. When unset, connects to the core store.
        tenant_id: Tenant identifier for multi-tenant routing.
        token: Bearer token for ohmd authentication.
        agent_id: Actor name for attribution.
        domain_config: Domain template filename (e.g. ``devsecops.json``).
        allowed_tools: Tool names this profile may invoke (``["*"]`` = all).
        read_only: When True, the profile must not perform writes.
        token_type: Token kind (e.g. ``"customer"`` or ``"agent"``).
        default: When True, selected by ``AgentProfiles.select()`` with no name.
    """

    name: str
    label: str = ""
    ohm_url: str | None = None
    tenant_id: str | None = None
    token: str | None = None
    agent_id: str = "unknown"
    domain_config: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    read_only: bool = False
    token_type: str | None = None
    default: bool = False


class AgentProfiles:
    """A catalog of agent connection profiles.

    Wraps the raw catalog JSON and exposes lookup, selection, and listing
    of :class:`Profile` objects.
    """

    def __init__(self, catalog: dict):
        """Initialize from a raw catalog dict.

        Args:
            catalog: Parsed profile catalog JSON. Expected to contain a
                ``"profiles"`` dict keyed by profile name.
        """
        self._catalog = catalog
        self._profiles: dict[str, Profile] = {}
        profiles = catalog.get("profiles", {})
        if isinstance(profiles, dict):
            for name, data in profiles.items():
                if not isinstance(data, dict):
                    continue
                self._profiles[name] = Profile(
                    name=name,
                    label=data.get("label", ""),
                    ohm_url=data.get("ohm_url"),
                    tenant_id=data.get("tenant_id"),
                    token=data.get("token"),
                    agent_id=data.get("agent_id", "unknown"),
                    domain_config=data.get("domain_config"),
                    allowed_tools=list(data.get("allowed_tools", [])),
                    read_only=bool(data.get("read_only", False)),
                    token_type=data.get("token_type"),
                    default=bool(data.get("default", False)),
                )

    def get(self, name: str) -> Profile | None:
        """Return the profile with the given name, or ``None`` if absent."""
        return self._profiles.get(name)

    def select(self, name: str | None = None) -> Profile | None:
        """Select the active profile.

        If ``name`` is provided, return that profile (or ``None`` if missing).
        Otherwise return the profile marked ``default=True`` (or ``None`` if
        no default is declared).
        """
        if name is not None:
            return self.get(name)
        for profile in self._profiles.values():
            if profile.default:
                return profile
        return None

    def list_profiles(self) -> list[Profile]:
        """Return all profiles in the catalog."""
        return list(self._profiles.values())

    @classmethod
    def from_files(cls) -> AgentProfiles | None:
        """Build an :class:`AgentProfiles` from the on-disk catalog.

        Calls :func:`load_catalog` and returns an ``AgentProfiles`` instance,
        or ``None`` if no catalog file was found.
        """
        catalog = load_catalog()
        if catalog is None:
            return None
        return cls(catalog)


def from_profile(profile: Profile) -> Graph:
    """Open a :class:`~ohm.framework.sdk.Graph` for the given profile.

    If ``profile.ohm_url`` is set, connects to the ohmd daemon at that URL via
    :func:`~ohm.framework.sdk.connect_http` (passing tenant routing and token).
    Otherwise connects to the core store via :func:`~ohm.framework.sdk.connect`
    using the default DuckDB path (``$OHM_DB`` or ``~/.ohm/ohm.duckdb``).
    """
    from ohm.framework.sdk import connect, connect_http

    if profile.ohm_url:
        return connect_http(
            base_url=profile.ohm_url,
            actor=profile.agent_id,
            token=profile.token,
            tenant_id=profile.tenant_id or None,
        )
    db_path = os.environ.get("OHM_DB", str(Path.home() / ".ohm" / "ohm.duckdb"))
    return connect(db_path, actor=profile.agent_id)
