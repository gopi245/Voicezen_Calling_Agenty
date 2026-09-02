"""Report this month's call minutes against the LiveKit free-tier quota.

LiveKit's Build plan includes 1,000 agent-session minutes a month and takes no
credit card. No Build overage rate is published, so the safe assumption is that
exceeding the quota interrupts service rather than quietly billing -- which on
a customer-facing phone line means calls stop being answered.

This totals actual call time from the Vobiz CDRs so the number is real rather
than estimated.

    python usage.py
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

FREE_TIER_MINUTES = 1000
ALERT_AT_PCT = 90
API = "https://api.vobiz.ai/api/v1"
ALERT_FILE = "/opt/gundusoodi/QUOTA_ALERT.txt"


def _alert(month: str, minutes: float, pct: float, state: str) -> None:
    """Write a standing alert file the daily cron surfaces.

    Deliberately a file rather than an email: no SMTP is configured on this
    box, and wiring into the existing n8n would mean touching a stack that is
    meant to stay untouched.
    """
    text = (
        f"LIVEKIT QUOTA ALERT -- {state}\n"
        f"month     : {month}\n"
        f"used      : {minutes:.1f} of {FREE_TIER_MINUTES} minutes ({pct:.0f}%)\n\n"
        "Options:\n"
        "  1. Upgrade LiveKit to Ship -- $50/mo, 5,000 minutes, card on file\n"
        "     so overage bills instead of cutting calls off.\n"
        "  2. Self-host LiveKit on this VPS -- see DEPLOY.md 'Self-hosting'.\n"
        "     Note: loses Krisp BVC noise cancellation.\n"
    )
    try:
        with open(ALERT_FILE, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass  # running off-box; the printed warning is enough


def main() -> None:
    auth_id = os.environ["VOBIZ_AUTH_ID"]
    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": os.environ["VOBIZ_AUTH_TOKEN"],
    }

    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")

    total_sec = 0
    calls = 0
    answered = 0
    offset = 0

    while True:
        r = httpx.get(
            f"{API}/Account/{auth_id}/Call",
            headers=headers,
            params={"limit": 100, "offset": offset},
            timeout=60,
        )
        r.raise_for_status()
        body = r.json()
        objects = body.get("objects", [])
        if not objects:
            break

        for c in objects:
            started = c.get("initiation_time") or ""
            if not started.startswith(month):
                continue
            calls += 1
            if c.get("answer_time"):
                answered += 1
            # bill_duration is in seconds; fall back to billed_duration.
            dur = c.get("bill_duration") or c.get("billed_duration") or 0
            try:
                total_sec += int(dur)
            except (TypeError, ValueError):
                pass

        offset += 100
        if offset >= body.get("meta", {}).get("total_count", 0):
            break

    minutes = total_sec / 60
    pct = minutes / FREE_TIER_MINUTES * 100

    print(f"Month              : {month}")
    print(f"Calls              : {calls}  ({answered} answered)")
    print(f"Talk time          : {minutes:.1f} minutes")
    print(f"LiveKit free tier  : {FREE_TIER_MINUTES} agent minutes")
    print(f"Used               : {pct:.1f}%")
    print()

    remaining = FREE_TIER_MINUTES - minutes
    if remaining <= 0:
        print("  *** OVER QUOTA *** Calls may stop being answered until the")
        print("  plan is upgraded. Check the LiveKit dashboard now.")
        _alert(month, minutes, pct, "OVER QUOTA")
    elif pct >= ALERT_AT_PCT:
        print(f"  *** {pct:.0f}% OF QUOTA USED *** {remaining:.0f} minutes left.")
        print("  Time to decide: upgrade to Ship ($50/mo, 5,000 min) or")
        print("  self-host LiveKit. The self-host recipe is in DEPLOY.md.")
        _alert(month, minutes, pct, f"{pct:.0f}% used")
    else:
        avg = minutes / answered if answered else 3.0
        print(f"  {remaining:.0f} minutes left "
              f"(~{remaining / max(avg, 0.1):.0f} more calls at {avg:.1f} min each)")

    print()
    print("Note: this is Vobiz talk time, which tracks agent-session minutes")
    print("closely but is not LiveKit's own meter. Confirm against the LiveKit")
    print("dashboard before relying on it for a billing decision.")


main()
