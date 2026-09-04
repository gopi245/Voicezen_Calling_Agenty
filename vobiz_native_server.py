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
from fastapi.responses import PlainTextResponse

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
    """Vobiz POSTs CallUUID/From/To here for an inbound call. We must
    respond with XML inside ~1-2s or the caller hears dead air."""
    form = await request.form()
    call_uuid = form.get("CallUUID")
    log.info("answer: call=%s from=%s to=%s", call_uuid, form.get("From"), form.get("To"))
    if call_uuid:
        pending_call_info[call_uuid] = {
            "from": form.get("From"),
            "to": form.get("To"),
        }

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true"
            contentType="audio/x-l16;rate={STT_SAMPLE_RATE}"
            statusCallbackUrl="{escape(str(request.base_url))}stream-status">
{PUBLIC_WSS_URL}
    </Stream>
</Response>"""
    return PlainTextResponse(xml, media_type="application/xml")


@app.post("/hangup")
async def hangup(request: Request):
    form = await request.form()
    log.info("hangup: call=%s duration=%s cause=%s",
             form.get("CallUUID"), form.get("Duration"), form.get("HangupCause"))
    return PlainTextResponse("OK")


@app.post("/dial")
async def dial(request: Request):
    """Place an outbound call. Reuses /answer and /hangup unchanged -- Vobiz
    calls answer_url the same way once an outbound call connects as it does
    for an inbound one, so the whole CallSession pipeline is identical."""
    body = await request.json()
    to = body.get("to")
    if not to:
        return {"error": "missing 'to'"}, 400

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
        "answer_url": f"{base}/answer",
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
            log.info("dial: from=%s to=%s -> HTTP %s %s", DIAL_FROM_NUMBER, to, r.status, data)
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
        self._started_at: float | None = None
        self._done = asyncio.Event()

    async def start(self):
        self._started_at = time.time()
        # answer() saw this call before the WebSocket did -- claim its
        # From/To now that _call_uuid (set from the "start" event) lets us
        # find the matching entry.
        info = pending_call_info.pop(self._call_uuid, None) if self._call_uuid else None
        if info:
            self._from_number = info.get("from")
            self._to_number = info.get("to")

        self.client, greeting, instructions = load_client_script(self._to_number)
        log.info("serving client '%s' (dialed %s)", self.client["client_id"], self._to_number)
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
        if self._started_at is None or not self._to_number:
            return  # never reached start() -- nothing real to report
        duration = time.time() - self._started_at
        turns = [m for m in self.messages if m["role"] != "system"]
        transcript = "\n".join(f"[{m['role']}] {m['content']}" for m in turns)
        summary = None
        if transcript.strip():
            try:
                summary = await asyncio.to_thread(
                    llm_reply,
                    [
                        {"role": "system", "content": (
                            "Summarise this phone call for a business dashboard, "
                            "in English, in at most four sentences. State what "
                            "the caller wanted, any details they gave (name, "
                            "address, phone, plan), and the outcome. If they "
                            "placed an order or made a complaint, say so "
                            "explicitly."
                        )},
                        {"role": "user", "content": transcript[:12000]},
                    ],
                )
            except Exception:
                log.exception("call summary failed -- reporting without one")
        try:
            await voicezen.report_call_async(
                dialed_number=self._to_number,
                call_duration_seconds=duration,
                caller_number=self._from_number,
                transcript=transcript or None,
                call_summary=summary,
                call_status="completed",
                language="te-IN",
            )
        except Exception:
            log.exception("call report failed")


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
