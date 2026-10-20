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
# Someone else picked up: the person we rang is out, asleep, at work, or
# the phone belongs to a relative. A scripted call (2026-10-20) answered
# "ఆయన ఇంట్లో లేరు అండి" and "నేను ఆయన భార్యను" with "there's a lot of
# noise there" -- both are perfectly clear, and neither is the person we
# called.
NOT_AVAILABLE_RX = re.compile(
    r"(ఆయన|ఆవిడ|అతను|ఆమె|తను|వాళ్ళు)\s*(ఇంట్లో\s*)?(లేరు|లేడు|లేదు|లేరండి)|"
    r"ఇంట్లో లేరు|బయటికి వెళ్ళారు|ఊర్లో లేరు|నిద్రపోతున్నారు|పనిలో ఉన్నారు|"
    r"(ఆయన|ఆవిడ|అతని|ఆమె)\s*(భార్య|భర్త|కొడుకు|కూతురు|తమ్ముడు|అన్నయ్య)|"
    r"నేను\s*(ఆయన|ఆవిడ|అతని|ఆమె)\s*(భార్యను|భర్తను|కొడుకును|కూతురును)", re.I)
NOT_AVAILABLE_LINE = ("సరేనండి, తర్వాత ఆయనకి కాల్ చేస్తామండి. ధన్యవాదాలు, నమస్కారం!")

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


# ------------------------------------------------------------------ call queue

# campaign-dispatch hands contacts over as fast as it likes (two a minute,
# any hour). Agent-script calls are held here instead and placed ONE AT A
# TIME (now up to MAX_CONCURRENT_CALLS), only inside calling hours (owner's ask, 2026-10-19: no Supabase
# deploy needed). The queue is saved under logs/ so a restart or redeploy
# picks up where it left off.
#
# Limit: the server cannot see a dashboard "Cancel"; numbers already handed
# over are still called. clear_queue() (POST /agent-queue/clear) empties it.

import asyncio  # noqa: E402
import datetime as _dt  # noqa: E402

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
CALL_START_MIN_IST = 10 * 60        # 10:00 IST
CALL_END_MIN_IST = 19 * 60 - 5      # no new call after 18:55 IST
GAP_BETWEEN_CALLS_S = 20
# Three at a time (owner's choice, 2026-10-20; was 4). The Vobiz account's
# simultaneous-call limit is shared with inbound calls: if it is lower than
# this plus the inbound calls in progress, the extra dial is refused and is
# reported as no_answer, so campaign-outcome retries that contact. At 4 a
# campaign of 178 contacts came back 32 USER_BUSY against 7 normal calls --
# the account ceiling is 3, so the fourth dial was always refused.
MAX_CONCURRENT_CALLS = 3
ACTIVE_CALL_TIMEOUT_S = 20 * 60     # a call whose hangup never arrives stops blocking

_QUEUE_FILE = Path(__file__).resolve().parent / "logs" / "agent_script_queue.json"
_queue: list[dict] | None = None
_active: dict[str, float] = {}      # request_uuid -> when it was placed
_wake: asyncio.Event | None = None
_runner_task: asyncio.Task | None = None


# SWITCH: calling hours. Off since 2026-10-19 (owner: "remove time rule") --
# queued calls go out at any hour. Set True to call only 10:00-18:55 IST.
CALLING_HOURS_ON = False


def within_hours(now: _dt.datetime | None = None) -> bool:
    if not CALLING_HOURS_ON:
        return True
    t = now or _dt.datetime.now(IST)
    minutes = t.hour * 60 + t.minute
    return CALL_START_MIN_IST <= minutes < CALL_END_MIN_IST


def _load_queue() -> list[dict]:
    global _queue
    if _queue is None:
        try:
            _queue = json.loads(_QUEUE_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _queue = []
        except Exception:
            log.exception("could not read %s", _QUEUE_FILE)
            _queue = []
    return _queue


def _save_queue() -> None:
    try:
        _QUEUE_FILE.parent.mkdir(exist_ok=True)
        tmp = _QUEUE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_load_queue(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(_QUEUE_FILE)
    except Exception:
        log.exception("could not save the call queue")


def queue_call(body: dict) -> int:
    """Hold one /dial request; returns its place in the line (1 = next)."""
    q = _load_queue()
    contact = (body.get("campaign") or {}).get("contact_id")
    if contact is not None and any((i["body"].get("campaign") or {}).get("contact_id") == contact for i in q):
        return next(n for n, i in enumerate(q, 1) if (i["body"].get("campaign") or {}).get("contact_id") == contact)
    q.append({"body": body, "queued_at": time.time()})
    _save_queue()
    if _wake:
        _wake.set()
    log.info("call queue: %s held (campaign %s contact %s), %d waiting",
             body.get("to"), (body.get("campaign") or {}).get("campaign_id"), contact, len(q))
    return len(q)


def clear_queue() -> int:
    q = _load_queue()
    n = len(q)
    q.clear()
    _save_queue()
    log.info("call queue cleared (%d dropped)", n)
    return n


def queue_status() -> dict:
    return {"waiting": len(_load_queue()), "active": len(_active), "calling_hours": within_hours()}


def on_hangup(request_uuid: str | None) -> None:
    if request_uuid and _active.pop(request_uuid, None) is not None and _wake:
        _wake.set()


async def _sleep(seconds: float) -> None:
    _wake.clear()
    try:
        await asyncio.wait_for(_wake.wait(), seconds)
    except asyncio.TimeoutError:
        pass


# An account whose minutes are used up is not dialed (owner's ask,
# 2026-10-20): its contacts wait in the queue, and dialing resumes on its own
# once the account recharges. Other accounts' contacts are not held up.
BALANCE_RECHECK_S = 60
_balance_cache: dict = {}   # customer_id -> (has balance, checked at)
_waiting_logged: set = set()


async def _has_balance(has_balance, customer_id) -> bool:
    if has_balance is None or customer_id is None:
        return True
    ok, at = _balance_cache.get(customer_id, (None, 0.0))
    if ok is None or time.time() - at >= (BALANCE_RECHECK_S if not ok else 15):
        ok = bool(await has_balance(customer_id))
        _balance_cache[customer_id] = (ok, time.time())
        if not ok and customer_id not in _waiting_logged:
            _waiting_logged.add(customer_id)
            log.warning("call queue: customer %s is out of minutes -- their contacts wait for a recharge",
                        customer_id)
        elif ok and customer_id in _waiting_logged:
            _waiting_logged.discard(customer_id)
            log.info("call queue: customer %s has minutes again -- dialing resumes", customer_id)
    return ok


async def _run(place) -> None:
    """place(body) -> (ok, request_uuid, detail); report_failed(body, detail);
    has_balance(customer_id) -> bool, or None to dial without checking."""
    place_call, report_failed, has_balance = place
    log.info("call queue started: %d waiting, %d at a time, calling hours %s", len(_load_queue()),
             MAX_CONCURRENT_CALLS, "10:00-18:55 IST" if CALLING_HOURS_ON else "off (any time)")
    while True:
        try:
            q = _load_queue()
            now = time.time()
            for u, at in list(_active.items()):
                if now - at > ACTIVE_CALL_TIMEOUT_S:
                    _active.pop(u, None)
            if not q:
                await _sleep(30)
                continue
            if not within_hours():
                await _sleep(60)
                continue
            if len(_active) >= MAX_CONCURRENT_CALLS:
                await _sleep(10)
                continue
            item = None
            for candidate in q:
                customer = (candidate["body"].get("campaign") or {}).get("customer_id")
                if await _has_balance(has_balance, customer):
                    item = candidate
                    break
            if item is None:
                await _sleep(BALANCE_RECHECK_S)
                continue
            # Re-checked right before dialing: the cached answer may be
            # up to 15 s old and calls in progress use minutes meanwhile.
            customer = (item["body"].get("campaign") or {}).get("customer_id")
            _balance_cache.pop(customer, None)
            if not await _has_balance(has_balance, customer):
                continue
            try:
                ok, request_uuid, detail = await place_call(item["body"])
            except Exception as e:
                log.exception("call queue: dial failed")
                ok, request_uuid, detail = False, None, str(e)
            q.remove(item)
            _save_queue()
            if ok and request_uuid:
                _active[request_uuid] = time.time()
            else:
                await report_failed(item["body"], detail)
            await asyncio.sleep(GAP_BETWEEN_CALLS_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("call queue error")
            await asyncio.sleep(30)


def start_queue(place_call, report_failed, has_balance=None) -> None:
    global _wake, _runner_task
    if _runner_task is None:
        _wake = asyncio.Event()
        _runner_task = asyncio.create_task(_run((place_call, report_failed, has_balance)))
