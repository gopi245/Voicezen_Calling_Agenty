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
    # An empty queue is the normal case now that it's truncated rather than
    # deleted, so return before rewriting a file that has nothing in it.
    if not lines:
        return

    still_failing: list[str] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue  # drop unparseable rows rather than retry forever
        if _post(item["payload"], timeout=10.0) is None:
            still_failing.append(line)

    # Emptied, never unlinked. In the container this file is a single-file
    # bind mount, and unlink() on a mount target raises EBUSY -- which the
    # except below swallowed, so the queue could never clear itself. Every
    # later successful call re-flushed the same payloads and logged them
    # again: four stuck calls became nine call_logs rows in one evening, each
    # replay also re-uploading its recording. Truncating works on a bind
    # mount; only deleting doesn't.
    try:
        if still_failing:
            QUEUE_FILE.write_text("\n".join(still_failing) + "\n", encoding="utf-8")
        else:
            QUEUE_FILE.write_text("", encoding="utf-8")
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
    captured_data: dict[str, Any] | None = None,
    recording_base64: str | None = None,
    sentiment: str | None = None,
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
        # Structured order/address fields, when the call produced any --
        # shaped to match what the "Gundusoodhi - post-call WhatsApp" n8n
        # workflow expects. Passed through as-is, not wrapped.
        "captured_data": captured_data,
        # Caller-side audio only, base64 WAV -- lets a call actually be
        # listened to, not just read as a transcript.
        "recording_base64": recording_base64,
        # positive / neutral / negative, judged by the call-end extraction
        # from the whole conversation (2026-10-18) -- fills the dashboard's
        # Sentiment column, which had shown dashes since launch.
        "sentiment": sentiment,
    }

    _send(payload)


async def report_call_async(**kwargs: Any) -> None:
    """Async wrapper. httpx here is synchronous and this runs on the agent's
    event loop during shutdown, so push it to a thread rather than blocking
    the loop while the worker is trying to tear the job down."""
    import asyncio

    await asyncio.to_thread(report_call, **kwargs)


def send_lead_alert(to: str, text: str) -> bool:
    """WhatsApp a lead's details to a team member, via the lead-alert edge
    function (which relays over WaSender -- the internal-alert channel, never
    a customer-facing WhatsApp number). Best-effort: a failed alert is logged
    and lost, never queued -- the same lead is already in the call log.
    Same auth as log-call: VOICEZEN_AGENT_SECRET in x-agent-secret."""
    base = os.getenv("VOICEZEN_URL", "").rstrip("/")
    secret = os.getenv("VOICEZEN_AGENT_SECRET", "")
    anon = os.getenv("VOICEZEN_ANON_KEY", "")
    if not base or not secret:
        logger.warning("lead alert not configured -- skipped")
        return False
    try:
        r = httpx.post(
            f"{base}/functions/v1/lead-alert",
            headers={"x-agent-secret": secret, "Content-Type": "application/json",
                     "apikey": anon, "Authorization": f"Bearer {anon}"},
            json={"to": to, "text": text},
            timeout=20.0,
        )
    except Exception as e:  # noqa: BLE001 -- best-effort by design
        logger.warning("lead alert failed: %s", type(e).__name__)
        return False
    if r.status_code != 200:
        logger.warning("lead alert failed: HTTP %s %s", r.status_code, r.text[:120])
        return False
    logger.info("lead alert sent to %s", to)
    return True


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
