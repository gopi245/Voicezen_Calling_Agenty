"""Which client owns which dialed number.

vobiz-native serves every client from one process. This is the lookup that
replaces what LiveKit's dispatch-rule metadata used to do: given the number
a caller dialed, find which client's script to load and which Vobiz account
to use for anything outbound.

To onboard a new client: add an entry here, and drop their script module
(same shape as script_gundusoodi.py -- GREETING and INSTRUCTIONS) next to it.
"""
from __future__ import annotations

import os

CLIENTS: dict[str, dict] = {
    "+918065353946": {
        "client_id": "gundusoodi",
        "customer_id": 3,
        "prompt_module": "script_gundusoodi",
        # Reuses the account already in .env -- Gundusoodi hasn't moved to
        # its own dedicated Vobiz account, so there's nothing separate to
        # point at yet. A client with their own account gets real values
        # here instead.
        "vobiz_auth_id": os.getenv("VOBIZ_AUTH_ID", ""),
        "vobiz_auth_token": os.getenv("VOBIZ_AUTH_TOKEN", ""),
    },
}


def lookup(dialed_number: str | None) -> dict | None:
    if not dialed_number:
        return None
    return CLIENTS.get(dialed_number)
