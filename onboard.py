"""Onboard a client in one command.

Wiring a customer up by hand is six ordered steps -- assign the Vobiz number,
create the LiveKit inbound trunk, create the dispatch rule carrying the client
metadata, create the outbound trunk, insert the customer_integrations row, and
add the client profile folder. Miss one and the failure is silent: the number
rings and nobody answers.

    python onboard.py acme --number +918065353946 --customer-id 5 \
                           --agent-name "Acme Clinic Reception"

    python onboard.py acme --status      # what exists so far
    python onboard.py acme --rollback    # undo what this script created

Nothing is created until you have seen the plan and confirmed it. The database
insert in particular is shown as the exact SQL first, because changing the
database always requires sign-off -- automating the work does not remove that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
import psycopg2
from dotenv import load_dotenv

load_dotenv()

from livekit import api  # noqa: E402

VOBIZ_API = "https://api.vobiz.ai/api/v1"
AGENT_NAME = "voice-assistant"
STATE_DIR = Path(__file__).parent / ".onboard"
CLIENTS_DIR = Path(__file__).parent / "clients"


# ---------------------------------------------------------------------------
# state -- so a half-finished run can be inspected and undone
# ---------------------------------------------------------------------------


def _state_path(client_id: str) -> Path:
    return STATE_DIR / f"{client_id}.json"


def _load_state(client_id: str) -> dict[str, Any]:
    p = _state_path(client_id)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save_state(client_id: str, state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    _state_path(client_id).write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _vobiz_headers() -> dict[str, str]:
    return {
        "X-Auth-ID": os.environ["VOBIZ_AUTH_ID"],
        "X-Auth-Token": os.environ["VOBIZ_AUTH_TOKEN"],
        "Content-Type": "application/json",
    }


def _db():
    """Direct Postgres. The REST API is bound by RLS; onboarding writes on
    behalf of a customer who is not logged in."""
    return psycopg2.connect(
        host=os.environ["SUPABASE_DB_HOST"],
        port=int(os.getenv("SUPABASE_DB_PORT", "5432")),
        user=os.environ["SUPABASE_DB_USER"],
        password=os.environ["SUPABASE_DB_PASSWORD"],
        dbname=os.getenv("SUPABASE_DB_NAME", "postgres"),
        connect_timeout=30,
    )


def _fail(msg: str) -> None:
    print(f"\n  STOP: {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# pre-flight -- check everything BEFORE creating anything
# ---------------------------------------------------------------------------


async def preflight(client_id: str, number: str, customer_id: int) -> dict[str, Any]:
    print("== Checking (nothing is created yet) ==")
    found: dict[str, Any] = {}

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}", client_id):
        _fail(f"client id {client_id!r} must be lowercase letters, digits and hyphens")

    if not re.fullmatch(r"\+\d{10,15}", number):
        _fail(f"number {number!r} must be E.164, e.g. +918071580257")

    auth_id = os.environ["VOBIZ_AUTH_ID"]

    # The number must exist on the account, or every call fails at the carrier.
    r = httpx.get(f"{VOBIZ_API}/Account/{auth_id}/numbers",
                  headers=_vobiz_headers(), timeout=30)
    r.raise_for_status()
    numbers = {n["e164"]: n for n in r.json().get("items", [])}
    if number not in numbers:
        _fail(f"{number} is not on this Vobiz account. Owned: {', '.join(sorted(numbers))}")
    print(f"  ok    vobiz owns {number} (status {numbers[number].get('status')})")

    conn = _db()
    cur = conn.cursor()
    cur.execute("select id, full_name from public.customers where id = %s", (customer_id,))
    row = cur.fetchone()
    if not row:
        _fail(f"customers.id={customer_id} does not exist")
    print(f"  ok    customer {customer_id} exists ({row[1] or 'unnamed'})")

    # A second integration for the same number would make routing ambiguous.
    cur.execute(
        "select id, agent_name, engine from public.customer_integrations "
        "where vobiz_number = %s", (number,))
    existing = cur.fetchone()
    conn.close()
    if existing:
        _fail(f"{number} already has integration id={existing[0]} "
              f"({existing[1]}, engine={existing[2]}). Use --rollback or pick another number.")
    print(f"  ok    no existing integration for {number}")

    # Two dispatch rules for one number route calls unpredictably.
    async with api.LiveKitAPI() as lk:
        trunks = await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
        clash = [t.sip_trunk_id for t in trunks.items if number in list(t.numbers)]
        if clash:
            _fail(f"LiveKit already has an inbound trunk for {number}: {clash}")
    print(f"  ok    no existing LiveKit inbound trunk for {number}")

    profile = CLIENTS_DIR / client_id / "agent_profile.py"
    found["profile_exists"] = profile.exists()
    print(f"  {'ok   ' if profile.exists() else 'note '} client profile "
          f"{'exists' if profile.exists() else 'will be stubbed'}: {profile}")

    return found


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def onboard(client_id: str, number: str, customer_id: int,
                  agent_name: str, assume_yes: bool) -> None:
    found = await preflight(client_id, number, customer_id)
    trunk_id = os.environ["VOBIZ_TRUNK_ID"]

    insert_sql = (
        "INSERT INTO public.customer_integrations (\n"
        "    customer_id, dograh_workflow_id, agent_name, vobiz_number,\n"
        "    engine, voice_provider, agent_type, status, is_default\n"
        f") VALUES ({customer_id}, 'livekit:{client_id}', '{agent_name}',\n"
        f"          '{number}', 'livekit', 'cartesia', 'receptionist',\n"
        "          'active', false);"
    )

    print("\n== WILL CREATE ==")
    print(f"  Vobiz     : assign {number} to trunk {trunk_id}")
    print(f"  LiveKit   : inbound trunk for {number}")
    print(f"  LiveKit   : dispatch rule -> "
          f'{{"client":"{client_id}","customer_id":{customer_id},'
          f'"dialed_number":"{number}"}}')
    print(f"  LiveKit   : outbound trunk for {number}")
    if not found["profile_exists"]:
        print(f"  Files     : clients/{client_id}/agent_profile.py (stub)")
    print("  Database  :")
    for line in insert_sql.splitlines():
        print(f"      {line}")

    if not assume_yes:
        # The database write needs sign-off. Automating the work does not
        # remove the rule that a schema/data change is shown first.
        if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted -- nothing was created")
            return

    state = _load_state(client_id)
    state.update({"client_id": client_id, "number": number,
                  "customer_id": customer_id})

    print("\n== Creating ==")
    auth_id = os.environ["VOBIZ_AUTH_ID"]

    r = httpx.post(f"{VOBIZ_API}/Account/{auth_id}/numbers/{number}/assign",
                   headers=_vobiz_headers(),
                   json={"trunk_group_id": trunk_id}, timeout=30)
    r.raise_for_status()
    state["vobiz_number_assigned"] = True
    _save_state(client_id, state)
    print(f"  done  vobiz: {number} -> trunk {trunk_id}")

    async with api.LiveKitAPI() as lk:
        inbound = await lk.sip.create_inbound_trunk(
            api.CreateSIPInboundTrunkRequest(
                trunk=api.SIPInboundTrunkInfo(
                    name=f"{agent_name} inbound", numbers=[number],
                    allowed_addresses=["0.0.0.0/0"])))
        state["inbound_trunk_id"] = inbound.sip_trunk_id
        _save_state(client_id, state)
        print(f"  done  livekit inbound trunk {inbound.sip_trunk_id}")

        meta = json.dumps({"client": client_id, "customer_id": customer_id,
                           "dialed_number": number})
        rule = await lk.sip.create_dispatch_rule(
            api.CreateSIPDispatchRuleRequest(
                name=f"{agent_name} calls",
                trunk_ids=[inbound.sip_trunk_id],
                metadata=meta,
                attributes={"client": client_id, "dialed_number": number,
                            "customer_id": str(customer_id)},
                rule=api.SIPDispatchRule(
                    dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                        room_prefix="call")),
                room_config=api.RoomConfiguration(
                    agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME,
                                                  metadata=meta)])))
        state["dispatch_rule_id"] = rule.sip_dispatch_rule_id
        _save_state(client_id, state)
        print(f"  done  livekit dispatch rule {rule.sip_dispatch_rule_id}")

        outbound = await lk.sip.create_outbound_trunk(
            api.CreateSIPOutboundTrunkRequest(
                trunk=api.SIPOutboundTrunkInfo(
                    name=f"{agent_name} outbound",
                    address=os.environ["VOBIZ_SIP_DOMAIN"],
                    auth_username=os.environ["VOBIZ_SIP_USERNAME"],
                    auth_password=os.environ["VOBIZ_SIP_PASSWORD"],
                    numbers=[number])))
        state["outbound_trunk_id"] = outbound.sip_trunk_id
        _save_state(client_id, state)
        print(f"  done  livekit outbound trunk {outbound.sip_trunk_id}")

    conn = _db()
    cur = conn.cursor()
    try:
        cur.execute(insert_sql)
        conn.commit()
        cur.execute("select id from public.customer_integrations "
                    "where vobiz_number = %s", (number,))
        state["integration_id"] = cur.fetchone()[0]
        _save_state(client_id, state)
        print(f"  done  customer_integrations id={state['integration_id']}")
    except Exception as e:
        conn.rollback()
        _fail(f"database insert failed: {e}\n  Run --rollback to undo the rest.")
    finally:
        conn.close()

    if not found["profile_exists"]:
        _write_stub(client_id, agent_name)
        print(f"  done  clients/{client_id}/agent_profile.py (STUB -- must be filled in)")

    print("\n== Done ==")
    print(f"  {number} -> client '{client_id}' -> customer {customer_id}")
    if not found["profile_exists"]:
        print("\n  NEXT: the profile is a stub and the smoke test will FAIL until")
        print(f"        clients/{client_id}/agent_profile.py has the real script.")
        print("        That is deliberate -- an unfinished client must not deploy.")
    print("  Then: python smoke_test.py && restart the worker")


def _write_stub(client_id: str, agent_name: str) -> None:
    d = CLIENTS_DIR / client_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "__init__.py").write_text("", encoding="utf-8")
    (d / "agent_profile.py").write_text(f'''"""{agent_name} -- agent profile.

STUB. Replace every TODO below with this client's real script before
deploying. The smoke test fails while TODO markers remain, so an unfinished
client cannot reach a caller by accident.

Copy clients/gundusoodi/agent_profile.py as a worked example.
"""

from livekit.agents import RunContext, function_tool  # noqa: F401

CONFIG = {{
    # Only what differs from .env. Everything else falls back to the process
    # defaults, so most clients need very little here.
    # "language": "hi-IN",
    # "tts_voice": "<cartesia voice id>",
}}

# TODO: the exact words spoken when the call is answered.
GREETING = "TODO: greeting for {agent_name}"

# TODO: the full system prompt -- persona, fixed answers, prices, refusals,
# and what must never be invented.
INSTRUCTIONS = """TODO: system prompt for {agent_name}"""


class Tools:
    """This client's business tools. Add @function_tool methods here for the
    things they need captured -- orders, bookings, complaints."""
''', encoding="utf-8")


# ---------------------------------------------------------------------------
# status / rollback
# ---------------------------------------------------------------------------


async def status(client_id: str) -> None:
    state = _load_state(client_id)
    if not state:
        print(f"no onboarding state for {client_id!r}")
        return
    print(f"== {client_id} ==")
    for k, v in state.items():
        print(f"  {k:24} {v}")


async def rollback(client_id: str) -> None:
    """Undo what this script created, newest first. Only touches resources
    recorded in the state file -- never anything created by hand."""
    state = _load_state(client_id)
    if not state:
        print(f"nothing recorded for {client_id!r}")
        return

    print(f"== Rolling back {client_id} ==")

    if state.get("integration_id"):
        conn = _db(); cur = conn.cursor()
        cur.execute("delete from public.customer_integrations where id = %s",
                    (state["integration_id"],))
        conn.commit(); conn.close()
        print(f"  removed customer_integrations id={state.pop('integration_id')}")

    async with api.LiveKitAPI() as lk:
        for key, call in (
            ("dispatch_rule_id", lambda i: lk.sip.delete_dispatch_rule(i)),
            ("inbound_trunk_id", lambda i: lk.sip.delete_sip_trunk(i)),
            ("outbound_trunk_id", lambda i: lk.sip.delete_sip_trunk(i)),
        ):
            if state.get(key):
                try:
                    await call(state[key])
                    print(f"  removed {key}={state.pop(key)}")
                except Exception as e:
                    print(f"  could not remove {key}={state[key]}: {str(e)[:90]}")

    if state.get("vobiz_number_assigned"):
        auth_id = os.environ["VOBIZ_AUTH_ID"]
        r = httpx.delete(f"{VOBIZ_API}/Account/{auth_id}/numbers/"
                         f"{state['number']}/assign",
                         headers=_vobiz_headers(), timeout=30)
        print(f"  unassigned {state['number']} (HTTP {r.status_code})")
        state.pop("vobiz_number_assigned")

    _save_state(client_id, state)
    print("\n  The client profile folder is left alone -- delete it by hand if")
    print("  you meant to discard the script too.")


def main() -> None:
    p = argparse.ArgumentParser(description="Onboard a voice-agent client.")
    p.add_argument("client_id", help="lowercase id, e.g. acme")
    p.add_argument("--number", help="E.164, e.g. +918065353946")
    p.add_argument("--customer-id", type=int, help="customers.id in Supabase")
    p.add_argument("--agent-name", default=None, help="shown in the dashboard")
    p.add_argument("--yes", action="store_true", help="skip the confirmation")
    p.add_argument("--status", action="store_true")
    p.add_argument("--rollback", action="store_true")
    a = p.parse_args()

    if a.status:
        asyncio.run(status(a.client_id)); return
    if a.rollback:
        asyncio.run(rollback(a.client_id)); return
    if not a.number or not a.customer_id:
        p.error("--number and --customer-id are required to onboard")

    asyncio.run(onboard(a.client_id, a.number, a.customer_id,
                        a.agent_name or f"{a.client_id.title()} Assistant",
                        a.yes))


if __name__ == "__main__":
    main()
