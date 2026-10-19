"""Server rules for dashboard campaigns that run on the agent's own script.

The dashboard's Campaign Calls page can launch a campaign with "Use my
agent's call script" (2026-10-19, Vedantha Vanam first): no pitch fields,
just a contact list. campaign-dispatch dials each contact through /dial
as usual, but the answered call speaks the calling line's own script --
the same script, and every server guard, that line's inbound calls get --
instead of the generic CAMPAIGN_TEMPLATE.

These calls also get a few rules the server says itself, never left to the
model: "don't call me" and "wrong number" end the call and put the number
on a do-not-call list that every later campaign skips; busy gets a polite
"we'll call back"; "where did you get my number" has one true answer; a
bare "hello" right after the greeting re-introduces.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("campaign_rules")

# What campaign-launch stores in pitch_content / call_to_action for a
# campaign that runs on the agent's own script (the table has no column
# for it; the dashboard writes and reads this same marker).
AGENT_SCRIPT_MARKER = "[agent script]"


def uses_agent_script(campaign: dict | None) -> bool:
    if not campaign or campaign.get("demo_call") or campaign.get("instant_lead") or campaign.get("script_test"):
        return False
    return bool(campaign.get("use_agent_script")) or (campaign.get("offering") or "").strip() == AGENT_SCRIPT_MARKER


# ------------------------------------------------------------------ do-not-call

_DNC_FILE = Path(__file__).resolve().parent / "logs" / "do_not_call.json"
_dnc: dict[str, dict] | None = None


def _load_dnc() -> dict:
    global _dnc
    if _dnc is None:
        try:
            _dnc = json.loads(_DNC_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _dnc = {}
        except Exception:
            log.exception("could not read %s", _DNC_FILE)
            _dnc = {}
    return _dnc


def is_dnc(phone: str | None) -> bool:
    return bool(phone) and phone in _load_dnc()


def add_dnc(phone: str | None, reason: str) -> None:
    dnc = _load_dnc()
    if not phone or phone in dnc:
        return
    dnc[phone] = {"at": time.time(), "reason": reason}
    try:
        _DNC_FILE.parent.mkdir(exist_ok=True)
        tmp = _DNC_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(dnc, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(_DNC_FILE)
    except Exception:
        log.exception("could not save the do-not-call list")
    log.info("do-not-call: %s (%s)", phone, reason)


# ------------------------------------------------------------------ what the caller said

OPT_OUT_RX = re.compile(
    r"(కాల్|ఫోన్|కాల్స్)\s*(లు\s*)?(చేయ|చెయ్య|చేయ్య|చెయ)\s*(కండి|కండీ|ొద్దు|వద్దు|కూడదు|ద్దు)|"
    r"(కాల్|ఫోన్)\s*(చేయ|చెయ్య)?\s*(అవసరం లేదు|వద్దు)|"
    r"నా నంబర్\s*(తీసేయండి|తీసివేయండి|డిలీట్)|నంబర్ (తీసేయండి|డిలీట్ చేయండి)|"
    r"don'?t call|do not call|stop calling|remove my number|never call",
    re.I,
)
_NOT_NOW_RX = re.compile(r"ఇప్పుడు|తర్వాత|తరువాత|రేపు|సాయంత్రం|later|right now|\bnow\b", re.I)
WRONG_NUMBER_RX = re.compile(r"రాంగ్ నంబర్|రాంగ్ నెంబర్|wrong number|తప్పు నంబర్|ఎంక్వయిరీ చేయలేదు|ఎంక్వైరీ చేయలేదు|"
                             r"నేను (ఏమీ )?అడగలేదు|నేను ఎవరికీ నంబర్ ఇవ్వలేదు", re.I)
_BUSY_RX = re.compile(
    r"బిజీ|busy|ఇప్పుడు (కాదు|మాట్లాడలేను|వీలు కాదు|కుదరదు|టైం లేదు)|డ్రైవింగ్|బండి నడుపుతున్నా|మీటింగ్|"
    r"(తర్వాత|తరువాత|సాయంత్రం|రేపు)\s*(కాల్|ఫోన్|చేయండి|చెయ్యండి|మాట్లాడ)", re.I)
NUMBER_SOURCE_RX = re.compile(
    r"(నా|మా)\s*నంబర్\s*(మీకు\s*)?(ఎక్కడ|ఎలా|ఎవరు)|నంబర్ ఎవరు ఇచ్చారు|ఎక్కడి నుంచి నంబర్|"
    r"how did you get my number|where did you get my number",
    re.I,
)
HELLO_RX = re.compile(r"^\s*((హలో|హల్లో|hello|హాయ్|ఆ|ఏ)[\s,.!?]*){1,3}(అండి|సార్)?[\s.!?]*$", re.I)


def wants_opt_out(text: str | None) -> bool:
    """ "Don't call me" -- but not "don't call now, call later", which is busy."""
    return bool(OPT_OUT_RX.search(text or "")) and not _NOT_NOW_RX.search(text or "")


def wants_later(text: str | None) -> bool:
    return bool(_BUSY_RX.search(text or ""))


# ------------------------------------------------------------------ what the agent says

OPT_OUT_LINE = "సరేనండి, క్షమించండి. ఇకపై మీకు కాల్ చేయము. ధన్యవాదాలు, నమస్కారం!"
WRONG_NUMBER_LINE = "క్షమించండి సార్, పొరపాటు జరిగింది. ఇకపై మీకు కాల్ చేయము. నమస్కారం!"
BUSY_LINE = "సరేనండి, ఇబ్బంది లేదు. తర్వాత మళ్ళీ కాల్ చేస్తామండి. ధన్యవాదాలు, నమస్కారం!"
NUMBER_SOURCE_LINE = "మీరు ఇంతకుముందు మా ప్రాజెక్ట్ గురించి వివరాలు అడిగారండి, అందుకే కాల్ చేశాను."

# Added to the line's own instructions on these calls.
OUTBOUND_NOTE = """

THIS IS AN OUTBOUND CALL: we called them. They enquired about us earlier and the opening line already said who you are and why you called. Do not ask how you can help them; follow the flow from the opening line's question. If they say this is a wrong number or they never enquired, apologise once and end politely."""

# How each result is reported to campaign-outcome (its outcome vocabulary).
OUTCOME_FOR = {
    "opted_out": "do_not_call",
    "wrong_number": "wrong_number",
    "booked": "interested",
    "busy_later": "callback_requested",
}
