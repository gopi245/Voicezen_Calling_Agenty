"""LiveKit-free Telugu voice agent -- talks to Vobiz directly.

Vobiz calls POST /answer when a call arrives; we return XML that opens a
bidirectional WebSocket stream to /media. Over that WebSocket we get the
caller's audio as it arrives and push the bot's speech back, with no SIP
trunk, no LiveKit room, no agent dispatch -- just this process and two HTTP
APIs (Sarvam STT, Raya or Rumik TTS) plus one LLM call per turn.

Protocol details below come from Vobiz's docs (docs.vobiz.ai) and, for
Sarvam's streaming STT, from reading the working livekit-plugins-sarvam
source directly rather than guessing at the wire format from prose.

Reuses, unchanged: each client's <module>.INSTRUCTIONS/GREETING (the script
logic has nothing to do with how the call is transported), and the Raya TTS
client built for the LiveKit plugin (agent/tts_raya.py's
_synthesize_to_emitter logic, adapted here to write straight to the
WebSocket instead of a LiveKit AudioEmitter).

Multi-client: which client a call belongs to is resolved from the dialed
number (clients_registry.py), the same job LiveKit's dispatch-rule metadata
used to do. One process serves every client; nothing here is per-client
except what that lookup returns.
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
import os
import struct
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

import aiohttp
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "agent"))
load_dotenv(HERE / ".env")

import voicezen  # noqa: E402
from clients_registry import lookup as lookup_client  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vobiz-native")

app = FastAPI()

RAYA_API_KEY = os.environ["RAYA_API_KEY"]
RAYA_VOICE_ID = os.getenv("RAYA_VOICE_ID", "25a7c7d9-57b3-488a-a880-33edf6642902")
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
SARVAM_API_KEY = os.environ["SARVAM_API_KEY"]
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3.5-flash-lite")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "400"))

# Public wss:// URL for this service (sslip.io -- no DNS setup, Caddy issues
# a real cert for it automatically). Set once the Caddy route is live.
PUBLIC_WSS_URL = os.getenv("PUBLIC_WSS_URL", "wss://vobiz-native.185-199-52-73.sslip.io/media")

VOBIZ_AUTH_ID = os.environ["VOBIZ_AUTH_ID"]
VOBIZ_AUTH_TOKEN = os.environ["VOBIZ_AUTH_TOKEN"]
VOBIZ_API = "https://api.vobiz.ai/api/v1"

# Caller ID for outbound calls this service places. Same number as inbound --
# +918071580257 is the one number this project uses in both directions.
DIAL_FROM_NUMBER = os.getenv("DIAL_FROM_NUMBER", "+918071580257")

# /dial has no other access control -- without this, anyone who found the
# URL could place calls on this Vobiz account for free. campaign-dispatch
# (the Supabase function that drives bulk campaign calling) sends the same
# value in X-Dial-Secret.
DIAL_SECRET = os.getenv("DIAL_SECRET", "")

# Where a finished campaign call's outcome is reported -- a different
# endpoint from voicezen.py's log-call, because a campaign call's customer
# is already known directly (campaigns.customer_id) and doesn't need, or
# want, the dialled-number lookup log-call does for regular calls.
CAMPAIGN_OUTCOME_URL = os.getenv(
    "CAMPAIGN_OUTCOME_URL",
    os.getenv("VOICEZEN_URL", "").rstrip("/") + "/functions/v1/campaign-outcome"
    if os.getenv("VOICEZEN_URL") else "",
)
VOICE_AGENT_SECRET = os.getenv("VOICEZEN_AGENT_SECRET", "")
VOICEZEN_ANON_KEY = os.getenv("VOICEZEN_ANON_KEY", "")

TTS_SAMPLE_RATE = 24000  # Raya's native output; Vobiz playAudio accepts L16/24000 directly.
STT_SAMPLE_RATE = 16000  # Vobiz inbound contentType we request; Sarvam recommends 16k.

# One caller sentence often arrives as several separate final transcripts in
# quick succession. Without this, each one became its own turn: a real call
# produced three bot replies inside two seconds, talking over itself, because
# "అవును." / "అవును సార్." / "సబ్‌స్క్రిప్షన్ ప్లాన్స్ ఎలా ఉన్నాయండి?" arrived as
# three transcripts. Wait this long after a transcript for more to arrive, and
# answer the combined text as ONE turn.
TURN_DEBOUNCE_S = float(os.getenv("TURN_DEBOUNCE_S", "0.9"))

# Domain vocabulary. The brand name is consistently mis-heard on real calls
# ("గుండు సోతి", "గుండుకో జ్ఞానం"), which is exactly the problem the production
# agent solved the same way -- saaras models accept a config prompt to bias
# recognition toward these words.
STT_PROMPT = (
    "గుండుసూది, పిన్ మీడియా, మ్యాగజైన్, సబ్‌స్క్రిప్షన్, ప్రింట్, డిజిటల్, "
    "ఎడిషన్, డెలివరీ, పేమెంట్, వాట్సాప్, పిన్‌కోడ్, చిరునామా, ఇంటి నెంబర్, "
    "వీధి, ఊరు, జిల్లా, రూపాయలు, ఆరు నెలలు, ఏడాది, నెలవారీ, వార్షిక"
)

# The system prompt has no notion of ending a call (that was a LiveKit
# function_tool before). Ask the model to mark it in-band instead.
END_MARKER = "[[END_CALL]]"


def _instructions_no_livekit(base_instructions: str) -> str:
    return base_instructions + (
        f"\n\nWhen you have said goodbye and the call should end, append the "
        f"exact literal text {END_MARKER} at the very end of that message, "
        f"after your spoken words. Do not say {END_MARKER} out loud -- it is "
        f"a control marker, stripped before your words are spoken."
    )


def load_client_script(dialed_number: str | None):
    """Resolve which client owns this number and load their prompt module.

    Falls back to Gundusoodi if the number isn't in the registry (e.g. the
    "start" event's callId lookup missed, or a number was dialed that was
    never registered) -- answering with the wrong-but-known script is safer
    than a call with no prompt at all.
    """
    client = lookup_client(dialed_number) or lookup_client("+918065353946")
    module = importlib.import_module(client["prompt_module"])
    return client, module.GREETING, module.INSTRUCTIONS


# A generic outbound-sales-call template, filled in per campaign at dial
# time -- proven against real scenarios in campaign_demo.py before this was
# wired into a real call. Deliberately one skeleton, not one prompt per
# campaign: the script comes entirely from the launch form's fields, so the
# same code path serves any client's campaign with no per-client branching.
CAMPAIGN_TEMPLATE = """You are an AI calling agent making an OUTBOUND call on behalf of {business_name}.

Goal of this call:
{offering}

If the person sounds interested:
{call_to_action}

If they raise concerns or objections:
{objection_handling}

Speak in {language}. Keep replies short -- this is a phone call, not an email.
Be polite, get to the point fast, and never pretend to be a human if asked directly --
say you're an AI calling on behalf of {business_name}.
If they ask "how did you get my number" or "who is this", explain you're calling on behalf
of {business_name} about a current offer, and offer to remove them from future calls if asked.
If they ask to speak to a human, be honest that none is available on this call, but offer to
note their request or have someone call back.
"""


_CAMPAIGN_GREETINGS = {
    "English": "Hi{name}, I'm an AI assistant calling on behalf of {business_name}.",
    "Telugu": "నమస్కారం{name}, నేను {business_name} తరఫున మాట్లాడుతున్న AI అసిస్టెంట్‌ని.",
    "Hindi": "नमस्ते{name}, मैं {business_name} की ओर से बात कर रहा AI असिस्टेंट हूँ।",
    "Tamil": "வணக்கம்{name}, நான் {business_name} சார்பாக பேசும் AI உதவியாளர்.",
}


def campaign_greeting(campaign: dict) -> str:
    language = campaign.get("language") or "English"
    template = _CAMPAIGN_GREETINGS.get(language, _CAMPAIGN_GREETINGS["English"])
    name = campaign.get("contact_name")
    return template.format(name=f" {name}" if name else "", business_name=campaign["business_name"])


# ---------------------------------------------------------------- webhook

# /answer (HTTP) and /media (WebSocket) are separate connections for the same
# call, correlated only by CallUUID / callId. This bridges them so the
# WebSocket side -- which owns the transcript -- has the caller/dialed
# numbers to report the call with once it ends. Entries are removed once a
# CallSession claims them; a call that answer() sees but that never opens a
# media stream just leaks one small dict entry, not a real problem here.
pending_call_info: dict[str, dict] = {}


@app.post("/answer")
async def answer(request: Request):
    """Vobiz POSTs CallUUID/From/To here once a call connects -- for both
    directions, since an outbound call answers through this same route.
    We must respond with XML inside ~1-2s or the caller hears dead air.

    `direction` comes from a query param /dial appends to its own
    answer_url, not from anything Vobiz sends -- Vobiz's From/To fields
    don't say which side placed the call, they're just "originator" and
    "destination" either way. Without this, an outbound call's `to` (the
    person WE called) would get used to resolve which client the call
    belongs to and to load a script -- both wrong, since that number
    identifies the callee, not the account placing the call.
    """
    form = await request.form()
    call_uuid = form.get("CallUUID")
    direction = request.query_params.get("direction", "inbound")
    log.info("answer: call=%s from=%s to=%s direction=%s",
              call_uuid, form.get("From"), form.get("To"), direction)
    if call_uuid:
        # /dial may have already stashed a `campaign` dict here, keyed by the
        # same UUID Vobiz assigns (its request_uuid becomes this CallUUID) --
        # update() rather than overwrite so that survives.
        entry = pending_call_info.setdefault(call_uuid, {})
        entry.update({
            "from": form.get("From"),
            "to": form.get("To"),
            "direction": direction,
        })

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true"
            contentType="audio/x-l16;rate={STT_SAMPLE_RATE}"
            statusCallbackUrl="{escape(str(request.base_url))}stream-status">
{PUBLIC_WSS_URL}
    </Stream>
</Response>"""
    return PlainTextResponse(xml, media_type="application/xml")


async def _report_unanswered_campaign_call(campaign: dict, cause: str | None) -> None:
    """A campaign call that never gets answered never reaches
    CallSession.start() -- that only fires once the /media WebSocket opens,
    which only happens if the callee actually picks up. Without this, such
    a contact would stay stuck on 'dialing' forever, since nothing else
    ever reports it. Reported the same way _report_campaign_call() does,
    just with a fixed no_answer outcome and no transcript to summarize."""
    if not CAMPAIGN_OUTCOME_URL or not VOICE_AGENT_SECRET:
        log.error("campaign call never answered but CAMPAIGN_OUTCOME_URL/secret "
                  "not configured -- outcome for contact %s is lost",
                  campaign.get("contact_id"))
        return
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                CAMPAIGN_OUTCOME_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-agent-secret": VOICE_AGENT_SECRET,
                    "apikey": VOICEZEN_ANON_KEY,
                    "Authorization": f"Bearer {VOICEZEN_ANON_KEY}",
                },
                json={
                    "campaign_id": campaign.get("campaign_id"),
                    "contact_id": campaign.get("contact_id"),
                    "customer_id": campaign.get("customer_id"),
                    "call_duration": 0,
                    "call_status": "completed",
                    "outcome": "no_answer",
                    "transcript": None,
                    "call_summary": f"Not answered (cause: {cause or 'unknown'})",
                    "language": campaign.get("language"),
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    log.error("campaign-outcome report (no_answer) failed: HTTP %s %s",
                              r.status, await r.text())
                else:
                    log.info("campaign call never answered: campaign=%s contact=%s cause=%s",
                              campaign.get("campaign_id"), campaign.get("contact_id"), cause)
    except Exception:
        log.exception("campaign-outcome (no_answer) request failed")


@app.post("/hangup")
async def hangup(request: Request):
    form = await request.form()
    call_uuid = form.get("CallUUID")
    cause = form.get("HangupCause")
    log.info("hangup: call=%s duration=%s cause=%s",
             call_uuid, form.get("Duration"), cause)

    # If this call's stashed info is still here, no CallSession ever claimed
    # it (that only happens once /media's "start" event fires), meaning the
    # callee never answered. A CallSession that WAS created already popped
    # and reported this itself via _report(), so this only fires for the
    # never-answered case.
    info = pending_call_info.pop(call_uuid, None) if call_uuid else None
    campaign = info.get("campaign") if info else None
    if campaign:
        await _report_unanswered_campaign_call(campaign, cause)

    return PlainTextResponse("OK")


@app.post("/dial")
async def dial(request: Request):
    """Place an outbound call. Reuses /answer and /hangup unchanged -- Vobiz
    calls answer_url the same way once an outbound call connects as it does
    for an inbound one, so the whole CallSession pipeline is identical.

    Optional `campaign` object in the body marks this as a campaign call --
    see CallSession.start() for how that changes which script gets spoken
    and where the outcome gets reported.
    """
    if DIAL_SECRET and request.headers.get("x-dial-secret") != DIAL_SECRET:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    to = body.get("to")
    if not to:
        return JSONResponse({"error": "missing 'to'"}, status_code=400)
    campaign = body.get("campaign")

    # request.base_url reflects what uvicorn itself saw, not what the caller
    # actually reached -- Caddy terminates TLS in front of this container and
    # uvicorn isn't told (no --proxy-headers), so this always comes back
    # http:// even when the real request was https://. Vobiz rejects an
    # http answer_url outright ("answer_url parameter is not valid"), so
    # this derives the public base from PUBLIC_WSS_URL (wss://.../media)
    # instead of trusting the request at all.
    base = PUBLIC_WSS_URL.replace("wss://", "https://").rsplit("/media", 1)[0]
    payload = {
        "from": DIAL_FROM_NUMBER,
        "to": to,
        # The ?direction=outbound query param is how /answer tells this call
        # apart from a real inbound one -- see the comment there.
        "answer_url": f"{base}/answer?direction=outbound",
        "answer_method": "POST",
        "hangup_url": f"{base}/hangup",
        "hangup_method": "POST",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{VOBIZ_API}/Account/{VOBIZ_AUTH_ID}/Call/",
            headers={"X-Auth-ID": VOBIZ_AUTH_ID, "X-Auth-Token": VOBIZ_AUTH_TOKEN,
                     "Content-Type": "application/json"},
            json=payload, timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            data = await r.json()
            log.info("dial: from=%s to=%s campaign=%s -> HTTP %s %s",
                      DIAL_FROM_NUMBER, to, bool(campaign), r.status, data)
            # Stash the campaign context under whatever UUID Vobiz just
            # handed back -- /answer's own webhook arrives with the same
            # UUID as CallUUID and merges into this same dict entry.
            request_uuid = data.get("request_uuid")
            # Vobiz returns 201 ("call queued") for a successful dial, not
            # 200 -- checking only for 200 silently dropped the campaign
            # context on every real call, since it never matched.
            if 200 <= r.status < 300 and campaign and request_uuid:
                pending_call_info[request_uuid] = {"campaign": campaign}
            return data


@app.post("/stream-status")
async def stream_status(request: Request):
    form = await request.form()
    log.info("stream-status: event=%s call=%s", form.get("Event"), form.get("CallUUID"))
    return PlainTextResponse("OK")


# ---------------------------------------------------------------- Sarvam STT

class SarvamSTT:
    """Raw client for Sarvam's legacy streaming WebSocket -- wire format read
    from the installed livekit-plugins-sarvam source, not guessed from docs.
    """

    URL = "wss://api.sarvam.ai/speech-to-text/ws"

    def __init__(self, on_transcript, on_speech_start):
        self._on_transcript = on_transcript
        self._on_speech_start = on_speech_start
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._recv_task: asyncio.Task | None = None

    async def connect(self):
        self._session = aiohttp.ClientSession()
        params = {
            # Pinned, not "unknown" (auto-detect). Two real calls showed
            # auto-detect misidentifying the caller's language on individual
            # utterances -- confident, wrong-script transcripts in Hindi,
            # Punjabi, Kannada, and English (9 of ~19 turns in one call),
            # mostly during short number-heavy phrases like a house number or
            # street name. Callers here are Telugu speakers; a caller who
            # genuinely code-switches to English mid-call is the tradeoff.
            "language-code": "te-IN",
            "model": "saaras:v3",
            "vad_signals": "true",
            "sample_rate": str(STT_SAMPLE_RATE),
            "flush_signal": "true",
            "mode": "transcribe",
            "input_audio_codec": "pcm_s16le",
            # NOT high_vad_sensitivity. That preset ends a speech segment after
            # ~64ms of silence, which cut callers off mid-sentence on a real
            # call ("ప్రింట్ మ్యాగజైన్ వన్." -- they were still saying "one
            # month"). These values give roughly a quarter second of silence
            # before the turn is considered over: still responsive, but it
            # tolerates the natural pause inside a phrase.
            "negative_frames_count": "8",
            "negative_frames_window": "12",
        }
        url = self.URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self._ws = await self._session.ws_connect(
            url, headers={"api-subscription-key": SARVAM_API_KEY}, timeout=30)
        # saaras models take a vocabulary prompt as an initial config frame.
        await self._ws.send_str(json.dumps({"type": "config", "prompt": STT_PROMPT}))
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("sarvam stt connected")

    async def _recv_loop(self):
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            msg_type = data.get("type")
            if msg_type == "data":
                transcript = (data.get("data") or {}).get("transcript", "")
                if transcript:
                    await self._on_transcript(transcript)
            elif msg_type in ("events", "event"):
                signal = (data.get("data") or {}).get("signal_type")
                if signal == "START_SPEECH":
                    await self._on_speech_start()
            elif msg_type in ("error", "errors"):
                log.warning("sarvam stt error: %s", data)

    async def send_audio(self, pcm16_bytes: bytes):
        if self._ws is None:
            return
        # Sarvam's per-message audio.encoding is a fixed enum of exactly
        # "audio/wav" -- it rejected "pcm_s16le" outright with a pydantic
        # validation error ("Input should be 'audio/wav'") the first time
        # this ran against a real call. The data is still raw PCM; the real
        # raw-format signal is `input_audio_codec` on the CONNECTION URL, set
        # in connect(). audio.sample_rate is also required per-message, not
        # only at connect time -- the same error reported it missing.
        b64 = base64.b64encode(pcm16_bytes).decode()
        try:
            await self._ws.send_str(json.dumps({
                "audio": {"data": b64, "encoding": "audio/wav",
                         "sample_rate": STT_SAMPLE_RATE}}))
        except Exception:
            # A dead STT socket must not take the whole call down with it --
            # this is exactly what crashed the /media WebSocket handler
            # (and with it the call) the first time this ran for real.
            log.warning("sarvam send_audio failed; STT socket likely closed")

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()


# ---------------------------------------------------------------- Raya TTS

async def synthesize(session: aiohttp.ClientSession, text: str):
    """Whole-turn Raya synthesis. Yields raw S16LE PCM chunks at TTS_SAMPLE_RATE.

    Same endpoint and F32LE->S16LE conversion as agent/tts_raya.py; ported here
    directly rather than importing that module, since it depends on a running
    LiveKit job context this process doesn't have.
    """
    body = {"text": text, "voice_id": RAYA_VOICE_ID, "model": "standard",
            "language": "te", "sample_rate": TTS_SAMPLE_RATE, "speed": 1}
    async with session.post(
        "https://hub.getraya.app/v1/text-to-speech/stream",
        headers={"X-API-Key": RAYA_API_KEY, "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        json=body, timeout=aiohttp.ClientTimeout(total=30),
    ) as r:
        if r.status != 200:
            body_text = await r.text()
            raise RuntimeError(f"Raya TTS failed: HTTP {r.status} {body_text[:200]}")
        async for line in r.content:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            if ev.get("type") == "chunk" and ev.get("data"):
                yield _f32le_to_s16le(base64.b64decode(ev["data"]))
            if ev.get("done"):
                break


def _f32le_to_s16le(raw: bytes) -> bytes:
    n = len(raw) // 4
    if n == 0:
        return b""
    floats = struct.unpack(f"<{n}f", raw[: n * 4])
    return struct.pack(f"<{n}h", *(max(-32768, min(32767, int(x * 32767))) for x in floats))


# ---------------------------------------------------------------- Gemini LLM

def llm_reply(messages: list[dict], attempts: int = 3) -> str:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user",
         "parts": [{"text": m["content"]}]}
        for m in messages if m["role"] != "system"
    ]
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": LLM_MAX_TOKENS, "temperature": 0.2},
    }
    last = ""
    for i in range(attempts):
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{LLM_MODEL}:generateContent?key={GOOGLE_API_KEY}",
            json=body, timeout=30)
        if r.status_code != 200:
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            time.sleep(1.0 * (i + 1))
            continue
        cand = (r.json().get("candidates") or [{}])[0]
        text = "".join(p.get("text", "")
                       for p in cand.get("content", {}).get("parts", [])).strip()
        if text:
            return text
        last = f"empty content (finishReason={cand.get('finishReason')})"
    raise RuntimeError(f"{LLM_MODEL} failed -- {last}")


# ---------------------------------------------------------------- call session

class CallSession:
    """One phone call: owns the Vobiz WebSocket, the Sarvam STT connection,
    conversation state, and turn-taking (including barge-in)."""

    def __init__(self, ws: WebSocket):
        self.vobiz_ws = ws
        self.stream_id: str | None = None
        # Which client this call belongs to isn't known until start() resolves
        # it from the dialed number, so messages starts empty rather than
        # pre-loaded with one client's system prompt.
        self.messages: list[dict] = []
        self.client: dict | None = None
        self.http = aiohttp.ClientSession()
        self.stt = SarvamSTT(self._on_transcript, self._on_speech_start)
        # `speaking` means "still generating/sending" -- it goes False the
        # instant the last frame is handed to Vobiz. `playing` means "the
        # caller is still HEARING audio", which lasts far longer because Vobiz
        # drains its queue at real time. Barge-in must gate on `playing`;
        # gating on `speaking` left an ~11s window per utterance where an
        # interrupt was detected and then silently discarded.
        self.speaking = False
        self.playing = False
        self._awaiting_checkpoint: str | None = None
        self._speak_task: asyncio.Task | None = None
        self._turn_lock = asyncio.Lock()
        self._pending: list[str] = []
        self._debounce_task: asyncio.Task | None = None
        self._checkpoint_n = 0
        self._call_uuid = None
        self._from_number: str | None = None
        self._to_number: str | None = None
        self._direction = "inbound"
        self._campaign: dict | None = None
        self._started_at: float | None = None
        self._done = asyncio.Event()

    @property
    def _client_number(self) -> str | None:
        """Whichever number identifies the client this call belongs to --
        the callee for an inbound call (self._to_number), but the CALLER
        for an outbound one, since self._to_number there is just whoever we
        dialed, not any client's registered number. Used for both which
        script to load and which customer to report the call under.

        Normalized to E.164 (+ prefix) because Vobiz is inconsistent about
        this in its own webhooks -- an inbound call's `to` arrived as
        "+918065353946", but an outbound call's `from` on the exact same
        number arrived as "918065353946" with no plus. Both
        clients_registry.py and the dashboard's customer lookup key off the
        + form, so a missing plus silently fails to match either one.
        """
        raw = self._from_number if self._direction == "outbound" else self._to_number
        if raw and not raw.startswith("+"):
            raw = "+" + raw
        return raw

    async def start(self):
        self._started_at = time.time()
        # answer() saw this call before the WebSocket did -- claim its
        # From/To now that _call_uuid (set from the "start" event) lets us
        # find the matching entry.
        info = pending_call_info.pop(self._call_uuid, None) if self._call_uuid else None
        if info:
            self._from_number = info.get("from")
            self._to_number = info.get("to")
            self._direction = info.get("direction", "inbound")
            self._campaign = info.get("campaign")

        if self._campaign:
            # Campaign calls skip client-registry resolution entirely -- the
            # script comes from the campaign's own fields, not any client's
            # fixed prompt, and the customer is already known directly
            # (campaigns.customer_id), not looked up from a number at all.
            instructions = CAMPAIGN_TEMPLATE.format(**self._campaign)
            greeting = campaign_greeting(self._campaign)
            log.info("serving campaign %s contact %s (%s)",
                      self._campaign.get("campaign_id"), self._campaign.get("contact_id"),
                      self._campaign.get("business_name"))
        else:
            self.client, greeting, instructions = load_client_script(self._client_number)
            log.info("serving client '%s' (%s call, identified by %s)",
                      self.client["client_id"], self._direction, self._client_number)
        self.messages = [{"role": "system", "content": _instructions_no_livekit(instructions)}]

        await self.stt.connect()
        # Recorded in history immediately, not after _speak() finishes -- a
        # real call showed the bug this avoids: the caller's first reply
        # arrived before this line existed, so Gemini's context had no
        # assistant turn at all and it re-opened with the greeting instead of
        # answering, because as far as its history showed it hadn't spoken yet.
        self.messages.append({"role": "assistant", "content": greeting})
        self._speak_task = asyncio.create_task(self._speak(greeting))

    async def on_media(self, pcm16_b64: str):
        await self.stt.send_audio(base64.b64decode(pcm16_b64))

    async def _on_speech_start(self):
        """Sarvam VAD says the caller started talking. If the caller is still
        hearing audio, this is a barge-in: flush it."""
        if not self.playing:
            return
        log.info("barge-in: clearing playback")
        # clearAudio is the ONLY thing that stops audio already queued inside
        # Vobiz. Cancelling the task just stops us sending more, and by now
        # there is usually nothing left to send -- the whole utterance was
        # handed over seconds ago.
        await self._send({"event": "clearAudio", "streamId": self.stream_id})
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
        # Vobiz does NOT send playedStream for a checkpoint whose audio was
        # interrupted, so clear the state here or it would never clear.
        self.playing = False
        self._awaiting_checkpoint = None

    def _on_played(self, name: str | None):
        """Vobiz confirms the caller has now heard everything up to a
        checkpoint -- the real end of an utterance."""
        if name and name == self._awaiting_checkpoint:
            self.playing = False
            self._awaiting_checkpoint = None
            log.info("playback finished: %s", name)

    async def _on_transcript(self, text: str):
        """Buffer the transcript and (re)start the debounce timer. Sarvam
        splits one spoken sentence into several finals, so answering each one
        immediately makes the bot reply to fragments and talk over itself."""
        log.info("caller (partial): %s", text)
        self._pending.append(text)
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._flush_turn_after_debounce())

    async def _flush_turn_after_debounce(self):
        try:
            await asyncio.sleep(TURN_DEBOUNCE_S)
        except asyncio.CancelledError:
            return  # more speech arrived; a newer timer owns the turn
        text = " ".join(self._pending).strip()
        self._pending.clear()
        if not text:
            return
        await self._handle_turn(text)

    async def _handle_turn(self, text: str):
        log.info("caller: %s", text)
        async with self._turn_lock:
            self.messages.append({"role": "user", "content": text})
            try:
                reply = await asyncio.to_thread(llm_reply, self.messages)
            except Exception:
                log.exception("llm failed")
                return
            self.messages.append({"role": "assistant", "content": reply})
            end_call = END_MARKER in reply
            spoken = reply.replace(END_MARKER, "").strip()
            log.info("bot: %s%s", spoken, "  [END_CALL]" if end_call else "")
            self._speak_task = asyncio.create_task(self._speak(spoken, end_call=end_call))

    async def _speak(self, text: str, end_call: bool = False):
        self.speaking = True
        self._checkpoint_n += 1
        name = f"turn-{self._checkpoint_n}"
        try:
            async for chunk in synthesize(self.http, text):
                # ~20ms frames at 24kHz/16-bit mono = 960 bytes.
                frame = 960
                for i in range(0, len(chunk), frame):
                    # From the first frame on, the caller is hearing us --
                    # even though we finish SENDING seconds before they finish
                    # HEARING. This stays true until playedStream.
                    self.playing = True
                    await self._send({
                        "event": "playAudio",
                        "streamId": self.stream_id,
                        "media": {"contentType": "audio/x-l16",
                                 "sampleRate": TTS_SAMPLE_RATE,
                                 "payload": base64.b64encode(chunk[i:i + frame]).decode()},
                    })
            self._awaiting_checkpoint = name
            await self._send({"event": "checkpoint", "streamId": self.stream_id,
                              "name": name})
        except asyncio.CancelledError:
            log.info("speak cancelled (barge-in)")
            raise
        finally:
            # Only the SENDING is done here. `playing` deliberately stays set:
            # it is cleared by playedStream, or by a barge-in's clearAudio.
            self.speaking = False

        if end_call:
            # Wait for the goodbye to actually reach the caller before tearing
            # the stream down, or the last words get cut off. Bounded, since
            # playedStream never arrives if playback was interrupted.
            for _ in range(300):
                if not self.playing:
                    break
                await asyncio.sleep(0.1)
            await self._send({"event": "stop", "streamId": self.stream_id})
            self._done.set()

    async def _send(self, obj: dict):
        try:
            await self.vobiz_ws.send_text(json.dumps(obj))
        except Exception:
            pass

    async def close(self):
        if self._speak_task:
            self._speak_task.cancel()
        # Otherwise a pending debounce fires after the caller has hung up and
        # tries to answer a turn on a closed socket.
        if self._debounce_task:
            self._debounce_task.cancel()
        await self.stt.close()
        await self.http.close()
        await self._report()

    async def _report(self):
        """Send the finished call to the VoiceZen Dashboard. Runs after
        everything else has torn down, matching voicezen.py's own stated
        design: nothing here can affect a call that has already ended."""
        if self._started_at is None or not self._client_number:
            return  # never reached start() -- nothing real to report
        duration = time.time() - self._started_at
        turns = [m for m in self.messages if m["role"] != "system"]
        transcript = "\n".join(f"[{m['role']}] {m['content']}" for m in turns)

        if self._campaign:
            await self._report_campaign_call(duration, transcript)
            return

        summary = None
        caller_name = None
        if transcript.strip():
            try:
                raw = await asyncio.to_thread(
                    llm_reply,
                    [
                        {"role": "system", "content": (
                            "Read this phone call transcript. Reply with exactly "
                            "two lines, nothing else:\n"
                            "Line 1: the caller's name if they stated one, "
                            "otherwise the literal word none\n"
                            "Line 2: a summary for a business dashboard, in "
                            "English, at most four sentences -- what the caller "
                            "wanted, any details they gave (address, phone, "
                            "plan), and the outcome. If they placed an order or "
                            "made a complaint, say so explicitly."
                        )},
                        {"role": "user", "content": transcript[:12000]},
                    ],
                )
                first_line, _, rest = raw.partition("\n")
                first_line = first_line.strip()
                if first_line and first_line.lower() != "none":
                    caller_name = first_line
                summary = rest.strip() or raw
            except Exception:
                log.exception("call summary failed -- reporting without one")
        try:
            # dialed_number identifies which CLIENT this belongs to (see
            # _client_number). caller_number is purely "who was the other
            # party" for display -- the callee for an outbound call, the
            # caller for an inbound one -- so it flips the opposite way.
            other_party = self._to_number if self._direction == "outbound" else self._from_number
            await voicezen.report_call_async(
                dialed_number=self._client_number,
                call_duration_seconds=duration,
                caller_number=other_party,
                caller_name=caller_name,
                call_type=self._direction,
                transcript=transcript or None,
                call_summary=summary,
                call_status="completed",
                language="te-IN",
            )
        except Exception:
            log.exception("call report failed")

    async def _report_campaign_call(self, duration: float, transcript: str) -> None:
        """Campaign calls report to campaign-outcome, not voicezen -- the
        customer is already known directly (campaigns.customer_id), so this
        skips the dialled-number lookup entirely rather than trying to force
        a campaign call through a path built for a client's own number."""
        outcome = None
        summary = None
        if transcript.strip():
            try:
                raw = await asyncio.to_thread(
                    llm_reply,
                    [
                        {"role": "system", "content": (
                            "Read this outbound sales call transcript. Reply "
                            "with exactly two lines, nothing else:\n"
                            "Line 1: a short outcome tag -- one of: interested, "
                            "not_interested, callback_requested, no_answer, "
                            "wrong_number, do_not_call, unclear\n"
                            "Line 2: a one-to-two sentence note for a sales "
                            "dashboard on what happened and any next step."
                        )},
                        {"role": "user", "content": transcript[:12000]},
                    ],
                )
                first_line, _, rest = raw.partition("\n")
                outcome = first_line.strip() or None
                summary = rest.strip() or None
            except Exception:
                log.exception("campaign call summary failed -- reporting without one")

        if not CAMPAIGN_OUTCOME_URL or not VOICE_AGENT_SECRET:
            log.error("campaign call finished but CAMPAIGN_OUTCOME_URL/secret "
                      "not configured -- outcome for contact %s is lost",
                      self._campaign.get("contact_id"))
            return
        # self.http is already closed by close() before _report() runs (see
        # close()'s ordering) -- a fresh short-lived session is needed here,
        # not the session that belonged to the call itself.
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    CAMPAIGN_OUTCOME_URL,
                    headers={
                        "Content-Type": "application/json",
                        "x-agent-secret": VOICE_AGENT_SECRET,
                        "apikey": VOICEZEN_ANON_KEY,
                        "Authorization": f"Bearer {VOICEZEN_ANON_KEY}",
                    },
                    json={
                        "campaign_id": self._campaign.get("campaign_id"),
                        "contact_id": self._campaign.get("contact_id"),
                        "customer_id": self._campaign.get("customer_id"),
                        "call_duration": duration,
                        "call_status": "completed",
                        "outcome": outcome,
                        "transcript": transcript or None,
                        "call_summary": summary,
                        "language": self._campaign.get("language"),
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        log.error("campaign-outcome report failed: HTTP %s %s",
                                  r.status, await r.text())
                    else:
                        log.info("campaign call reported: campaign=%s contact=%s outcome=%s",
                                  self._campaign.get("campaign_id"),
                                  self._campaign.get("contact_id"), outcome)
        except Exception:
            log.exception("campaign-outcome request failed")


# ---------------------------------------------------------------- WS endpoint

@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    session = CallSession(ws)
    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            event = data.get("event")

            if event == "start":
                session.stream_id = data["start"]["streamId"]
                session._call_uuid = data["start"].get("callId")
                log.info("stream started: call=%s stream=%s",
                         session._call_uuid, session.stream_id)
                await session.start()

            elif event == "media":
                await session.on_media(data["media"]["payload"])

            elif event == "playedStream":
                # The authoritative "caller has finished hearing it" signal.
                # Payload is event + name only -- no streamId.
                session._on_played(data.get("name"))

            elif event == "clearedAudio":
                log.info("barge-in: playback queue flushed")

    except WebSocketDisconnect:
        log.info("vobiz websocket closed (call ended)")
    finally:
        await session.close()


@app.get("/health")
async def health():
    return {"status": "ok"}
