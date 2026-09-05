"""Report finished calls to the VoiceZen dashboard.

Posts to the `log-call` Supabase Edge Function rather than writing to
call_logs directly. That function holds the service-role key; this agent
holds only VOICEZEN_AGENT_SECRET, which can do exactly one thing -- log a
call. If this VPS were compromised, the attacker could not read other
customers' transcripts or billing, which a service-role key would allow.

The function resolves customer_id from the dialled number itself, so a
compromised agent cannot log a call against a customer it does not serve.

Nothing here runs during a call. It fires after the caller has hung up, so a
slow or failed dashboard never affects the conversation.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("voicezen")

# Calls that could not be reported land here and are retried on the next
# successful send, so a dashboard outage loses nothing.
QUEUE_FILE = Path(__file__).parent / "unsent_calls.jsonl"


def _endpoint() -> str | None:
    base = os.getenv("VOICEZEN_URL", "").rstrip("/")
    return f"{base}/functions/v1/log-call" if base else None


def _queue(payload: dict[str, Any], reason: str) -> None:
    """Persist a call we could not send. Never raises -- the call already
    happened and a logging failure must not surface anywhere else."""
    try:
        with QUEUE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"queued_at": datetime.now(timezone.utc).isoformat(),
                                "reason": reason, "payload": payload},
                               ensure_ascii=False) + "\n")
        logger.warning("call queued for retry (%s)", reason)
    except OSError:
        logger.exception("could not queue the call log")


def _post(payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any] | None:
    url = _endpoint()
    secret = os.getenv("VOICEZEN_AGENT_SECRET", "")
    anon = os.getenv("VOICEZEN_ANON_KEY", "")
    if not url or not secret:
        _queue(payload, "not configured")
        return None
    try:
        r = httpx.post(
            url,
            headers={
                "x-agent-secret": secret,
                "Content-Type": "application/json",
                # Supabase requires an apikey/Authorization on functions even
                # when the function does its own auth.
                "apikey": anon,
                "Authorization": f"Bearer {anon}",
            },
            json=payload,
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001 -- any network failure queues
        _queue(payload, f"{type(e).__name__}")
        return None

    if r.status_code != 200:
        _queue(payload, f"HTTP {r.status_code}: {r.text[:120]}")
        return None
    return r.json()


def _flush_queue() -> None:
    """Retry anything previously queued. Best-effort and silent on failure."""
    if not QUEUE_FILE.exists():
        return
    try:
        lines = QUEUE_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    still_failing: list[str] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue  # drop unparseable rows rather than retry forever
        if _post(item["payload"], timeout=10.0) is None:
            still_failing.append(line)

    try:
        if still_failing:
            QUEUE_FILE.write_text("\n".join(still_failing) + "\n", encoding="utf-8")
        else:
            QUEUE_FILE.unlink()
    except OSError:
        pass


def report_call(
    *,
    dialed_number: str,
    call_duration_seconds: float,
    caller_number: str | None = None,
    caller_name: str | None = None,
    call_type: str | None = None,
    transcript: str | None = None,
    call_summary: str | None = None,
    call_status: str = "completed",
    language: str | None = None,
    captured_data: list[dict[str, Any]] | None = None,
) -> None:
    """Send one finished call. Never raises."""
    payload = {
        "dialed_number": dialed_number,
        # SECONDS. The dashboard bills ceil(seconds / 60) per call, so sending
        # minutes here would under-bill by a factor of sixty.
        "call_duration": int(round(max(call_duration_seconds, 0))),
        "caller_number": caller_number,
        "caller_name": caller_name,
        "call_type": call_type,
        "transcript": transcript,
        "call_summary": call_summary,
        "call_status": call_status,
        "language": language,
        "captured_data": {"events": captured_data} if captured_data else None,
    }

    _send(payload)


async def report_call_async(**kwargs: Any) -> None:
    """Async wrapper. httpx here is synchronous and this runs on the agent's
    event loop during shutdown, so push it to a thread rather than blocking
    the loop while the worker is trying to tear the job down."""
    import asyncio

    await asyncio.to_thread(report_call, **kwargs)


def _send(payload: dict[str, Any]) -> None:
    result = _post(payload)
    if result:
        logger.info(
            "call logged: id=%s customer=%s %ss -> %s billed minute(s)",
            result.get("id"), result.get("customer_id"),
            payload["call_duration"], result.get("billed_minutes"),
        )
        # Only worth retrying the backlog when the endpoint is clearly healthy.
        _flush_queue()
    else:
        logger.warning(
            "call not logged (queued): %ss, %s billed minute(s)",
            payload["call_duration"],
            math.ceil(payload["call_duration"] / 60),
        )
