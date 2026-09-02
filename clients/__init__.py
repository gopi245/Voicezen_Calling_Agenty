"""Per-client agent profiles.

One package per client under `clients/`, each exposing:

    GREETING      str   -- spoken verbatim when the call is answered
    INSTRUCTIONS  str   -- the system prompt
    Tools         class -- a mixin of that client's @function_tool methods
    CONFIG        dict  -- optional per-client overrides (voice, language,
                           models, endpointing). Anything absent falls back to
                           the process-wide .env, so a client only states what
                           differs.

A client is resolved per call from the dispatch rule's metadata, so adding a
customer means adding a folder and a dispatch rule -- no change to agent.py.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("clients")

# Used when a call arrives with no client metadata -- a misconfigured dispatch
# rule should still answer with something rather than dropping the call.
DEFAULT_CLIENT = "gundusoodi"


@dataclass(frozen=True)
class ClientProfile:
    client_id: str
    greeting: str
    instructions: str
    tools: type
    config: dict[str, Any]


_cache: dict[str, ClientProfile] = {}


def load(client_id: str | None) -> ClientProfile:
    """Load a client profile by id, falling back to the default.

    Cached because this runs on every inbound call and the module contents
    never change within a process.
    """
    cid = (client_id or DEFAULT_CLIENT).strip().lower()
    if cid in _cache:
        return _cache[cid]

    try:
        module = importlib.import_module(f"clients.{cid}.agent_profile")
    except ModuleNotFoundError:
        if cid == DEFAULT_CLIENT:
            raise
        logger.error("unknown client %r, falling back to %s", cid, DEFAULT_CLIENT)
        return load(DEFAULT_CLIENT)

    profile = ClientProfile(
        client_id=cid,
        greeting=module.GREETING,
        instructions=module.INSTRUCTIONS,
        tools=module.Tools,
        config=getattr(module, "CONFIG", {}),
    )
    _cache[cid] = profile
    return profile


def available() -> list[str]:
    """Client ids with a profile on disk -- used by the smoke test so every
    client's rules are checked, not just the one currently deployed."""
    from pathlib import Path

    root = Path(__file__).parent
    return sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and (p / "agent_profile.py").exists()
    )
