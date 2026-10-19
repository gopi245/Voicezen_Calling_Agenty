"""Excel campaigns: upload a sheet of leads, the server calls them one by one.

Built 2026-10-19 for Vedantha Vanam. A private page on this server
(/campaigns, HTTP Basic auth with CAMPAIGN_PAGE_PASSWORD) takes an .xlsx or
.csv of leads (a phone column, optionally a name column), and a background
runner dials them ONE AT A TIME from the campaign line, only between
CALL_START_HOUR and CALL_END_HOUR IST. Each answered call is an ordinary
outbound call on that line's registry entry, so it speaks the same
script and gets every server-side guard the inbound calls have.

Everything lives in one JSON file under logs/ (a volume that survives
redeploys). Anyone who says "don't call me" goes on a do-not-call list and
is skipped by every later campaign.

The server owns the phone side: init() gets its dial function, and the
server reports back with on_hangup() (every call) and on_call_result()
(answered calls, from CallSession._report).
"""
from __future__ import annotations

import asyncio
import base64
import csv
import datetime as dt
import html
import io
import json
import logging
import os
import re
import secrets
import time
import uuid
from pathlib import Path

log = logging.getLogger("excel_campaign")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CALL_START_HOUR = 10   # 10:00 IST
CALL_END_HOUR = 19     # no new call after 18:55 IST
LAST_DIAL_MINUTES_BEFORE_END = 5
GAP_BETWEEN_CALLS_S = 20
RING_TIMEOUT_S = 20 * 60          # a call that never reports back is given up on
RESULT_WAIT_AFTER_HANGUP_S = 120  # answered call: wait this long for the report
MAX_ATTEMPTS = 2                  # one retry for no answer / busy
RETRY_AFTER_S = 2 * 60 * 60

LINE = os.getenv("EXCEL_CAMPAIGN_LINE", "+918071580257")
PAGE_PASSWORD = os.getenv("CAMPAIGN_PAGE_PASSWORD", "")

# Said by the caller: never call them again.
OPT_OUT_RX = re.compile(
    r"(కాల్|ఫోన్|కాల్స్)\s*(లు\s*)?(చేయ|చెయ్య|చేయ్య|చెయ)\s*(కండి|కండీ|ొద్దు|వద్దు|కూడదు|ద్దు)|"
    r"(కాల్|ఫోన్)\s*(చేయ|చెయ్య)?\s*(అవసరం లేదు|వద్దు)|"
    r"నా నంబర్\s*(తీసేయండి|తీసివేయండి|డిలీట్)|నంబర్ (తీసేయండి|డిలీట్ చేయండి)|"
    r"don'?t call|do not call|stop calling|remove my number|never call",
    re.I,
)
_NOT_NOW_RX = re.compile(r"ఇప్పుడు|తర్వాత|తరువాత|రేపు|సాయంత్రం|later|right now|now", re.I)


def wants_opt_out(text: str | None) -> bool:
    """ "Don't call me" -- but not "don't call now, call later", which is busy."""
    return bool(OPT_OUT_RX.search(text or "")) and not _NOT_NOW_RX.search(text or "")


_BUSY_RX = re.compile(
    r"బిజీ|busy|ఇప్పుడు (కాదు|మాట్లాడలేను|వీలు కాదు|కుదరదు|టైం లేదు)|డ్రైవింగ్|బండి నడుపుతున్నా|మీటింగ్|"
    r"(తర్వాత|తరువాత|సాయంత్రం|రేపు)\s*(కాల్|ఫోన్|చేయండి|చెయ్యండి|మాట్లాడ)", re.I)


def wants_later(text: str | None) -> bool:
    """Busy now: "call me later", "I'm driving", "don't call now, later"."""
    return bool(_BUSY_RX.search(text or ""))


BUSY_LINE = "సరేనండి, ఇబ్బంది లేదు. తర్వాత మళ్ళీ కాల్ చేస్తామండి. ధన్యవాదాలు, నమస్కారం!"

# Only "hello" right after the greeting: they didn't hear it.
HELLO_RX = re.compile(r"^\s*((హలో|హల్లో|hello|హాయ్|ఆ|ఏ)[\s,.!?]*){1,3}(అండి|సార్)?[\s.!?]*$", re.I)
HELLO_LINE = "నమస్కారం సార్, వేదాంత వనం ప్రాజెక్ట్ నుంచి మాట్లాడుతున్నాను. రెండు నిమిషాలు మాట్లాడవచ్చా సార్?"

WRONG_NUMBER_RX = re.compile(r"రాంగ్ నంబర్|రాంగ్ నెంబర్|wrong number|తప్పు నంబర్|ఎంక్వయిరీ చేయలేదు|ఎంక్వైరీ చేయలేదు|"
                             r"నేను (ఏమీ )?అడగలేదు|నేను ఎవరికీ నంబర్ ఇవ్వలేదు", re.I)
WRONG_NUMBER_LINE = "క్షమించండి సార్, పొరపాటు జరిగింది. ఇకపై మీకు కాల్ చేయము. నమస్కారం!"

OPT_OUT_LINE = "సరేనండి, క్షమించండి. ఇకపై మీకు కాల్ చేయము. ధన్యవాదాలు, నమస్కారం!"

# How did you get my number?
NUMBER_SOURCE_RX = re.compile(
    r"(నా|మా)\s*నంబర్\s*(మీకు\s*)?(ఎక్కడ|ఎలా|ఎవరు)|నంబర్ ఎవరు ఇచ్చారు|ఎక్కడి నుంచి నంబర్|"
    r"how did you get my number|where did you get my number",
    re.I,
)
NUMBER_SOURCE_LINE = ("మీరు ఇంతకుముందు మా ప్రాజెక్ట్ గురించి వివరాలు అడిగారండి, అందుకే కాల్ చేశాను. "
                      "రెండు నిమిషాలు మాట్లాడవచ్చా సార్?")

# Added to the line's own instructions on these calls only.
OUTBOUND_NOTE = """

THIS IS AN OUTBOUND CALL: we called them. They enquired about Vedantha Vanam earlier and the opening line already said who you are and why you called. Do not ask how you can help them; follow the flow from the opening line's question. If they say this is a wrong number or they never enquired, apologise once and end politely."""

RESULT_LABELS = {
    "pending": "Waiting",
    "calling": "Calling now",
    "booked": "Visit booked",
    "interested": "Talked - interested",
    "talked": "Talked",
    "busy_later": "Busy - call later",
    "not_interested": "Not interested",
    "opted_out": "Do not call",
    "wrong_number": "Wrong number",
    "no_answer": "No answer",
    "busy": "Line busy",
    "failed": "Call failed",
    "skipped_dnc": "Skipped (do-not-call list)",
    "invalid": "Invalid number",
    "duplicate": "Duplicate in file",
}
RETRYABLE = {"no_answer", "busy", "failed", "busy_later"}  # busy_later: we said we would call back

_dir = Path(__file__).resolve().parent / "logs"
_STATE_FILE = _dir / "excel_campaigns.json"
_state: dict = {"campaigns": {}, "dnc": {}}
_place_call = None            # async (to, from_number, excel_ref) -> (ok, request_uuid, detail)
_waiting: dict[str, dict] = {}  # request_uuid -> {"done": Event, "hangup": Event, "contact": dict}
_runner_task: asyncio.Task | None = None
_wake = asyncio.Event()


# ------------------------------------------------------------------ storage

def _load() -> None:
    global _state
    try:
        _state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        _state.setdefault("campaigns", {})
        _state.setdefault("dnc", {})
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("could not read %s -- starting empty", _STATE_FILE)
    # A restart while a call was ringing: that contact is tried again.
    for c in _state["campaigns"].values():
        for k in c["contacts"]:
            if k["status"] == "calling":
                k["status"] = "failed"
                k["note"] = "server restarted during the call"


def _save() -> None:
    try:
        _dir.mkdir(exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception:
        log.exception("could not save excel campaigns")


# ------------------------------------------------------------------ phones

def normalize_phone(raw) -> str | None:
    """Indian mobile numbers only: +91 and 10 digits starting 6-9."""
    if raw is None:
        return None
    if isinstance(raw, float):
        raw = str(int(raw))
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def is_dnc(phone: str | None) -> bool:
    return bool(phone) and phone in _state["dnc"]


def add_dnc(phone: str | None, reason: str) -> None:
    if phone and phone not in _state["dnc"]:
        _state["dnc"][phone] = {"at": time.time(), "reason": reason}
        _save()
        log.info("do-not-call: %s (%s)", phone, reason)


# ------------------------------------------------------------------ parsing

_PHONE_HEAD = re.compile(r"phone|mobile|number|contact|cell|whats|ఫోన్|నంబర్|మొబైల్", re.I)
_NAME_HEAD = re.compile(r"name|పేరు", re.I)


def _rows_from_upload(filename: str, data: bytes) -> list[list]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if name.endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        return [r for r in csv.reader(io.StringIO(text))]
    raise ValueError("Please upload an .xlsx or .csv file (old .xls: open it in Excel and Save As .xlsx).")


def parse_contacts(filename: str, data: bytes) -> list[dict]:
    rows = [r for r in _rows_from_upload(filename, data) if any(c not in (None, "") for c in r)]
    if not rows:
        raise ValueError("The file is empty.")
    head = [str(c or "").strip() for c in rows[0]]
    phone_col = next((i for i, h in enumerate(head) if _PHONE_HEAD.search(h)), None)
    name_col = next((i for i, h in enumerate(head) if _NAME_HEAD.search(h) and i != phone_col), None)
    body = rows[1:]
    if phone_col is None:
        # No header row: the first column that holds phone numbers.
        body = rows
        width = max(len(r) for r in rows)
        phone_col = next((i for i in range(width)
                          if sum(1 for r in rows[:20] if i < len(r) and normalize_phone(r[i])) >= 1), None)
        if phone_col is None:
            raise ValueError("No phone number column found. Put the numbers in a column named 'Phone'.")
        name_col = next((i for i in range(width) if i != phone_col and any(
            i < len(r) and isinstance(r[i], str) and r[i].strip() and not normalize_phone(r[i]) for r in rows[:20])),
            None)
    contacts, seen = [], set()
    for r in body:
        raw = r[phone_col] if phone_col < len(r) else None
        if raw in (None, ""):
            continue
        nm = str(r[name_col]).strip() if name_col is not None and name_col < len(r) and r[name_col] else ""
        phone = normalize_phone(raw)
        status = "pending"
        if not phone:
            status = "invalid"
        elif phone in seen:
            status = "duplicate"
        elif is_dnc(phone):
            status = "skipped_dnc"
        if phone:
            seen.add(phone)
        contacts.append({"phone": phone or str(raw), "name": nm, "status": status, "attempts": 0,
                         "last_at": None, "duration": None, "visit_day": None, "summary": None,
                         "caller_said": None, "note": None})
    if not contacts:
        raise ValueError("No phone numbers found in the file.")
    return contacts


def create_campaign(name: str, filename: str, data: bytes) -> dict:
    contacts = parse_contacts(filename, data)
    cid = uuid.uuid4().hex[:8]
    c = {"id": cid, "name": (name or filename or "Campaign").strip()[:80], "file": filename,
         "created": time.time(), "status": "paused", "line": LINE, "contacts": contacts}
    _state["campaigns"][cid] = c
    _save()
    log.info("excel campaign %s created: %s, %d contacts", cid, c["name"], len(contacts))
    return c


# ------------------------------------------------------------------ hours

def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def within_hours(t: dt.datetime | None = None) -> bool:
    t = t or now_ist()
    start = t.replace(hour=CALL_START_HOUR, minute=0, second=0, microsecond=0)
    end = t.replace(hour=CALL_END_HOUR, minute=0, second=0, microsecond=0) - dt.timedelta(
        minutes=LAST_DIAL_MINUTES_BEFORE_END)
    return start <= t < end


# ------------------------------------------------------------------ runner

def _next_contact(c: dict) -> dict | None:
    for k in c["contacts"]:
        if k["status"] == "pending":
            if is_dnc(k["phone"]):
                k["status"] = "skipped_dnc"
                continue
            return k
    now = time.time()
    for k in c["contacts"]:
        if (k["status"] in RETRYABLE and k["attempts"] < MAX_ATTEMPTS
                and now - (k["last_at"] or 0) >= RETRY_AFTER_S and not is_dnc(k["phone"])):
            return k
    return None


def _has_retries_waiting(c: dict) -> bool:
    return any(k["status"] in RETRYABLE and k["attempts"] < MAX_ATTEMPTS and not is_dnc(k["phone"])
               for k in c["contacts"])


def _running_campaign() -> dict | None:
    running = [c for c in _state["campaigns"].values() if c["status"] == "running"]
    return min(running, key=lambda c: c["created"]) if running else None


async def _sleep_or_wake(seconds: float) -> None:
    _wake.clear()
    try:
        await asyncio.wait_for(_wake.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def _call_one(c: dict, k: dict) -> None:
    k["status"] = "calling"
    k["attempts"] += 1
    k["last_at"] = time.time()
    k["note"] = None
    _save()
    ref = {"campaign_id": c["id"], "phone": k["phone"]}
    try:
        ok, request_uuid, detail = await _place_call(k["phone"], c["line"], ref)
    except Exception as e:
        log.exception("excel campaign dial failed")
        ok, request_uuid, detail = False, None, str(e)
    if not ok or not request_uuid:
        k["status"] = "failed"
        k["note"] = f"dial failed: {detail}"[:200]
        _save()
        return
    w = {"done": asyncio.Event(), "hangup": asyncio.Event(), "contact": k, "answered": False}
    _waiting[request_uuid] = w
    log.info("excel campaign %s: calling %s (attempt %d)", c["id"], k["phone"], k["attempts"])
    try:
        await asyncio.wait_for(w["hangup"].wait(), RING_TIMEOUT_S)
        if w["answered"] and not w["done"].is_set():
            await asyncio.wait_for(w["done"].wait(), RESULT_WAIT_AFTER_HANGUP_S)
    except asyncio.TimeoutError:
        if k["status"] == "calling":
            k["status"] = "talked" if w["answered"] else "failed"
            k["note"] = "no report from the call"
    finally:
        _waiting.pop(request_uuid, None)
        if k["status"] == "calling":
            k["status"] = "talked" if w["answered"] else "failed"
        _save()


async def _runner() -> None:
    log.info("excel campaign runner started (line %s, %02d:00-%02d:00 IST)", LINE, CALL_START_HOUR, CALL_END_HOUR)
    while True:
        try:
            c = _running_campaign()
            if not c:
                await _sleep_or_wake(30)
                continue
            if not within_hours():
                await _sleep_or_wake(60)
                continue
            k = _next_contact(c)
            if not k:
                if not _has_retries_waiting(c):
                    c["status"] = "done"
                    c["finished"] = time.time()
                    _save()
                    log.info("excel campaign %s finished", c["id"])
                else:
                    await _sleep_or_wake(60)
                continue
            await _call_one(c, k)
            await asyncio.sleep(GAP_BETWEEN_CALLS_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("excel campaign runner error")
            await asyncio.sleep(30)


def init(place_call) -> None:
    """Called once at server startup with the server's dial function."""
    global _place_call, _runner_task
    _place_call = place_call
    _load()
    _save()
    if _runner_task is None:
        _runner_task = asyncio.create_task(_runner())


# ------------------------------------------------------------------ server hooks

def is_campaign_call(request_uuid: str | None) -> bool:
    return bool(request_uuid) and request_uuid in _waiting


def on_answered(request_uuid: str | None) -> None:
    w = _waiting.get(request_uuid or "")
    if w:
        w["answered"] = True


def on_hangup(request_uuid: str | None, cause: str | None, duration, answered: bool) -> None:
    """Every campaign call ends here, answered or not."""
    w = _waiting.get(request_uuid or "")
    if not w:
        return
    k = w["contact"]
    w["answered"] = w["answered"] or answered
    if not w["answered"] and k["status"] == "calling":
        cause_l = (cause or "").lower()
        k["status"] = "busy" if "busy" in cause_l else "no_answer"
        k["note"] = cause
        k["duration"] = 0
        _save()
        w["done"].set()
    w["hangup"].set()


def on_call_result(request_uuid: str | None, result: dict) -> None:
    """An answered call's outcome, from CallSession._report."""
    w = _waiting.get(request_uuid or "")
    if not w:
        return
    k = w["contact"]
    k.update({key: result.get(key) for key in ("status", "duration", "visit_day", "caller_said")})
    if result.get("status") == "opted_out":
        add_dnc(k["phone"], "said do not call")
    _save()
    w["answered"] = True
    w["done"].set()


def add_summary(request_uuid_or_phone: str | None, summary: str | None, name: str | None) -> None:
    """The call summary arrives later than the result -- matched by phone."""
    if not summary and not name:
        return
    for c in _state["campaigns"].values():
        for k in c["contacts"]:
            if k["phone"] == request_uuid_or_phone and k["last_at"] and time.time() - k["last_at"] < 3600:
                if summary:
                    k["summary"] = summary
                if name and not k["name"]:
                    k["name"] = name
    _save()


# ------------------------------------------------------------------ page

def check_auth(header: str | None) -> bool:
    if not PAGE_PASSWORD or not header or not header.lower().startswith("basic "):
        return False
    try:
        user_pass = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", "replace")
    except Exception:
        return False
    return secrets.compare_digest(user_pass.split(":", 1)[-1], PAGE_PASSWORD)


def _counts(c: dict) -> dict:
    out: dict = {}
    for k in c["contacts"]:
        out[k["status"]] = out.get(k["status"], 0) + 1
    return out


def state_json() -> dict:
    t = now_ist()
    camps = []
    for c in sorted(_state["campaigns"].values(), key=lambda c: -c["created"]):
        camps.append({"id": c["id"], "name": c["name"], "status": c["status"], "file": c.get("file"),
                      "created": dt.datetime.fromtimestamp(c["created"], IST).strftime("%d %b %H:%M"),
                      "total": len(c["contacts"]), "counts": _counts(c),
                      "contacts": [{**k, "label": RESULT_LABELS.get(k["status"], k["status"]),
                                    "last": dt.datetime.fromtimestamp(k["last_at"], IST).strftime("%d %b %H:%M")
                                    if k["last_at"] else ""} for k in c["contacts"]]})
    return {"now": t.strftime("%d %b %Y, %I:%M %p IST"), "calling_hours": within_hours(t),
            "hours": f"{CALL_START_HOUR}:00 AM - {CALL_END_HOUR - 12}:00 PM IST", "line": LINE,
            "dnc": len(_state["dnc"]), "campaigns": camps}


def set_status(cid: str, action: str) -> str | None:
    c = _state["campaigns"].get(cid)
    if not c:
        return "not found"
    if action == "start":
        if c["status"] == "running":
            return None
        c["status"] = "running"
    elif action == "pause":
        c["status"] = "paused"
    elif action == "delete":
        if any(k["status"] == "calling" for k in c["contacts"]):
            return "a call is in progress -- pause, then delete after it ends"
        del _state["campaigns"][cid]
    else:
        return "unknown action"
    _save()
    _wake.set()
    log.info("excel campaign %s: %s", cid, action)
    return None


def results_csv(cid: str) -> str | None:
    c = _state["campaigns"].get(cid)
    if not c:
        return None
    buf = io.StringIO()
    buf.write("﻿")  # Excel opens Telugu text correctly with a BOM
    w = csv.writer(buf)
    w.writerow(["Name", "Phone", "Result", "Visit day", "Attempts", "Last call (IST)", "Talk time (s)",
                "Summary", "What they said", "Note"])
    for k in c["contacts"]:
        w.writerow([k["name"], k["phone"], RESULT_LABELS.get(k["status"], k["status"]), k["visit_day"] or "",
                    k["attempts"], dt.datetime.fromtimestamp(k["last_at"], IST).strftime("%Y-%m-%d %H:%M")
                    if k["last_at"] else "", k["duration"] if k["duration"] is not None else "",
                    k["summary"] or "", k["caller_said"] or "", k["note"] or ""])
    return buf.getvalue()


PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Call Campaigns</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#667085;--line:#e4e7ec;--accent:#1f7a4d;--warn:#b54708;--bad:#b42318}
@media (prefers-color-scheme: dark){:root{--bg:#14171c;--card:#1d2128;--text:#e7e9ee;--muted:#98a2b3;--line:#2c323c;--accent:#3ccb7f;--warn:#f79009;--bad:#f97066}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:14px 0}
input[type=text]{padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text);width:100%;max-width:320px}
button,.btn{padding:7px 14px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer;font:inherit;text-decoration:none;display:inline-block}
button.go{background:var(--accent);color:#fff;border-color:var(--accent)}button.del{color:var(--bad)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.pill{padding:2px 9px;border-radius:99px;font-size:13px;border:1px solid var(--line)}
.running{color:var(--accent);border-color:var(--accent)}.paused{color:var(--warn);border-color:var(--warn)}
.tablewrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px;margin-top:10px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}
.stats span{margin-right:14px}#msg{min-height:20px}.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:8px}.bar i{display:block;height:100%;background:var(--accent)}
details summary{cursor:pointer;color:var(--muted);margin-top:8px}
</style></head><body><main>
<h1>Call Campaigns</h1>
<div class="muted" id="top"></div>
<div class="card">
<b>New campaign</b>
<p class="muted" style="margin:6px 0 10px">Excel (.xlsx) or CSV with a <b>Phone</b> column and an optional <b>Name</b> column. Calls are made one at a time from the line below, only in calling hours, using the Vedantha Vanam script. Anyone who says "don't call" is never called again.</p>
<form id="up" class="row"><input type="text" name="name" placeholder="Campaign name"><input type="file" name="file" accept=".xlsx,.csv" required><button class="go">Upload</button></form>
<div id="msg"></div>
</div>
<div id="list"></div>
</main>
<script>
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const done=["booked","interested","talked","not_interested","opted_out","wrong_number","skipped_dnc","invalid","duplicate"];
async function load(){
 const r=await fetch("campaigns/api/state");if(!r.ok)return;const s=await r.json();
 document.getElementById("top").textContent=`${s.now} · Line ${s.line} · Calling hours ${s.hours} (${s.calling_hours?"open now":"closed now"}) · Do-not-call list: ${s.dnc}`;
 const open=new Set([...document.querySelectorAll("details[open]")].map(d=>d.dataset.id));
 document.getElementById("list").innerHTML=s.campaigns.map(c=>{
  const n=c.counts,fin=c.contacts.filter(k=>done.includes(k.status)||(k.attempts>=2&&k.status!=="calling")).length;
  const pct=c.total?Math.round(100*fin/c.total):0;
  return `<div class="card"><div class="row"><b>${esc(c.name)}</b><span class="pill ${c.status}">${c.status}</span><span class="muted">${esc(c.file)} · ${c.created}</span></div>
  <div class="stats muted" style="margin-top:6px">${Object.entries(n).map(([k,v])=>`<span>${esc(labelOf(c,k))}: <b>${v}</b></span>`).join("")}</div>
  <div class="bar"><i style="width:${pct}%"></i></div>
  <div class="row" style="margin-top:10px">${c.status==="running"?`<button onclick="act('${c.id}','pause')">Pause</button>`:c.status==="done"?"":`<button class="go" onclick="act('${c.id}','start')">Start calling</button>`}
  <a class="btn" href="campaigns/${c.id}/results.csv">Download results</a><button class="del" onclick="if(confirm('Delete this campaign?'))act('${c.id}','delete')">Delete</button></div>
  <details data-id="${c.id}" ${open.has(c.id)?"open":""}><summary>Contacts (${c.total})</summary><div class="tablewrap"><table><tr><th>Name</th><th>Phone</th><th>Result</th><th>Visit day</th><th>Tries</th><th>Last call</th><th>Summary</th></tr>
  ${c.contacts.map(k=>`<tr><td>${esc(k.name)}</td><td>${esc(k.phone)}</td><td>${esc(k.label)}</td><td>${esc(k.visit_day)}</td><td>${k.attempts}</td><td>${esc(k.last)}</td><td>${esc(k.summary||k.note)}</td></tr>`).join("")}</table></div></details></div>`}).join("")||'<p class="muted">No campaigns yet.</p>';
}
function labelOf(c,k){const x=c.contacts.find(z=>z.status===k);return x?x.label:k}
async function act(id,a){const r=await fetch(`campaigns/${id}/${a}`,{method:"POST"});const j=await r.json();if(j.error)alert(j.error);load()}
document.getElementById("up").onsubmit=async e=>{e.preventDefault();const m=document.getElementById("msg");m.textContent="Uploading...";
 const r=await fetch("campaigns/upload",{method:"POST",body:new FormData(e.target)});const j=await r.json();
 m.textContent=j.error?j.error:`Added "${j.name}": ${j.total} contacts (${j.ready} ready to call). Press Start calling when ready.`;e.target.reset();load()};
load();setInterval(load,5000);
</script></body></html>"""
