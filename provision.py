"""One-time wiring between Vobiz and LiveKit.

Run once (or after changing numbers/trunks):

    python provision.py trunk        # create the Vobiz SIP trunk
    python provision.py outbound     # LiveKit -> Vobiz  (placing calls)
    python provision.py inbound      # Vobiz -> LiveKit  (receiving calls)

The credential (SIP username/password) is created in the Vobiz console under
SIP Trunk > Outbound Trunks > Trunks > Authentication & Linking. That step is
deliberately not scripted -- the password is shown once at creation and is
hashed server-side afterwards, so it cannot be read back out of the API.
"""

from __future__ import annotations

import asyncio
import os
import sys

import secrets

import httpx
from dotenv import load_dotenv
from livekit import api

load_dotenv()

VOBIZ_API = "https://api.vobiz.ai/api/v1"
AGENT_NAME = "voice-assistant"


def _vobiz_headers() -> dict[str, str]:
    return {
        "X-Auth-ID": os.environ["VOBIZ_AUTH_ID"],
        "X-Auth-Token": os.environ["VOBIZ_AUTH_TOKEN"],
        "Content-Type": "application/json",
    }


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"{name} is not set in .env -- fill it in and re-run.")
    return value


# --------------------------------------------------------------------------


def create_vobiz_trunk() -> None:
    auth_id = _require("VOBIZ_AUTH_ID")
    resp = httpx.post(
        f"{VOBIZ_API}/Account/{auth_id}/trunks",
        headers=_vobiz_headers(),
        json={
            "name": "LiveKit Voice Agent",
            "trunk_type": "OUTBOUND",
            "max_concurrent_calls": 10,
            # UDP is the low-latency default and is what the LiveKit
            # integration expects. Switch to TLS only if you need encrypted
            # signalling and have confirmed LiveKit is configured to match.
            "transport": "udp",
        },
        timeout=30,
    )
    resp.raise_for_status()
    trunk = resp.json()

    print(f"trunk_id     : {trunk['trunk_id']}")
    print(f"trunk_domain : {trunk['trunk_domain']}")
    print()
    print("Next:")
    print(f"  1. Put VOBIZ_TRUNK_ID={trunk['trunk_id']} and")
    print(f"     VOBIZ_SIP_DOMAIN={trunk['trunk_domain']} in .env")
    print("  2. In the Vobiz console, open this trunk > Authentication & Linking")
    print("     and create a credential. Copy the username and password into")
    print("     VOBIZ_SIP_USERNAME / VOBIZ_SIP_PASSWORD.")
    print("     The credential username is NOT your dashboard display name --")
    print("     using the display name gives you a 401 on every call.")


def create_sip_credential() -> None:
    """Create a SIP credential and attach it to the trunk.

    The password is generated here rather than chosen, because it is only ever
    typed by machines -- and because reusing a human password you already use
    elsewhere on a SIP endpoint is how trunks get toll-fraud drained.

    Vobiz returns the password as `<redacted>` on every subsequent read, so
    this is the only moment it exists in readable form. It goes straight to
    .env.
    """
    auth_id = _require("VOBIZ_AUTH_ID")
    trunk_id = _require("VOBIZ_TRUNK_ID")

    username = f"lk-agent-{secrets.token_hex(3)}"
    password = secrets.token_urlsafe(24)

    resp = httpx.post(
        f"{VOBIZ_API}/Account/{auth_id}/credentials",
        headers=_vobiz_headers(),
        json={"username": username, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    credential = resp.json()
    credential_uuid = credential.get("id") or credential.get("credential_uuid")

    # A credential is account-scoped; it only takes effect once the trunk
    # points at it.
    link = httpx.put(
        f"{VOBIZ_API}/Account/{auth_id}/trunks/{trunk_id}",
        headers=_vobiz_headers(),
        json={"credential_uuid": credential_uuid},
        timeout=30,
    )
    link.raise_for_status()

    print(f"credential_uuid : {credential_uuid}")
    print(f"username        : {username}")
    print(f"password        : {password}")
    print()
    print("Put these in .env now -- Vobiz redacts the password from here on:")
    print(f"  VOBIZ_SIP_USERNAME={username}")
    print(f"  VOBIZ_SIP_PASSWORD={password}")


def assign_number_to_trunk() -> None:
    """Route the PSTN number through the trunk.

    Without this the trunk is configured correctly but nothing feeds it -- a
    call to the number never reaches LiveKit and the line just rings out.
    """
    auth_id = _require("VOBIZ_AUTH_ID")
    number = _require("VOBIZ_PHONE_NUMBER")

    resp = httpx.post(
        f"{VOBIZ_API}/Account/{auth_id}/numbers/{number}/assign",
        headers=_vobiz_headers(),
        json={"trunk_group_id": _require("VOBIZ_TRUNK_ID")},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"{number} assigned to trunk {os.environ['VOBIZ_TRUNK_ID']}")


async def create_livekit_outbound_trunk() -> None:
    """Teach LiveKit how to hand calls to Vobiz."""
    sip_domain = _require("VOBIZ_SIP_DOMAIN")
    number = _require("VOBIZ_PHONE_NUMBER")

    async with api.LiveKitAPI() as lk:  # reads LIVEKIT_* from the environment
        trunk = await lk.sip.create_outbound_trunk(
            api.CreateSIPOutboundTrunkRequest(
                trunk=api.SIPOutboundTrunkInfo(
                    name="Vobiz",
                    # Must match trunk_domain exactly. The generic
                    # "sip.vobiz.ai" will fail silently.
                    address=sip_domain,
                    auth_username=_require("VOBIZ_SIP_USERNAME"),
                    auth_password=_require("VOBIZ_SIP_PASSWORD"),
                    numbers=[number],
                )
            )
        )

    print(f"LiveKit outbound trunk: {trunk.sip_trunk_id}")
    print(f"Put LIVEKIT_OUTBOUND_TRUNK_ID={trunk.sip_trunk_id} in .env")


async def create_livekit_inbound(livekit_sip_uri: str) -> None:
    """Teach LiveKit to accept calls from Vobiz, then point Vobiz at LiveKit."""
    number = _require("VOBIZ_PHONE_NUMBER")
    auth_id = _require("VOBIZ_AUTH_ID")

    async with api.LiveKitAPI() as lk:
        trunk = await lk.sip.create_inbound_trunk(
            api.CreateSIPInboundTrunkRequest(
                trunk=api.SIPInboundTrunkInfo(
                    name="Vobiz Inbound",
                    numbers=[number],
                    # Wide open so the first call works. Before going live,
                    # narrow this to Vobiz's signalling IP range.
                    allowed_addresses=["0.0.0.0/0"],
                )
            )
        )
        print(f"LiveKit inbound trunk: {trunk.sip_trunk_id}")

        rule = await lk.sip.create_dispatch_rule(
            api.CreateSIPDispatchRuleRequest(
                name="Vobiz Inbound Calls",
                trunk_ids=[trunk.sip_trunk_id],
                # One room per caller.
                rule=api.SIPDispatchRule(
                    dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                        room_prefix="call"
                    )
                ),
                room_config=api.RoomConfiguration(
                    agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]
                ),
            )
        )
        print(f"LiveKit dispatch rule: {rule.sip_dispatch_rule_id}")

    # Vobiz needs the bare host. Leaving the "sip:" prefix on is the single
    # most common reason inbound calls never arrive.
    destination = livekit_sip_uri.removeprefix("sip:").strip()

    # The trunk domain is a truncated form of the ID (8a94fdf6.sip.vobiz.ai vs
    # 8a94fdf6-a2d1-...), so the ID cannot be derived from it -- keep both.
    trunk_id = _require("VOBIZ_TRUNK_ID")
    # PUT, not PATCH -- the trunk resource has no PATCH method.
    resp = httpx.put(
        f"{VOBIZ_API}/Account/{auth_id}/trunks/{trunk_id}",
        headers=_vobiz_headers(),
        json={"inbound_destination": destination},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Vobiz inbound_destination -> {destination}")


# --------------------------------------------------------------------------

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""

    if command == "trunk":
        create_vobiz_trunk()
    elif command == "credential":
        create_sip_credential()
    elif command == "number":
        assign_number_to_trunk()
    elif command == "outbound":
        asyncio.run(create_livekit_outbound_trunk())
    elif command == "inbound":
        if len(sys.argv) < 3:
            sys.exit(
                "usage: python provision.py inbound <livekit-sip-uri>\n"
                "  find it in LiveKit Dashboard > Settings > Project,\n"
                "  it looks like sip:abc123.sip.livekit.cloud"
            )
        asyncio.run(create_livekit_inbound(sys.argv[2]))
    else:
        sys.exit(__doc__)
