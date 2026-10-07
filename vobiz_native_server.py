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

import array
import asyncio
import functools
import base64
import importlib
import io
import json
import logging
import os
import re
import unicodedata
import struct
import sys
import time
import uuid
import wave
from collections import OrderedDict
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
from clients_registry import dial_from_for_customer  # noqa: E402
from clients_registry import lookup as lookup_client  # noqa: E402
from clients_registry import CLIENTS as CLIENTS_REGISTRY  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vobiz-native")

app = FastAPI()

RAYA_API_KEY = os.environ["RAYA_API_KEY"]
RAYA_VOICE_ID = os.getenv("RAYA_VOICE_ID", "25a7c7d9-57b3-488a-a880-33edf6642902")
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
SARVAM_API_KEY = os.environ["SARVAM_API_KEY"]
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3.5-flash-lite")
# Tried first on every turn, Gemini is the fallback. Groq's hardware
# answers in ~0.1-0.3s where Gemini from this VPS took 1.0-1.5s --
# measured directly, repeatedly, with this project's own Telugu prompts
# -- so this is the single biggest latency lever available. Empty by
# default: unset, GROQ_API_KEY simply isn't used and every call behaves
# exactly as before this existed.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# Qwen 3.6 with thinking off, chosen 2026-09-23 on measured first-word
# time with the VoiceZen prompt: median 0.23s, worst 0.56s over 10 turns,
# against gpt-oss-120b's 0.47s median and 3.24s worst -- gpt-oss thinks
# before it speaks and that thinking time is what swung. ~3x gpt-oss's
# price per call; chosen knowingly for the latency.
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
# Groq's API sits behind Cloudflare, which silently 403s a request with
# no browser-like User-Agent -- found by testing, not documented
# anywhere. Without this header every Groq call fails and falls back to
# Gemini on every single turn, which would look like Groq "isn't
# working" rather than the one-line cause it actually is.
GROQ_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
# Both model families reason silently before their first visible word, and
# that thinking time is what made first-token time swing from 0.3s to 4s.
# Qwen takes "none" (thinking off entirely); gpt-oss can't turn it off and
# takes "low" (measured median 0.36s vs 0.88s at its default "medium").
GROQ_REASONING_EFFORT = os.getenv(
    "GROQ_REASONING_EFFORT", "none" if GROQ_MODEL.startswith("qwen/") else "low")
# If Groq hasn't produced a first word by now, a second identical request
# is sent and whichever speaks first wins. Groq's slow requests are server
# jitter, not the prompt, so a fresh request is usually fast; this is what
# keeps the rare slow turn under two seconds. Costs a second request only
# on the turns that needed it.
GROQ_HEDGE_AFTER_S = float(os.getenv("GROQ_HEDGE_AFTER_S", "0.7"))
# VOICEZEN TEST -- set to a Gemini model (e.g. gemini-3.1-flash-lite) to run
# VoiceZen's replies on it instead of Groq. Unset = Groq, as before.
VOICEZEN_LLM = os.getenv("VOICEZEN_LLM", "").strip()
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "400"))

# ADMIN -- the models the admin dashboard may pick per client, each tried on
# real prompts on 2026-09-18. A client setting naming anything else is
# ignored in favour of LLM_MODEL, so a typo can never reach Gemini.
# "no_thinking": these models reason before answering by default, which cost
# ~300 extra billed tokens and 1-3s of silence per reply on a phone call;
# thinkingBudget 0 brings them back to ~1.5s. The Lite models don't think, and
# their request stays exactly what it was before this existed.
# (gemini-2.5-* are listed by the API but return 404; gemini-3.8-flash stayed
# at ~4s even with thinking off.)
SELECTABLE_LLM_MODELS = {
    "gemini-3.1-flash-lite": {"no_thinking": False},
    "gemini-3.5-flash-lite": {"no_thinking": False},
    "gemini-3.5-flash": {"no_thinking": True},
    "gemini-3.6-flash": {"no_thinking": True},
    "gemini-3.7-flash": {"no_thinking": True},
    # ADMIN -- Groq models any client can be given from the dashboard's AI
    # settings (before 2026-09-25 only VoiceZen's line ran on Groq). A turn
    # that Groq can't answer falls back to Gemini (LLM_MODEL), as VoiceZen's
    # does, so a Groq outage costs one slow reply, never a silent call.
    "openai/gpt-oss-20b": {"provider": "groq"},
    "openai/gpt-oss-120b": {"provider": "groq"},
    "qwen/qwen3.6-27b": {"provider": "groq"},
}

# Public wss:// URL for this service (sslip.io -- no DNS setup, Caddy issues
# a real cert for it automatically). Set once the Caddy route is live.
PUBLIC_WSS_URL = os.getenv("PUBLIC_WSS_URL", "wss://vobiz-native.185-199-52-73.sslip.io/media")

VOBIZ_AUTH_ID = os.environ["VOBIZ_AUTH_ID"]
VOBIZ_AUTH_TOKEN = os.environ["VOBIZ_AUTH_TOKEN"]
VOBIZ_API = "https://api.vobiz.ai/api/v1"

# Carrier-side recording of campaign calls. The file is still being written
# when the call ends, so it's polled for rather than assumed present; these
# give it ~20s to appear before falling back to the caller-only capture.
VOBIZ_RECORD_TIME_LIMIT = 900  # seconds; a hard stop, not an expected length
VOBIZ_RECORD_POLL_ATTEMPTS = 10
VOBIZ_RECORD_POLL_DELAY = 2  # seconds between polls

# Last-resort caller ID, used only when a call carries no customer_id we can
# match to a client (see clients_registry.dial_from_for_customer). Every real
# client dials out as their OWN number -- routing an outbound call through
# another client's line shows the wrong caller ID and sends anyone who rings
# back to the wrong script.
DIAL_FROM_NUMBER = os.getenv("DIAL_FROM_NUMBER", "+918065353946")

DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "91")

# "Call Me Now" on voicezentech.com only -- a free instant demo call, capped
# so one visitor can't turn it into an unbounded free conversation. Regular
# Gundusoodi calls and scheduled/batch campaigns have no such limit.
INSTANT_LEAD_TIME_LIMIT_SECONDS = int(os.getenv("INSTANT_LEAD_TIME_LIMIT_SECONDS", "120"))
INSTANT_LEAD_TIME_LIMIT_CLOSING = {
    "te": "మన సమయం అయిపోయింది అండి. మా team మీకు మళ్ళీ callback చేస్తుంది, ధన్యవాదాలు.",
    "hi": "हमारा समय पूरा हो गया है। हमारी टीम आपको दोबारा कॉल करेगी, धन्यवाद।",
    "ta": "நேரம் முடிந்துவிட்டது. எங்கள் குழு உங்களுக்கு மீண்டும் அழைப்பார்கள், நன்றி.",
    "en": "That's our time for today -- our team will follow up with you shortly. Thank you!",
    "kn": "ನಮ್ಮ ಸಮಯ ಮುಗಿದಿದೆ. ನಮ್ಮ ತಂಡ ನಿಮಗೆ ಮತ್ತೆ ಕರೆ ಮಾಡುತ್ತದೆ, ಧನ್ಯವಾದಗಳು.",
    "ml": "സമയം കഴിഞ്ഞു. ഞങ്ങളുടെ ടീം നിങ്ങളെ വീണ്ടും വിളിക്കും, നന്ദി.",
    "mr": "आमचा वेळ संपला आहे. आमची टीम तुम्हाला पुन्हा कॉल करेल, धन्यवाद.",
    "bn": "আমাদের সময় শেষ। আমাদের টিম আপনাকে আবার কল করবে, ধন্যবাদ।",
}


def _to_e164(raw: str | None) -> str | None:
    """Vobiz's own number formatting varies by trunk, so normalize here.

    Seen in the wild on this one account: "+918065353946", "918065353946"
    and "08071580257" -- E.164, country code without the plus, and the
    Indian national format. Everything downstream (clients_registry.py and
    the dashboard's customer_integrations lookup) keys off the E.164 form,
    and a miss there is silent: the call just serves the wrong client's
    script and never gets logged.

    Deliberately conservative -- anything that doesn't look like one of the
    known shapes is returned with a plus and left alone, since guessing
    harder risks rewriting a number into some other client's.
    """
    if not raw:
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return raw
    if raw.startswith("+"):
        return "+" + digits
    # National format: a single leading trunk "0", e.g. 08071580257.
    if digits.startswith("0"):
        return "+" + DEFAULT_COUNTRY_CODE + digits.lstrip("0")
    # Bare subscriber number with no country code, e.g. 8071580257.
    if len(digits) == 10:
        return "+" + DEFAULT_COUNTRY_CODE + digits
    return "+" + digits


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
# In-browser test calls: VALIDATE_TEST_SESSION_URL checks the one-time token
# a browser presents when it opens /test-media; REPORT_TEST_CALL_URL logs
# the finished test call to call_logs (same billing path as a real call --
# no discount, per a deliberate pricing decision).
VALIDATE_TEST_SESSION_URL = os.getenv(
    "VALIDATE_TEST_SESSION_URL",
    os.getenv("VOICEZEN_URL", "").rstrip("/") + "/functions/v1/validate-test-session"
    if os.getenv("VOICEZEN_URL") else "",
)
REPORT_TEST_CALL_URL = os.getenv(
    "REPORT_TEST_CALL_URL",
    os.getenv("VOICEZEN_URL", "").rstrip("/") + "/functions/v1/report-test-call"
    if os.getenv("VOICEZEN_URL") else "",
)
# Checked at the start of every call (inbound, campaign, instant-lead, test
# call alike) -- a customer past their plan's minutes (plus a 10-minute
# grace buffer) gets a short message instead of the normal conversation.
# Fails open on any error (network issue, misconfiguration) -- a real
# customer's call should never get blocked by an unrelated problem on our
# side, only by genuinely having no minutes left.
CHECK_MINUTES_URL = os.getenv(
    "CHECK_MINUTES_URL",
    os.getenv("VOICEZEN_URL", "").rstrip("/") + "/functions/v1/check-minutes"
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
# Lowered from 0.9s to 0.45s: fragments of one sentence arrive within
# ~150-300ms of each other on real calls, so 0.45s still merges them, and
# it is the single largest fixed cost in the whole turn -- cutting it is
# worth more than any other change here. Override with TURN_DEBOUNCE_S if a
# call starts replying to fragments again (that shows up as the bot
# answering a half-sentence, or two replies for one thing the caller said).
TURN_DEBOUNCE_S = float(os.getenv("TURN_DEBOUNCE_S", "0.65"))

# A barge-in signal this early into our own playback is line noise, not
# the caller -- see the note on _playback_started_at. 350ms is well under
# a normal human reaction/interrupt time, so a genuine early interruption
# is still honoured; only the call-setup click is filtered out.
MIN_BARGE_IN_MS = float(os.getenv("MIN_BARGE_IN_MS", "350"))
# VOICEZEN TEST -- how long a sound over the bot must last, with no words
# recognised yet, before the bot stops for it. "హ్మ్"/"ఓకే" have been
# transcribed well within this; a caller actually interrupting is still
# talking. See _on_speech_start.
BARGE_IN_CONFIRM_S = float(os.getenv("BARGE_IN_CONFIRM_S", "1.0"))
# VOICEZEN TEST -- how long to wait when the caller trails off on "అంటే..."
# before answering what they did say. See _ends_mid_thought.
TRAILING_CONNECTOR_WAIT_S = float(os.getenv("TRAILING_CONNECTOR_WAIT_S", "1.0"))

# Domain vocabulary. The brand name is consistently mis-heard on real calls
# ("గుండు సోతి", "గుండుకో జ్ఞానం"), which is exactly the problem the production
# agent solved the same way -- saaras models accept a config prompt to bias
# recognition toward these words.
STT_PROMPT = (
    "గుండుసూది, పిన్ మీడియా, మ్యాగజైన్, సబ్‌స్క్రిప్షన్, ప్రింట్, డిజిటల్, "
    "ఎడిషన్, డెలివరీ, పేమెంట్, వాట్సాప్, పిన్‌కోడ్, చిరునామా, ఇంటి నెంబర్, "
    "వీధి, ఊరు, జిల్లా, రూపాయలు, ఆరు నెలలు, ఏడాది, నెలవారీ, వార్షిక"
)

# Gundusoodi only -- the shared list above plus the address words call 1102
# lost: "డాష్" came through as "డాసెస్"/"డాస్", "MTC పాలెం" as "ఎంటిసి
# పార్లమెంట్", "విశాఖపట్నం" as "ఇది శాఖ పార్ట్". Kept separate so the other
# clients on STT_PROMPT are unchanged.
STT_PROMPT_GUNDUSOODI = STT_PROMPT + (
    ", డాష్, స్లాష్, పాలెం, నగర్, కాలనీ, వీధి పేరు, అపార్ట్‌మెంట్, "
    "విశాఖపట్నం, శ్రీకాకుళం, విజయనగరం, విజయవాడ, గుంటూరు, నెల్లూరు, కాకినాడ, "
    "రాజమండ్రి, తిరుపతి, హైదరాబాద్, సున్నా, డబల్"
)

# VOICEZEN TEST -- VoiceZen's own vocabulary. Until now VoiceZen calls were
# biased toward Gundusoodi's magazine words above, and "ప్లాన్స్" (plans)
# came through as "ప్లాంట్స్" (plants) on three separate real calls.
STT_PROMPT_VOICEZEN = (
    "వాయిస్ జెన్, VoiceZen, ప్లాన్స్, ప్లాన్, ప్రైసింగ్, ప్రైస్, రేట్లు, ఛార్జెస్, "
    "డెమో, ఏఐ ఏజెంట్, కాలింగ్ ఏజెంట్, బిజినెస్, కాల్స్, లీడ్స్, వాట్సాప్, "
    "ఆర్డర్స్, అపాయింట్‌మెంట్, కాల్‌బ్యాక్, కోచింగ్ సెంటర్, క్లినిక్, "
    "రెస్టారెంట్, రియల్ ఎస్టేట్, షాప్, నంబర్, "
    # The bot asks coaching centres about exactly these; call 970 heard the
    # answer as "పచ్చ పాలం... కారు".
    "అడ్మిషన్స్, అడ్మిషన్, ఫీజు, ఫీజులు, కోర్సులు, బ్యాచ్, స్టూడెంట్స్, పేరెంట్స్, ఎంక్వైరీ"
)

# The system prompt has no notion of ending a call (that was a LiveKit
# function_tool before). Ask the model to mark it in-band instead.
END_MARKER = "[[END_CALL]]"


def _instructions_no_livekit(base_instructions: str) -> str:
    return base_instructions + (
        f"\n\nWhen you have said goodbye and the call should end, append the "
        f"exact literal text {END_MARKER} at the very end of that message, "
        f"after your spoken words. Do not say {END_MARKER} out loud -- it is "
        f"a control marker, stripped before your words are spoken.\n\n"
        f"Ending the call is IRREVERSIBLE once {END_MARKER} is sent -- the line "
        f"physically hangs up, so only attach it when the caller has clearly and "
        f"unambiguously indicated the ENTIRE conversation is over (a plain "
        f"goodbye, \"that's all\", \"thanks, bye\"). A real call once ended "
        f"right as the caller was mid-sentence trying to change something, "
        f"because their reply was read as a simple confirmation when it wasn't "
        f"one. Specifically:\n"
        f"  - Confirming ONE detail (a time, an address, a plan) is NOT the "
        f"same as ending the call -- after confirming it, wait for the caller's "
        f"next turn instead of ending, unless they explicitly say goodbye in "
        f"that same reply.\n"
        f"  - A short or unclear reply (\"okay\", \"mm\", a mumbled word) is "
        f"NEVER grounds for ending the call -- ask a clarifying question "
        f"instead of guessing they are done.\n"
        f"  - If there is any real chance the caller might still say something "
        f"else, do not attach {END_MARKER} yet."
    )


# VOICEZEN TEST -- words a caller says to show they're listening, not to
# ask for anything. A real call showed "Okay", "Hello", "Hmm" and "ఓకేనండి"
# each answered as a separate turn while the bot was still talking, so the
# caller heard the same question four times.
_BACKCHANNEL_WORDS = {
    "ok", "okay", "okk", "hmm", "hm", "hmmm", "mm", "mhm", "haa", "ha", "yes",
    "yeah", "hello", "సరే", "సరేనండి", "ఓకే", "ఓకేనండి", "అలాగే", "అలాగేనండి",
    "అవును", "అవునండి", "హా", "హ్మ్", "హ్మ్మ్", "ఊ", "హలో", "అండి",
}


_FILLER_WORDS = {"hmm", "hm", "hmmm", "mm", "mhm", "హ్మ్", "హ్మ్మ్", "ఊ"}


def _words(text: str) -> list[str]:
    return re.sub(r"[.,!?" + chr(0x0964) + r"]", " ", text.lower()).split()


def _is_backchannel(text: str) -> bool:
    words = _words(text)
    return 0 < len(words) <= 4 and all(w in _BACKCHANNEL_WORDS for w in words)


def _is_filler(text: str) -> bool:
    words = _words(text)
    return 0 < len(words) <= 2 and all(w in _FILLER_WORDS for w in words)


# VOICEZEN TEST -- words a Telugu speaker leads into a thought with and then
# pauses on ("అంటే..." = "I mean..."). A turn ending on one isn't finished.
_CONNECTOR_WORDS = {
    # Not "ఏంటంటే": callers end questions on it ("వాయిస్ జెన్ అంటే ఏంటంటే",
    # call 973), so waiting on it added a second to a finished question.
    "అంటే", "అది", "అయితే", "ఆ", "మరి", "ఇంకా", "కానీ", "ఎందుకంటే",
    "and", "so", "but", "like", "uh", "um", "means",
}


_TELUGU_DIGIT_WORDS = ["సున్నా", "ఒకటి", "రెండు", "మూడు", "నాలుగు",
                       "ఐదు", "ఆరు", "ఏడు", "ఎనిమిది", "తొమ్మిది"]
_LONG_NUMBER = re.compile(r"[0-9౦-౯][0-9౦-౯\s-]{5,}[0-9౦-౯]")


def _speak_phone_digits(text: str) -> str:
    """Phone numbers and pincodes (seven digits or more), read digit by digit.

    Call 1001: gpt-oss-20b read a callback number back as Telugu numerals
    (౯౪౯౩...), which the TTS can't say clearly. Only long runs are touched,
    so an amount like "200" is left for the model to say as a number.
    """
    def words(m):
        ds = [c for c in m.group(0) if not c.isspace() and c != "-"]
        if len(ds) < 7:
            return m.group(0)
        return " ".join(_TELUGU_DIGIT_WORDS[int(unicodedata.digit(c))] for c in ds)
    return _LONG_NUMBER.sub(words, text)


def _ends_mid_thought(text: str) -> bool:
    words = _words(text)
    if not words or text.rstrip().endswith("?"):
        return False
    if words[-1] in _CONNECTOR_WORDS:
        return True
    # Call 972: "మా బిజినెస్" -- pause -- "కస్టమర్ కేర్ అండి." Sarvam ends a
    # finished sentence with punctuation; one or two bare words without it
    # are usually a phrase still in progress. Acknowledgements are exempt.
    return (len(words) <= 2 and not text.rstrip().endswith((".", "!", chr(0x0964)))
            and not _is_backchannel(text))


def _is_minor(text: str) -> bool:
    """Too little to interrupt the bot mid-sentence: an acknowledgement, or
    a single word -- which is usually noise the recogniser made into one."""
    return _is_backchannel(text) or len(_words(text)) <= 1


# VOICEZEN TEST -- what a caller says when they are done: goodbye, thanks,
# or a clear "not interested". Call 994: gpt-oss-120b ended the call right
# after answering "what is VoiceZen?", with the caller mid-enquiry. On
# VoiceZen the model may only hang up after the caller has said one of these.
_GOODBYE_PATTERN = re.compile(
    "|".join([
        r"\bbye\b", r"\bbyee\b", r"good ?bye", r"\bthank", r"\bthanks\b", r"\btq\b", r"that'?s all",
        r"not interested", r"no need", r"call later", r"\bok bye",
        "బై", "బాయ్", "థాంక్", "ధన్యవాద", "ఉంటాను", "పెట్టేస్తా", "పెడతాను", "ఇక చాలు", "అంతే అండి",
        "వద్దు", "అవసరం లేదు", "ఇంట్రెస్ట్ లేదు", "తర్వాత చేస్తా", "తర్వాత మాట్లాడ",
    ]),
    re.IGNORECASE,
)


def _caller_said_goodbye(text: str | None) -> bool:
    return bool(text) and bool(_GOODBYE_PATTERN.search(text))


# VOICEZEN TEST -- the caller's language, read from the words of their latest
# turn, so the model is told outright which language to answer in. The script
# alone wasn't enough: it is full of Telugu example lines, and a simulated
# English caller got Telugu replies 8 times in 36 turns. Telugu typed in Latin
# letters ("price entha") is left to the script rather than forced to English.
_ROMAN_TELUGU = re.compile(
    r"\b(entha|enta|enti|ento|emiti|kavali|kaavali|cheppandi|chepandi|andi|ledu|undi|unnaru|ela|ekkada|"
    r"naa|maa|meeru|mee|nenu|sare|avunu|kaadu|vaddu|chestara|cheyandi|ivvandi)\b", re.I)
_ROMAN_HINDI = re.compile(
    r"\b(kya|hai|nahi|nahin|kaise|kitna|kitne|mujhe|mera|meri|aap|haan|theek|chahiye|bataiye|karo)\b", re.I)


def _caller_language(text: str | None) -> str | None:
    if not text:
        return None
    if re.search(r"[ఀ-౿]", text):
        return "Telugu"
    if re.search(r"[ऀ-ॿ]", text):
        return "Hindi"
    if re.search(r"[஀-௿]", text):
        return "Tamil"
    letters = re.findall(r"[A-Za-z]+", text)
    if len(letters) >= 2 and not _ROMAN_TELUGU.search(text) and not _ROMAN_HINDI.search(text):
        return "English"
    return None


def _reply_wants_end_call(marked: bool, spoken: str, legacy: bool, last_caller: str | None = None) -> bool:
    """True if a reply carrying END_MARKER should actually hang up.

    VOICEZEN TEST -- a real Groq reply attached END_MARKER to a message
    that was itself still asking a qualifying question ("...మీ పేరు
    చెప్పగలరా?"), hanging up right after asking for the caller's name
    instead of waiting for their answer. A reply that ends in a question
    mark cannot also be a goodbye, so on VoiceZen that contradiction is
    treated as the marker being wrong, not the call being over -- the
    question is still spoken either way, only the hangup is skipped.
    """
    if not marked:
        return False
    if not legacy and spoken.rstrip().endswith("?"):
        log.warning("bot reply ended in a question but also carried END_CALL -- ignoring the marker")
        return False
    if not legacy and not _caller_said_goodbye(last_caller):
        log.warning("END_CALL but the caller never said goodbye (%r) -- ignoring the marker", last_caller)
        return False
    return True


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

If {language} is Telugu: speak the way a regular Telugu speaker actually talks on the
phone, not pure/bookish Telugu. That means naturally mixing in common English words for
brand names, technical terms and everyday business vocabulary -- keep words like the
business's own name, "demo", "website", "call", "team", "OK", "sorry" in English exactly
as a real Telugu speaker would say them, rather than translating everything into Telugu.
Also expect the CALLER to mix Telugu and English the same way -- treat an English word or
short English phrase inside their Telugu reply as completely normal, not something to be
confused by or ask them to repeat.

If they ask "how did you get my number" or "who is this", explain you're calling on behalf
of {business_name} about a current offer, and offer to remove them from future calls if asked.
If they ask to speak to a human, be honest that none is available on this call, but offer to
note their request or have someone call back.

Write every date, time and number the way a person would actually SAY it out loud, never
in written/digit form -- your text is converted directly to speech, and a TTS reads digits
and symbols literally rather than naturally. Follow the convention of whichever language
you are speaking in ({language}), not English written style translated word for word.

If speaking English, for example:
  "September 5th, 2026" -> "September fifth, twenty twenty-six"
  "10:00 AM"             -> "ten AM" or "ten in the morning"  (never "ten zero zero AM")
  "3-5 days"              -> "three to five days"
  "6 months"              -> "six months"

If speaking Telugu, use how a Telugu speaker actually says dates and times, not a
transliteration of the English format:
  "September 5th"        -> "సెప్టెంబర్ ఐదు" or "సెప్టెంబర్ ఐదవ తారీకు"
  "2026"                  -> "రెండు వేల ఇరవై ఆరు"  (never digit-by-digit here -- a year is
                             said as one number, unlike a phone number or pincode)
  "10:00 AM"              -> "ఉదయం పది గంటలకు"  (never "పది సున్నా సున్నా AM")
  "3-5 days"               -> "మూడు నుండి ఐదు రోజుల్లో"
  "6 months"               -> "ఆరు నెలలు"

This applies everywhere in your reply, not just when confirming a date -- prices, counts,
durations, and phone numbers all follow the same rule (a phone number or pincode is still
read digit by digit, since that is how a person actually says one -- it is only a year,
price or count that is spoken as a whole number).
"""


_CAMPAIGN_GREETINGS = {
    "English": "Hi{name}, I'm an AI assistant calling on behalf of {business_name}.",
    "Telugu": "నమస్కారం{name}, నేను {business_name} తరఫున మాట్లాడుతున్న AI అసిస్టెంట్‌ని.",
    "Hindi": "नमस्ते{name}, मैं {business_name} की ओर से बात कर रहा AI असिस्टेंट हूँ।",
    "Tamil": "வணக்கம்{name}, நான் {business_name} சார்பாக பேசும் AI உதவியாளர்.",
}


# Business names arrive in Latin script (customers.company_name), but the
# greeting is a fixed template, not an LLM turn -- there's no transliteration
# step before it's spoken. A real call confirmed this sounds garbled ("voicezen"
# read aloud mid-Telugu-sentence came out like "voizem"), even though the LLM
# itself correctly writes "వాయిస్‌జెన్" later in the same call, once it's
# generating text itself. This covers the one line that skips the LLM.
# Extend this dict as more clients are onboarded for a non-Latin-script call.
_BUSINESS_NAME_TRANSLITERATIONS = {
    "Telugu": {"voicezen": "వాయిస్‌జెన్"},
    "Hindi": {"voicezen": "वॉइसज़ेन"},
    "Tamil": {"voicezen": "வாய்ஸ்ஜென்"},
}


def campaign_greeting(campaign: dict) -> str:
    language = campaign.get("language") or "English"
    template = _CAMPAIGN_GREETINGS.get(language, _CAMPAIGN_GREETINGS["English"])
    name = campaign.get("contact_name")
    business_name = campaign["business_name"]
    transliterated = _BUSINESS_NAME_TRANSLITERATIONS.get(language, {}).get(business_name.strip().lower())
    return template.format(name=f" {name}" if name else "", business_name=transliterated or business_name)


# Raya (TTS) and Sarvam (STT) language codes for each language a campaign or
# instant-lead call can be configured with. A regular client call (Gundusoodi
# etc.) has no `language` field at all and stays on the Telugu default below.
_TTS_LANGUAGE_CODES = {"English": "en", "Telugu": "te", "Hindi": "hi", "Tamil": "ta"}
_STT_LANGUAGE_CODES = {"English": "en-IN", "Telugu": "te-IN", "Hindi": "hi-IN", "Tamil": "ta-IN"}


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
        # VOICEZEN TEST -- the media stream opens ~1.4s after this webhook
        # (measured on every call), and the greeting used to wait a further
        # 0.5-3.4s after that for the dashboard settings and minutes check.
        # Start both now so they're ready when the stream arrives. Same
        # requests, same arguments as start() would make, so a caller on the
        # dashboard's test list still gets the test script.
        if direction == "inbound" and not entry.get("campaign"):
            client = lookup_client(_to_e164(form.get("To")))
            if client and client.get("client_id") == "voicezen":
                entry["prefetch"] = asyncio.create_task(
                    _prefetch_call_setup(client.get("customer_id"), _to_e164(form.get("From"))))

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true"
            contentType="audio/x-l16;rate={STT_SAMPLE_RATE}"
            statusCallbackUrl="{escape(str(request.base_url))}stream-status">
{PUBLIC_WSS_URL}
    </Stream>
</Response>"""
    return PlainTextResponse(xml, media_type="application/xml")


async def _prefetch_call_setup(customer_id: int | None, caller: str | None) -> tuple[bool, dict]:
    """(has minutes, agent config) -- what start() otherwise asks for itself."""
    config_task = asyncio.create_task(fetch_agent_config(customer_id, caller))
    allowed = await check_customer_minutes(customer_id) if customer_id else True
    return allowed, await config_task


async def _warm_voicezen_greeting():
    """VOICEZEN TEST -- make the first call after a restart as quick as the
    rest: generate the greeting audio now, and wake the dashboard functions,
    which took 3.4s to answer the first call after the 2026-09-23 restart."""
    try:
        client, greeting, _ = next(
            (load_client_script(num) for num, c in CLIENTS_REGISTRY.items() if c.get("client_id") == "voicezen"),
            (None, None, None))
        if not client:
            return
        await asyncio.gather(
            fetch_agent_config(client.get("customer_id"), ""),
            check_customer_minutes(client.get("customer_id")) if client.get("customer_id") else asyncio.sleep(0),
        )
        async with aiohttp.ClientSession() as http:
            async for _ in synthesize(http, greeting, client.get("tts_language") or "te"):
                pass
        log.info("voicezen greeting pre-generated")
    except Exception:
        log.exception("could not pre-generate the voicezen greeting")


@app.on_event("startup")
async def _on_startup():
    asyncio.create_task(_warm_voicezen_greeting())


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

    # Whose line does this call go out on? An explicit dial_from in the body
    # wins (lets a caller override without a deploy), then the client that
    # owns this campaign's customer_id, then the service-wide default.
    customer_id = (campaign or {}).get("customer_id")
    from_number = body.get("dial_from") or dial_from_for_customer(customer_id)
    auth_id, auth_token = VOBIZ_AUTH_ID, VOBIZ_AUTH_TOKEN
    if not dial_from_for_customer(customer_id) and customer_id:
        # ADMIN -- a client added from the admin dashboard calls out on its
        # own number, and from its own Vobiz account when it has one.
        found = await lookup_client_in_db(customer_id=customer_id)
        from_number = from_number or (found.get("number") if found else None)
        auth_id, auth_token = vobiz_auth(found)
    from_number = from_number or DIAL_FROM_NUMBER

    payload = {
        "from": from_number,
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
            f"{VOBIZ_API}/Account/{auth_id}/Call/",
            headers={"X-Auth-ID": auth_id, "X-Auth-Token": auth_token,
                     "Content-Type": "application/json"},
            json=payload, timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            data = await r.json()
            log.info("dial: from=%s to=%s campaign=%s -> HTTP %s %s",
                      from_number, to, bool(campaign), r.status, data)
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

    def __init__(self, on_transcript, on_speech_start, on_language=None):
        self._on_transcript = on_transcript
        self._on_speech_start = on_speech_start
        self._on_language = on_language
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._recv_task: asyncio.Task | None = None

    async def connect(self, language_code: str = "te-IN", prompt: str = STT_PROMPT):
        self._session = aiohttp.ClientSession()
        params = {
            # Pinned, not "unknown" (auto-detect). Two real calls showed
            # auto-detect misidentifying the caller's language on individual
            # utterances -- confident, wrong-script transcripts in Hindi,
            # Punjabi, Kannada, and English (9 of ~19 turns in one call),
            # mostly during short number-heavy phrases like a house number or
            # street name. Defaults to Telugu (this client's callers), but a
            # campaign/instant-lead call passes its own configured language.
            "language-code": language_code,
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
        await self._ws.send_str(json.dumps({"type": "config", "prompt": prompt}))
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
                payload = data.get("data") or {}
                transcript = payload.get("transcript", "")
                # Present only when connected with language-code=unknown, i.e.
                # on a multilingual line. Reported per utterance, so a caller
                # who switches language mid-call is followed rather than
                # locked to whatever they opened with.
                detected = payload.get("language_code") or payload.get("language")
                if detected and self._on_language:
                    self._on_language(detected)
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


# VOICEZEN TEST -- Cartesia speech-to-text, for a live comparison on
# VoiceZen's line only (VOICEZEN_STT_PROVIDER=cartesia; default sarvam).
# ink-whisper is Cartesia's only model that takes Telugu. It scored far below
# Sarvam on 30 real caller clips offline (2026-09-23); this exists so that
# can be heard on a real call, and switched back by removing one setting.
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
VOICEZEN_STT_PROVIDER = os.getenv("VOICEZEN_STT_PROVIDER", "sarvam").strip().lower()


class CartesiaSTT:
    """Same interface as SarvamSTT: connect / send_audio / close, with the
    same three callbacks.

    Cartesia sends no separate "speech started" signal, so the first partial
    transcript of an utterance stands in for it -- that is what barge-in and
    the timing logs hang off. Final transcripts go to on_transcript exactly as
    Sarvam's do, so turn-taking downstream is unchanged.
    """

    URL = "wss://api.cartesia.ai/stt/websocket"
    # Cartesia asks for roughly 100 ms per message; Vobiz sends ~20 ms frames.
    CHUNK_BYTES = STT_SAMPLE_RATE * 2 // 10

    def __init__(self, on_transcript, on_speech_start, on_language=None):
        self._on_transcript = on_transcript
        self._on_speech_start = on_speech_start
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._recv_task: asyncio.Task | None = None
        self._buf = bytearray()
        self._in_utterance = False

    async def connect(self, language_code: str = "te-IN", prompt: str | None = None):
        self._session = aiohttp.ClientSession()
        lang = (language_code or "te").split("-")[0].lower()
        if lang in ("", "unknown"):
            lang = "te"
        params = {
            "model": "ink-whisper",
            "language": lang,
            "encoding": "pcm_s16le",
            "sample_rate": str(STT_SAMPLE_RATE),
            "cartesia_version": "2026-08-14",
            # A quarter second of silence ends an utterance, matching the
            # ~0.25 s Sarvam is configured for, so the comparison is fair.
            "max_silence_duration_secs": "0.25",
            "min_volume": "0.1",
        }
        url = self.URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self._ws = await self._session.ws_connect(url, headers={"X-API-Key": CARTESIA_API_KEY}, timeout=30)
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("cartesia stt connected (ink-whisper, %s)", lang)

    async def _recv_loop(self):
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            kind = data.get("type")
            if kind == "transcript":
                text = (data.get("text") or "").strip()
                if not text:
                    continue
                if not data.get("is_final"):
                    if not self._in_utterance:
                        self._in_utterance = True
                        await self._on_speech_start()
                    continue
                if not self._in_utterance:
                    await self._on_speech_start()
                self._in_utterance = False
                await self._on_transcript(text)
            elif kind == "error":
                log.warning("cartesia stt error: %s", data)

    async def send_audio(self, pcm16_bytes: bytes):
        if self._ws is None:
            return
        self._buf += pcm16_bytes
        if len(self._buf) < self.CHUNK_BYTES:
            return
        chunk, self._buf = bytes(self._buf), bytearray()
        try:
            await self._ws.send_bytes(chunk)
        except Exception:
            log.warning("cartesia send_audio failed; STT socket likely closed")

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()


# ---------------------------------------------------------------- Raya TTS

# One voice per language. A Raya voice_id belongs to exactly one language, so
# a multilingual line needs a different speaker per language rather than one
# speaker with a language flag. All "standard" model -- mixing an m1 voice_id
# with model="standard" 404s ("unknown voice_id"), and vice versa.
# Telugu deliberately stays Tanvi: it was picked by ear against the other four
# for this project's greeting, and every Gundusoodi call already uses it.
DEFAULT_TTS_LANGUAGE = "te"

RAYA_VOICES = {
    "te": os.getenv("RAYA_VOICE_ID", "25a7c7d9-57b3-488a-a880-33edf6642902"),  # Tanvi
    "hi": "07308011-d790-4187-8ad9-afe7425627e9",  # Neha
    "ta": "ab17f0d3-3202-4344-8d6c-98c1bea5e518",  # Vignesh
    "en": "0f24fb66-e495-4781-9e84-1224aa7dacde",  # Nayra (en-in)
    "kn": "522e4587-6611-4442-afdb-59f20ad5e420",  # Rohan
    "ml": "42710cb4-fad8-48b9-b89c-960824b485f8",  # Abirami
    "mr": "0c48e416-d0d8-4518-a778-384172c987fe",  # Kavya
    "bn": "145c9da5-96ef-4ad0-ba3b-2fafb3a63d87",  # Ritu
}


# Raya validates `language` against this exact list and is case-sensitive:
# "en-IN" is rejected outright, and bare "en" is rejected in combination with
# the en-in voice. Sarvam and the campaign config both emit the "xx-IN" shape,
# so normalize before sending rather than passing a caller's tag straight
# through -- a rejected language is a turn with no audio at all.
RAYA_LANGUAGES = {"hi", "mr", "te", "kn", "bn", "as", "en-us", "en-in",
                  "gu", "en", "ml", "ne", "ta", "pa"}


# ADMIN -- voices the admin dashboard may pick per client, keyed by id with
# the Raya language each one speaks (all "standard" model, all tried on
# 2026-09-18). A chosen voice is used only for its own language, so it can
# never be paired with a language it doesn't speak.
SELECTABLE_VOICES = {
    "25a7c7d9-57b3-488a-a880-33edf6642902": "te",     # Tanvi (default)
    "18897ed3-e3a8-46e0-ad0e-a176bcb6ceee": "te",     # Karthik
    "e39f7165-85e0-4bd8-b853-cb89c7dbd1b7": "te",     # Divya
    "43bebf06-bf02-4b39-8358-e7469bda0c0e": "te",     # Abirami Te
    "edf1b4a5-479c-4d85-885c-54c21a21beb5": "te",     # Anjura Te
    "0f24fb66-e495-4781-9e84-1224aa7dacde": "en-in",  # Nayra (default)
    "21e4f0f5-e2b3-40bc-9d9c-c6aecb87160a": "en-in",  # Mansi
    "4fc34582-f0e0-418d-8d0b-2738637d9229": "en-in",  # Aanya
    "bae3d3fe-64f9-4d2a-80b2-607ddff23528": "en-in",  # Roger
}
SELECTABLE_SPEEDS = (0.8, 0.9, 1.0, 1.1, 1.2)


def resolve_voice(language: str | None, overrides: dict | None = None) -> tuple[str, str]:
    """(voice_id, language) for Raya -- resolved together, never separately.

    They are one decision, not two: a voice belongs to exactly one language,
    and Raya rejects a mismatched pair with a 422, which on a live call is
    silent dead air. Punjabi is the case that proves it -- "pa" is a language
    Raya accepts, but this account has no Punjabi voice, so pairing the
    fallback (Telugu) voice with "pa" fails outright. When there's no voice
    for a language, fall back to the default voice AND its own language.

    Accepts the shapes Sarvam and the campaign config actually emit ("hi",
    "hi-IN", "en-IN"). Raya's tags are case-sensitive and exact: "en-IN" is
    rejected, and bare "en" is rejected alongside the en-in voice.
    """
    base = (language or "").strip().lower().split("-")[0]
    # Any flavour of English becomes Indian English -- these are Indian
    # business callers, and "en-in" is the tag Raya pairs with that voice.
    tag = "en-in" if base == "en" else base
    chosen = (overrides or {}).get(base)
    if chosen and SELECTABLE_VOICES.get(chosen) == tag:
        return chosen, tag
    voice = RAYA_VOICES.get(base)
    if not voice or tag not in RAYA_LANGUAGES:
        return RAYA_VOICE_ID, DEFAULT_TTS_LANGUAGE
    return voice, tag


# ------------------------------------------------- recording both sides
#
# The recording used to hold the caller's audio only, so what the agent
# actually said -- and how it sounded -- was gone the moment the call ended.
# That made every "it sounded robotic on that call" unanswerable after the
# fact, and it is the one question a voice product most needs to answer.
#
# The two sides arrive at different rates: the phone line gives us 16kHz and
# Raya returns 24kHz, so the agent's audio is resampled down before it can be
# mixed into the caller's timeline.

# ------------------------------------------------------- how words are said
#
# Raya reads Latin script with the pronunciation rules of whatever language
# it was asked to speak, so a brand name written in English comes out wrong
# in the middle of a Telugu sentence: 'VoiceZen' was heard on a real call as
# 'vaayis ven'. The same problem was already found and fixed for campaign
# greetings (see _BUSINESS_NAME_TRANSLITERATIONS), but that covers one
# template only -- an ordinary client's greeting and everything the model
# writes went straight to the voice untouched.
#
# Doing it here, just before synthesis, covers every path at once: the fixed
# greeting, the model's replies, and any script. Nothing upstream has to
# remember to spell a name phonetically, and the call scripts stay untouched.
#
# Only add a word here when the voice genuinely says it wrong. Telugu
# speakers say 'business', 'demo' and 'agent' in English and expect to hear
# them that way -- rewriting those would sound stranger than leaving them.
# Why this table is as long as it is. Raya applies Telugu pronunciation
# rules to Latin letters, so every English word is a place where the voice
# stops flowing and starts spelling out. Measured across recent calls:
# Gundusoodhi's replies contain zero Latin words -- it writes even borrowed
# words in Telugu script, so it reads fluently -- while VoiceZen's are 38%
# Latin, which is why the same voice sounds like it is reading off paper on
# one line and natural on the other.
#
# These spellings do not translate anything. A Telugu speaker still hears
# 'business' and 'demo' -- those words are part of how Telugu is actually
# spoken. Writing them in Telugu script only tells the voice how to say
# what it is already saying.
#
# Taken from the words the agent actually speaks most, counted from real
# call transcripts, rather than guessed at.
_SPOKEN_AS = {
    'te': {
        # names
        'voicezen': 'వాయిస్‌జెన్',
        'gundusoodhi': 'గుండుసూది',
        'whatsapp': 'వాట్సాప్',
        # what the agent talks about, most-spoken first
        'assistant': 'అసిస్టెంట్',
        'business': 'బిజినెస్',
        'agent': 'ఏజెంట్',
        'agents': 'ఏజెంట్స్',
        'ai': 'ఏఐ',
        'call': 'కాల్',
        'calls': 'కాల్స్',
        'calling': 'కాలింగ్',
        'details': 'డీటెయిల్స్',
        'demo': 'డెమో',
        'enquiry': 'ఎంక్వైరీ',
        'enquiries': 'ఎంక్వైరీస్',
        'website': 'వెబ్‌సైట్',
        'lead': 'లీడ్',
        'leads': 'లీడ్స్',
        'free': 'ఫ్రీ',
        'answer': 'ఆన్సర్',
        'orders': 'ఆర్డర్స్',
        'order': 'ఆర్డర్',
        'address': 'అడ్రస్',
        'collect': 'కలెక్ట్',
        'team': 'టీమ్',
        'number': 'నంబర్',
        'budget': 'బడ్జెట్',
        'location': 'లొకేషన్',
        'customer': 'కస్టమర్',
        'customers': 'కస్టమర్స్',
        'service': 'సర్వీస్',
        'support': 'సపోర్ట్',
        'appointment': 'అపాయింట్‌మెంట్',
        'appointments': 'అపాయింట్‌మెంట్స్',
        'callback': 'కాల్‌బ్యాక్',
        'callbacks': 'కాల్‌బ్యాక్స్',
        'pricing': 'ప్రైసింగ్',
        'confirm': 'కన్ఫర్మ్',
        'booking': 'బుకింగ్',
        'price': 'ప్రైస్',
        'tickets': 'టికెట్స్',
        'timings': 'టైమింగ్స్',
        'requirement': 'రిక్వైర్‌మెంట్',
        'requirements': 'రిక్వైర్‌మెంట్స్',
        'online': 'ఆన్‌లైన్',
        'link': 'లింక్',
        'email': 'ఈమెయిల్',
        'telugu': 'తెలుగు',
        'english': 'ఇంగ్లీష్',
        'hello': 'హలో',
    },
    'hi': {'voicezen': 'वॉइसज़ेन', 'whatsapp': 'व्हाट्सएप'},
    'ta': {'voicezen': 'வாய்ஸ்ஜென்', 'whatsapp': 'வாட்ஸ்அப்'},
}

_BOUNDARY = chr(92) + 'b'

_SPOKEN_AS_PATTERNS = {
    # chr(92) is a backslash. Written this way because the escape gets
    # mangled before it reaches the file, and a literal backspace here
    # silently matches nothing at all.
    lang: [(re.compile(_BOUNDARY + re.escape(word) + _BOUNDARY, re.IGNORECASE), said)
           for word, said in table.items()]
    for lang, table in _SPOKEN_AS.items()
}


# Which language's voice can actually read this text.
#
# A voice_id on Raya belongs to one language and reads everything with that
# language's rules, so a voice given the wrong script does not merely sound
# accented -- it produces noise. A real call showed exactly that: the caller
# said 'Customer care andi', Sarvam reported the language as English, the
# agent switched its voice to English, and then read a Telugu-script reply
# with it. The caller heard babble and hung up.
#
# The caller's language is the wrong thing to choose a voice by, because it
# says nothing about what the agent is about to say. The text itself does,
# and it is already written by the time a voice is needed. So the script
# decides, and a detected language only matters when the text does not
# settle it -- pure Latin, where several voices are plausible.

_SCRIPT_RANGES = {
    'te': (0x0C00, 0x0C7F),   # Telugu
    'hi': (0x0900, 0x097F),   # Devanagari
    'ta': (0x0B80, 0x0BFF),   # Tamil
    'kn': (0x0C80, 0x0CFF),   # Kannada
    'ml': (0x0D00, 0x0D7F),   # Malayalam
    'bn': (0x0980, 0x09FF),   # Bengali
}


def voice_language_for(text: str, requested: str) -> str:
    """The language whose voice should read `text`."""
    counts = {lang: 0 for lang in _SCRIPT_RANGES}
    for ch in text:
        code = ord(ch)
        for lang, (lo, hi) in _SCRIPT_RANGES.items():
            if lo <= code <= hi:
                counts[lang] += 1
                break
    best = max(counts, key=lambda k: counts[k])
    if counts[best] == 0:
        # Nothing but Latin, digits and punctuation -- the caller's language
        # is then the only signal there is.
        return requested
    if best != requested:
        log.info('voice: reading %s-script text, using the %s voice instead of %s',
                 best, best, requested)
    return best

# VOICEZEN TEST -- the script's Telugu lines are full of అండి, and the model
# carries it into replies in other languages ("Nice to meet you Ravi అండి",
# "अंडी"). In an English or Hindi reply it is wrong to say, and its Telugu
# letters also tipped voice_language_for into reading the whole English
# sentence with the Telugu voice. A Telugu reply keeps it.
# Spellings seen or expected per script: Hindi/Marathi, Tamil (call 2330
# said அங்கீ), Bengali (অ্যান্ডি), Kannada, Malayalam, Latin.
_ANDI = re.compile(
    r"[ \t]*(?:అండి|अंडी|अंडि|अण्डी|एंडी|அங்கீ|அங்கி|அண்டி|ஆண்டி|ஆண்டீ"
    r"|অ্যান্ডি|আন্ডি|এন্ডি|ಅಂಡಿ|ആൻഡി|അണ്ടി|അൻഡി|\bandi\b)(?=[\s.,!?।]|$)",
    re.IGNORECASE,
)


def drop_foreign_andi(text: str) -> str:
    """`text` without అండి unless the rest of it has Telugu in it. Telugu
    replies mix in English business words, so any Telugu letter counts."""
    if not _ANDI.search(text):
        return text
    stripped = _ANDI.sub("", text)
    if any(0x0C00 <= ord(ch) <= 0x0C7F for ch in stripped):
        return text
    return stripped


def say_as_spoken(text: str, language: str) -> str:
    """Rewrites words the voice mispronounces into the script it reads right."""
    for pattern, said in _SPOKEN_AS_PATTERNS.get(language, []):
        text = pattern.sub(said, text)
    return text

def _resample_24k_to_16k(pcm: bytes) -> bytes:
    """S16LE mono, 24kHz -> 16kHz."""
    if not pcm:
        return b''
    try:
        import audioop  # noqa: PLC0415 -- optional, see below
        converted, _ = audioop.ratecv(pcm, 2, 1, TTS_SAMPLE_RATE, STT_SAMPLE_RATE, None)
        return converted
    except Exception:
        # audioop is gone in Python 3.13. Falling back to plain arithmetic
        # keeps recordings working through a base-image bump instead of
        # silently losing the agent's side again.
        src = array.array('h')
        src.frombytes(pcm[: len(pcm) // 2 * 2])
        if not src:
            return b''
        ratio = TTS_SAMPLE_RATE / STT_SAMPLE_RATE
        out = array.array('h', bytes(int(len(src) / ratio) * 2))
        for i in range(len(out)):
            out[i] = src[min(int(i * ratio), len(src) - 1)]
        return out.tobytes()


def _mix_into(track: bytearray, offset: int, pcm: bytes) -> None:
    """Adds `pcm` into `track` at a byte offset, growing it if needed."""
    if not pcm:
        return
    end = offset + len(pcm)
    if end > len(track):
        track.extend(bytes(end - len(track)))
    region = bytes(track[offset:end])
    try:
        import audioop  # noqa: PLC0415
        track[offset:end] = audioop.add(region, pcm, 2)
        return
    except Exception:
        pass
    a = array.array('h'); a.frombytes(region)
    b = array.array('h'); b.frombytes(pcm)
    for i in range(min(len(a), len(b))):
        # Saturating add: two voices at once must not wrap into noise.
        a[i] = max(-32768, min(32767, a[i] + b[i]))
    track[offset:end] = a.tobytes()

# VOICEZEN TEST -- finished audio for lines said word-for-word again: the
# greeting on every call, and short stock lines. Measured on real calls,
# generating the greeting was ~0.4s of the silence before the first word.
_TTS_CACHE: "OrderedDict[tuple, list[bytes]]" = OrderedDict()
_TTS_CACHE_MAX = 48
_TTS_CACHE_MAX_CHARS = 300


async def synthesize(session: aiohttp.ClientSession, text: str, language: str = "te",
                     voice_overrides: dict | None = None, speed: float | None = None,
                     legacy: bool = False):
    """_synthesize_raya, with finished audio kept for VoiceZen (see _TTS_CACHE).
    Only a synthesis that ran to the end is kept, so a reply cut off by a
    barge-in never leaves half a line in the cache."""
    if legacy or len(text) > _TTS_CACHE_MAX_CHARS:
        async for chunk in _synthesize_raya(session, text, language, voice_overrides, speed, legacy):
            yield chunk
        return
    key = (text, language, tuple(sorted((voice_overrides or {}).items())), speed)
    cached = _TTS_CACHE.get(key)
    if cached is not None:
        _TTS_CACHE.move_to_end(key)
        for chunk in cached:
            yield chunk
        return
    chunks: list[bytes] = []
    async for chunk in _synthesize_raya(session, text, language, voice_overrides, speed, legacy):
        chunks.append(chunk)
        yield chunk
    _TTS_CACHE[key] = chunks
    while len(_TTS_CACHE) > _TTS_CACHE_MAX:
        _TTS_CACHE.popitem(last=False)


async def _synthesize_raya(session: aiohttp.ClientSession, text: str, language: str = "te",
                           voice_overrides: dict | None = None, speed: float | None = None,
                           legacy: bool = False):
    """Whole-turn Raya synthesis. Yields raw S16LE PCM chunks at TTS_SAMPLE_RATE.

    Same endpoint and F32LE->S16LE conversion as agent/tts_raya.py; ported here
    directly rather than importing that module, since it depends on a running
    LiveKit job context this process doesn't have.

    `language` defaults to Telugu (this client's callers) but a campaign or
    instant-lead call passes its own configured language -- without this,
    English text was still synthesised with Telugu pronunciation rules,
    which is why an English word like "VoiceZen" came out mispronounced.

    The voice has to change with the language, not just the `language` field:
    a voice_id belongs to exactly one language on Raya, so asking the Telugu
    speaker to read Hindi gives Telugu pronunciation of Hindi text at best.
    RAYA_VOICES maps each language to a voice that actually speaks it.
    """
    # The script decides the voice; see voice_language_for.
    # VOICEZEN TEST -- both are Sep-2026 additions and only run for VoiceZen;
    # every other client speaks the requested language and raw text exactly
    # as it did before either existed.
    if not legacy:
        text = drop_foreign_andi(text)
        language = voice_language_for(text, language)
        text = say_as_spoken(text, language)
    voice_id, raya_lang = resolve_voice(language, voice_overrides)
    rate = speed if speed in SELECTABLE_SPEEDS else 1
    # ADMIN -- true only when an admin-chosen voice or speed is in play.
    custom = voice_id != resolve_voice(language)[0] or rate != 1
    body = {"text": text, "voice_id": voice_id, "model": "standard",
            "language": raya_lang, "sample_rate": TTS_SAMPLE_RATE, "speed": rate}
    async with session.post(
        "https://hub.getraya.app/v1/text-to-speech/stream",
        headers={"X-API-Key": RAYA_API_KEY, "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        json=body, timeout=aiohttp.ClientTimeout(total=30),
    ) as r:
        if r.status != 200:
            body_text = await r.text()
            if custom:
                # A chosen voice or speed Raya won't take must not become
                # dead air: say this turn with the default voice instead.
                log.error("Raya rejected the chosen voice/speed (HTTP %s %s) -- using the default voice",
                          r.status, body_text[:120])
                async for chunk in synthesize(session, text, language):
                    yield chunk
                return
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

async def groq_stream(session: aiohttp.ClientSession, messages: list[dict], model: str):
    """Yields a reply in pieces, as Groq writes it. Same contract as
    gemini_stream: a caller that already reads one can read either.

    Groq speaks the OpenAI chat-completions shape, not Gemini's -- system
    prompt as a normal message with role='system' rather than a separate
    field, and the streamed chunks are SSE 'data: {...}' lines whose
    delta lives at choices[0].delta.content.
    """
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": 0.2,
        "stream": True,
        # VoiceZen's own model keeps its configured effort; a model chosen for
        # another client gets its family's default (Qwen: thinking off).
        "reasoning_effort": GROQ_REASONING_EFFORT if model == GROQ_MODEL
        else ("none" if model.startswith("qwen/") else "low"),
    }
    if model.startswith("qwen/"):
        # Qwen writes any reasoning inline as <think>...</think> in the
        # content itself -- an earlier test had it spoken aloud mid-reply.
        # Thinking is off, but "hidden" guarantees none ever reaches the
        # voice even if that changes.
        body["reasoning_format"] = "hidden"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": GROQ_USER_AGENT,
    }
    async with session.post("https://api.groq.com/openai/v1/chat/completions",
                            json=body, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            raise RuntimeError(f"groq {model} failed: HTTP {r.status} {(await r.text())[:200]}")
        async for line in r.content:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            for choice in ev.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content", "")
                if piece:
                    yield piece


async def _pump(agen, q: "asyncio.Queue"):
    try:
        async for piece in agen:
            await q.put(("piece", piece))
        await q.put(("done", None))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await q.put(("error", e))


async def groq_hedged_stream(session: aiohttp.ClientSession, messages: list[dict], model: str):
    """groq_stream, plus a second identical request if the first hasn't
    produced a word within GROQ_HEDGE_AFTER_S. The first to speak wins and
    the other is cancelled.

    Same contract as groq_stream: raises if every request failed, yields
    nothing if every request came back empty -- llm_stream falls back to
    Gemini on either.
    """
    contenders: list[tuple] = []

    def start():
        q: asyncio.Queue = asyncio.Queue()
        contenders.append((asyncio.create_task(_pump(groq_stream(session, messages, model), q)), q))

    getters: dict = {}
    start()
    hedged = False
    deadline = time.time() + GROQ_HEDGE_AFTER_S
    winner = None
    first_piece = None
    last_error: Exception | None = None
    try:
        while winner is None and contenders:
            for c in contenders:
                if c not in getters:
                    getters[c] = asyncio.create_task(c[1].get())
            timeout = None if hedged else max(0.0, deadline - time.time())
            done, _ = await asyncio.wait(set(getters.values()), timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                hedged = True
                log.info("groq silent after %.1fs -- sending a second request", GROQ_HEDGE_AFTER_S)
                start()
                continue
            for c, g in list(getters.items()):
                if g not in done:
                    continue
                del getters[c]
                kind, val = g.result()
                if kind == "piece":
                    if winner is None:
                        winner, first_piece = c, val
                else:
                    if kind == "error":
                        last_error = val
                    contenders.remove(c)

        if winner is None:
            if last_error is not None:
                raise last_error
            return
        if hedged:
            log.info("groq hedge: request %d of %d answered first",
                     contenders.index(winner) + 1, len(contenders))
        for c in contenders:
            if c is not winner:
                c[0].cancel()
        for g in getters.values():
            g.cancel()
        getters.clear()

        yield first_piece
        q = winner[1]
        while True:
            kind, val = await q.get()
            if kind == "piece":
                yield val
            elif kind == "done":
                return
            else:
                raise val
    finally:
        for g in getters.values():
            g.cancel()
        for t, _ in contenders:
            if not t.done():
                t.cancel()


async def llm_stream(session: aiohttp.ClientSession, messages: list[dict], model: str,
                     groq_model: str | None = None):
    """The primary streaming path a call actually uses: Groq first, Gemini
    if Groq is unavailable or fails before producing anything. Mirrors
    llm_reply's own rule for a chosen model -- a bad or slow path costs
    one turn, never a silent call.

    Only falls back before any text has been yielded. Once a piece has
    reached the caller it may already be mid-sentence to the speaker, and
    starting over from a second provider would repeat or garble what was
    already spoken -- the same reasoning _stream_turn already applies to
    its own whole-reply fallback.

    A stream that finishes with HTTP 200 but zero content pieces is treated
    the same as one that raised: Groq's oss models occasionally answer with
    nothing in the visible content field on odd or garbled input, and that
    is silent unless something notices and tries Gemini instead.
    """
    if GROQ_API_KEY:
        produced_any = False
        try:
            async for piece in groq_hedged_stream(session, messages, groq_model or GROQ_MODEL):
                produced_any = True
                yield piece
            if produced_any:
                return
            log.warning("groq returned no content for this turn, falling back to gemini")
        except Exception:
            if produced_any:
                log.exception("groq failed mid-reply -- turn ends with what was already spoken")
                return
            log.exception("groq unavailable, falling back to gemini for this turn")
    async for piece in gemini_stream(session, messages, model):
        yield piece


def llm_reply(messages: list[dict], attempts: int = 3, model: str | None = None) -> str:
    """One reply from Gemini. `model` is an admin-chosen model for this
    client; if it isn't one of SELECTABLE_LLM_MODELS, or it fails, the reply
    comes from LLM_MODEL instead -- a bad setting costs one slow turn, never
    a silent call."""
    # A Groq model isn't a Gemini model: this non-streaming path answers from
    # Gemini's default instead of failing a request first.
    chosen = model if model in SELECTABLE_LLM_MODELS and not SELECTABLE_LLM_MODELS[model].get("provider") else LLM_MODEL
    if chosen != LLM_MODEL:
        try:
            return _gemini_reply(messages, chosen, attempts=2)
        except Exception:
            log.exception("chosen model %s failed -- falling back to %s", chosen, LLM_MODEL)
    return _gemini_reply(messages, LLM_MODEL, attempts)


# ---------------------------------------------------------- streaming replies
#
# The caller hears nothing between finishing their sentence and the agent
# starting to speak. Measured on real calls that silence was 2.2-2.6 seconds,
# and the bulk of it -- 1.58s on average -- was waiting for Gemini to finish
# writing the WHOLE reply before a single word could be synthesised.
#
# Nothing about the reply's first sentence depends on its last, so there is no
# reason to wait. These stream the model's output and hand each finished
# sentence to the voice as soon as it exists, which cuts the gap to roughly
# the time the first sentence takes -- about a second, and it feels like a
# conversation instead of a pause.
#
# The non-streaming path below stays exactly as it was, and is still what a
# failure falls back to: a slow reply is bad, a silent call is worse.

# A sentence ends here. Telugu uses the Latin full stop and question mark, and
# the Devanagari danda turns up in mixed text; a newline ends one too.
_SENTENCE_END = ".?!" + chr(0x0964) + chr(10)

# Say something even if the model never reaches punctuation -- a long clause
# with no full stop should not hold the whole turn silent.
_MAX_UNSPOKEN_CHARS = 140


def _next_sentence(buffer: str) -> tuple[str, str]:
    """Splits off the first complete sentence. Returns (sentence, rest)."""
    for i, ch in enumerate(buffer):
        if ch in _SENTENCE_END:
            # Keep the punctuation: the voice reads a question differently
            # from a statement, and stripping it flattens the intonation.
            return buffer[: i + 1].strip(), buffer[i + 1 :]
    if len(buffer) >= _MAX_UNSPOKEN_CHARS:
        cut = buffer.rfind(" ", 0, _MAX_UNSPOKEN_CHARS)
        if cut > 0:
            return buffer[:cut].strip(), buffer[cut:]
    return "", buffer


async def gemini_stream(session: aiohttp.ClientSession, messages: list[dict], model: str):
    """Yields Gemini's reply in pieces, as it is written.

    Same request body as _gemini_reply -- only the endpoint and the parsing
    differ -- so a change to how the model is prompted cannot apply to one
    path and not the other.
    """
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
    if SELECTABLE_LLM_MODELS.get(model, {}).get("no_thinking"):
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:streamGenerateContent?alt=sse&key={GOOGLE_API_KEY}")
    async with session.post(url, json=body,
                            timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status != 200:
            raise RuntimeError(f"{model} stream failed: HTTP {r.status} {(await r.text())[:200]}")
        async for line in r.content:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            for cand in ev.get("candidates") or []:
                for part in cand.get("content", {}).get("parts", []):
                    piece = part.get("text", "")
                    if piece:
                        yield piece


def _gemini_reply(messages: list[dict], model: str, attempts: int) -> str:
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
    if SELECTABLE_LLM_MODELS.get(model, {}).get("no_thinking"):
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
    last = ""
    for i in range(attempts):
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GOOGLE_API_KEY}",
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
    raise RuntimeError(f"{model} failed -- {last}")


async def check_customer_minutes(customer_id: int) -> bool:
    """True if this customer still has minutes to spend (plan + topup, plus
    a 10-minute grace buffer past that). Fails open on any error -- a real
    customer's call should never get blocked by a network hiccup, only by
    genuinely having no minutes left."""
    if not CHECK_MINUTES_URL or not VOICE_AGENT_SECRET:
        return True
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                CHECK_MINUTES_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-agent-secret": VOICE_AGENT_SECRET,
                    "apikey": VOICEZEN_ANON_KEY,
                    "Authorization": f"Bearer {VOICEZEN_ANON_KEY}",
                },
                json={"customer_id": customer_id},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    log.error("check-minutes failed: HTTP %s %s", r.status, await r.text())
                    return True
                data = await r.json()
                return bool(data.get("allowed", True))
    except Exception:
        log.exception("check-minutes request failed -- allowing the call")
        return True


# ADMIN -- which script a call should speak, as set from the admin dashboard.
# Returns (greeting, instructions, label) only when the dashboard says to use
# a database version for this call; None means "use the script file", which
# is also the answer to every failure -- timeout, error, not configured. A
# problem on the dashboard side can therefore never leave a call without a
# script: the worst case is exactly the behaviour from before this existed.
AGENT_CONFIG_URL = (
    os.getenv("VOICEZEN_URL", "").rstrip("/") + "/functions/v1/agent-config"
    if os.getenv("VOICEZEN_URL") else ""
)


async def fetch_agent_config(customer_id: int | None, caller_number: str | None) -> dict:
    """{"script": (greeting, instructions, label) or None, "ai": settings dict
    or None}. None for either means "as in code" -- also the answer to every
    failure."""
    if not AGENT_CONFIG_URL or not VOICE_AGENT_SECRET or not customer_id:
        return {"script": None, "ai": None}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                AGENT_CONFIG_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-agent-secret": VOICE_AGENT_SECRET,
                    "apikey": VOICEZEN_ANON_KEY,
                    "Authorization": f"Bearer {VOICEZEN_ANON_KEY}",
                },
                json={"customer_id": customer_id, "caller_number": caller_number or ""},
                # Runs alongside the minutes check, so a warm answer (0.2-0.9s
                # measured) adds no wait. 5s rather than less because a cold
                # function took over 2.5s to wake on 2026-09-18, and once a
                # client is on a saved script, timing out would quietly serve
                # the code script instead.
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status != 200:
                    log.error("agent-config failed: HTTP %s -- using the script file", r.status)
                    return {"script": None, "ai": None}
                data = await r.json()
    except Exception:
        log.exception("agent-config request failed -- using the script file")
        return {"script": None, "ai": None}
    script = None
    greeting, instructions = data.get("greeting"), data.get("instructions")
    if data.get("source") == "db" and greeting and instructions:
        script = (greeting, instructions, f"db v{data.get('version')} ({data.get('mode')})")
    ai = data.get("ai") if isinstance(data.get("ai"), dict) else None
    return {"script": script, "ai": ai}


async def lookup_client_in_db(number: str | None = None, customer_id: int | None = None) -> dict | None:
    """ADMIN -- a client added from the admin dashboard, which isn't in
    clients_registry.py. By dialled number (inbound) or by customer
    (outbound caller ID). None on no match or any failure, which leaves the
    caller with exactly the behaviour from before this existed."""
    if not AGENT_CONFIG_URL or not VOICE_AGENT_SECRET or not (number or customer_id):
        return None
    body = {"lookup_number": number} if number else {"lookup_dial_from": True, "customer_id": customer_id}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                AGENT_CONFIG_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-agent-secret": VOICE_AGENT_SECRET,
                    "apikey": VOICEZEN_ANON_KEY,
                    "Authorization": f"Bearer {VOICEZEN_ANON_KEY}",
                },
                json=body,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status != 200:
                    log.error("client lookup failed: HTTP %s", r.status)
                    return None
                data = await r.json()
    except Exception:
        log.exception("client lookup request failed")
        return None
    client = data.get("client")
    return client if isinstance(client, dict) and client.get("customer_id") else None


def vobiz_auth(found: dict | None) -> tuple[str, str]:
    """ADMIN -- (auth_id, auth_token) for a client from lookup_client_in_db:
    their own Vobiz account if they have one, else the main account."""
    own = (found or {}).get("vobiz") or {}
    if own.get("auth_id") and own.get("auth_token"):
        return own["auth_id"], own["auth_token"]
    return VOBIZ_AUTH_ID, VOBIZ_AUTH_TOKEN


def db_client_entry(found: dict) -> dict:
    """A registry-shaped entry for a client that lives only in the database.
    Telugu-pinned by default like Gundusoodi; the client's AI settings from
    the admin dashboard (language, voices, model) are applied on top."""
    return {
        "client_id": f"customer-{found['customer_id']}",
        "customer_id": found["customer_id"],
        "dial_from": found.get("number"),
        "prompt_module": None,  # its script comes from agent-config
        "stt_language": "te-IN",
        "tts_language": "te",
        "multilingual": False,
    }


# ADMIN -- the language choices the admin dashboard offers. "auto" follows
# the caller per utterance, as VoiceZen's line does from clients_registry.
FIXED_LANGUAGES = {"te-IN": "te", "en-IN": "en", "hi-IN": "hi"}


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
        self.stt = SarvamSTT(self._on_transcript, self._on_speech_start,
                             self._on_detected_language)
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
        # Raw caller-side PCM16 audio (what STT receives), captured so a call
        # can actually be listened to afterward rather than only read as a
        # transcript -- a transcript alone couldn't settle whether a real
        # call's odd caller replies were background noise or a real
        # misunderstanding.
        self._caller_audio_chunks: list[bytes] = []
        # The agent's own audio, so a recording holds both halves of the
        # conversation. Kept as (offset, pcm) pairs rather than one buffer:
        # the offset is where in the caller's timeline the agent started
        # speaking, which is what lines the two sides up on playback.
        self._bot_audio_segments: list[tuple[int, bytearray]] = []
        self._bot_audio_segment: bytearray | None = None
        # Timing, so the gap a caller actually hears can be attributed
        # rather than guessed at. A real call measured 3.8s of silence per
        # turn while the logs only accounted for 1.6s of it, and the
        # difference was invisible: it sits between the caller finishing and
        # the transcript arriving, which nothing was recording.
        self._speech_seen_at: float | None = None
        # When the current playback actually started -- guards the
        # opening greeting against a false barge-in. A real call showed
        # exactly this: STT connected, the greeting began playing, and
        # 173ms later a spurious START_SPEECH signal (almost certainly
        # line noise from the call just being answered, not the caller)
        # wiped the whole greeting before a word reached them. Nobody can
        # physically start talking 173ms after their own call connects.
        self._playback_started_at: float | None = None
        self._first_fragment_at: float | None = None
        self._transcript_at: float | None = None
        self._call_uuid = None
        self._from_number: str | None = None
        self._to_number: str | None = None
        self._direction = "inbound"
        self._campaign: dict | None = None
        # Telugu by default (this client's callers) -- overridden in start()
        # for a campaign/instant-lead call configured with a different
        # language.
        self._tts_language = "te"
        self._stt_language = "te-IN"
        # Only a multilingual client lets the caller's own language steer the
        # reply voice; a pinned line keeps whatever it was configured with.
        self._follow_caller_language = False
        # VOICEZEN TEST -- everything added for the Sep 2026 latency/audio
        # work (Groq, the shorter debounce, the barge-in guard, the
        # pronunciation table, voice_language_for) is gated on this and only
        # runs for VoiceZen. Every other client, including Gundusoodi, keeps
        # exactly its pre-Sep-2026 behaviour until this is proven out and the
        # gate is deliberately widened. Defaults True (old behaviour) until
        # start() identifies the call as VoiceZen's.
        self._legacy_engine = True
        # VOICEZEN TEST -- language-switch stickiness. A single short or
        # misheard word (Sarvam reported "bn" for a one-word "yes") used to
        # flip the reply voice immediately; now the same language has to be
        # detected twice in a row before the switch happens.
        self._pending_lang_code: str | None = None
        self._pending_lang_count = 0
        # VOICEZEN TEST -- see the guard in _on_speech_start/_on_transcript.
        self._ignored_noise_until: float | None = None
        # VOICEZEN TEST -- one speaker at a time. A caller who says "hello"
        # while the greeting is still being generated (before it starts
        # playing, so it isn't a barge-in) used to get a reply sent to the
        # phone line alongside the still-arriving greeting. Held for the
        # whole of a turn's sending, so the next turn's audio can only
        # follow it, never mix into it.
        self._speak_lock = asyncio.Lock()
        # VOICEZEN TEST -- the reply to the caller's latest words, so a newer
        # turn can replace it (see _supersede_open_turn), plus an estimate of
        # when the phone line will have finished playing everything sent to
        # it. Vobiz plays in real time, so audio is usually sent seconds
        # before the caller hears it, and "sent" alone can't tell us that.
        self._open_turn: dict | None = None
        self._line_free_at = 0.0
        self._live_speak: set = set()
        self._barge_timer: asyncio.Task | None = None
        self._started_at: float | None = None
        self._done = asyncio.Event()
        # Only set for a "Call Me Now" instant-lead call -- see start().
        self._time_limit_task: asyncio.Task | None = None
        # ADMIN -- per-client AI settings from the admin dashboard. None means
        # the server-wide default, exactly as before those settings existed.
        self._llm_model: str | None = None
        self._voice_overrides: dict | None = None
        self._tts_speed: float | None = None
        # ADMIN -- the Vobiz account this call is on, for recording it: the
        # main account unless the client has their own (set in start()).
        self._vobiz_auth: tuple[str, str] = (VOBIZ_AUTH_ID, VOBIZ_AUTH_TOKEN)

    def _apply_ai_settings(self, ai: dict) -> None:
        """Admin-chosen model, voices, speed and language for this call.
        Anything missing or unrecognised keeps the value from the code; the
        model and voices are checked again against their allow-lists where
        they're used."""
        model = ai.get("llm_model")
        if model in SELECTABLE_LLM_MODELS:
            self._llm_model = model
        voices = ai.get("voices")
        if isinstance(voices, dict):
            self._voice_overrides = {k: v for k, v in voices.items() if v in SELECTABLE_VOICES}
        if ai.get("speed") in SELECTABLE_SPEEDS:
            self._tts_speed = ai["speed"]
        language = ai.get("language")
        if language == "auto":
            self._stt_language = "unknown"
            self._follow_caller_language = True
        elif language in FIXED_LANGUAGES:
            self._stt_language = language
            self._tts_language = FIXED_LANGUAGES[language]
            self._follow_caller_language = False
        log.info("ai settings: model=%s voices=%s speed=%s stt=%s multilingual=%s",
                 self._llm_model or LLM_MODEL, self._voice_overrides, self._tts_speed or 1,
                 self._stt_language, self._follow_caller_language)

    def _on_detected_language(self, code: str) -> None:
        """Sarvam reported which language the caller just spoke.

        Only acted on for a multilingual client: on a pinned line the STT is
        already locked to one language, and a spurious detection there would
        swap the voice mid-call for no reason.
        """
        if not self._follow_caller_language:
            return
        base = (code or "").split("-")[0].lower()
        if not base or base == self._tts_language:
            self._pending_lang_code = None
            self._pending_lang_count = 0
            return
        if base not in RAYA_VOICES:
            # Heard something we have no voice for -- keep the current one
            # rather than falling back to a default mid-conversation.
            return
        # VOICEZEN TEST -- stickiness: a real call showed Sarvam reporting
        # "bn" off a single misheard one-word "yes", which used to flip the
        # voice on the spot. Every client but VoiceZen keeps that original,
        # immediate-switch behaviour; VoiceZen requires the same language
        # twice in a row first, so one bad guess can't swing the voice.
        if self._legacy_engine:
            log.info("caller language detected: %s (was %s)", base, self._tts_language)
            self._tts_language = base
            return
        if base == self._pending_lang_code:
            self._pending_lang_count += 1
        else:
            self._pending_lang_code = base
            self._pending_lang_count = 1
        if self._pending_lang_count < 2:
            log.info("caller language detected: %s (was %s) -- waiting for a second match before switching",
                      base, self._tts_language)
            return
        log.info("caller language detected: %s (was %s)", base, self._tts_language)
        self._tts_language = base
        self._pending_lang_count = 0

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

        The inconsistency is per-trunk, not just per-direction: the CDR for
        one and the same calling phone renders as "919493918625" against
        Gundusoodi's number but "09493918625" against VoiceZen's, which is
        the Indian national trunk prefix rather than a country code. Bolting
        a "+" onto that gives "+09493918625", which matches no client and no
        customer -- the call would silently fall through to whichever client
        the registry defaults to. So strip a leading 0 and add the country
        code instead of assuming every plus-less number is already E.164.
        """
        raw = self._from_number if self._direction == "outbound" else self._to_number
        return _to_e164(raw)

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
            language_name = self._campaign.get("language") or "English"
            # ADMIN -- a browser test of a client's call script from the
            # dashboard's script page: run exactly the greeting and
            # instructions on screen, in the script's language. Test-only;
            # no phone line, no minutes check, not logged or billed.
            if self._campaign.get("script_test"):
                instructions = self._campaign.get("script_instructions") or instructions
                greeting = self._campaign.get("script_greeting") or greeting
                lang = (self._campaign.get("language") or "te").split("-")[0]
                self._stt_language = {"en": "en-IN", "hi": "hi-IN"}.get(lang, "te-IN")
                self._tts_language = lang if lang in ("en", "hi") else "te"
            # Both TTS and STT stay on the __init__ defaults (te / te-IN) for
            # now, matching each other, until Raya's TTS actually supports a
            # language other than Telugu -- Raya returned HTTP 422 for "en"
            # on a real call and went silent for the rest of the
            # conversation. Sarvam STT itself had no problem accepting
            # "en-IN" (correctly transcribed English speech on that same
            # call), but leaving STT on English while TTS can only reply in
            # Telugu would be a confusing half-fix, so both are pinned to
            # Telugu together until TTS is properly fixed.
            log.info("serving campaign %s contact %s (%s, language=%s)",
                      self._campaign.get("campaign_id"), self._campaign.get("contact_id"),
                      self._campaign.get("business_name"), language_name)
            if self._campaign.get("instant_lead"):
                self._time_limit_task = asyncio.create_task(
                    self._enforce_instant_lead_time_limit())
            # Campaign calls are the ones reviewed afterwards, so they get a
            # real two-sided recording from Vobiz. Fired off rather than
            # awaited: a recording that fails to start must not delay the
            # greeting, and _fetch_vobiz_recording falls back to the
            # caller-only capture if nothing turns up.
            # ADMIN -- a campaign for a client on their own Vobiz account is
            # recorded on that account; registry clients never wait on this.
            campaign_customer = self._campaign.get("customer_id")
            if campaign_customer and not dial_from_for_customer(campaign_customer):
                self._vobiz_auth = vobiz_auth(await lookup_client_in_db(customer_id=campaign_customer))
            if not self._campaign.get("script_test"):  # no phone call to record
                asyncio.create_task(self._start_vobiz_recording())
        else:
            # ADMIN -- a number missing from clients_registry.py may belong to
            # a client added from the admin dashboard. Asked only for unknown
            # numbers, so Gundusoodi's and VoiceZen's calls never wait on it.
            found = None
            if self._client_number and not lookup_client(self._client_number):
                found = await lookup_client_in_db(number=self._client_number)
            if found:
                self.client = db_client_entry(found)
                self._vobiz_auth = vobiz_auth(found)
                greeting = instructions = None  # filled from agent-config below
            else:
                self.client, greeting, instructions = load_client_script(self._client_number)
            # Per-client language config. Defaults preserve the original
            # Telugu-pinned behaviour for any client that doesn't set these.
            self._stt_language = self.client.get("stt_language") or self._stt_language
            self._tts_language = self.client.get("tts_language") or self._tts_language
            self._follow_caller_language = bool(self.client.get("multilingual"))
            log.info("serving client '%s' (%s call, identified by %s, stt=%s multilingual=%s)",
                      self.client["client_id"], self._direction, self._client_number,
                      self._stt_language, self._follow_caller_language)

        # VOICEZEN TEST -- see the flag's definition in __init__. Campaign and
        # DB-registry calls have no client_id here and stay on the legacy
        # engine along with everyone but VoiceZen, until this is widened on
        # purpose.
        self._legacy_engine = not (self.client and self.client.get("client_id") == "voicezen")
        customer_id = self._campaign.get("customer_id") if self._campaign else self.client.get("customer_id")
        # VOICEZEN TEST -- answer() already started these (see there).
        prefetched = None
        prefetch = (info or {}).get("prefetch")
        if prefetch is not None and not self._legacy_engine:
            try:
                prefetched = await prefetch
            except Exception:
                log.exception("early call setup failed -- asking again")
        # ADMIN -- asked in parallel with the minutes check below, so it adds
        # no wait before the greeting. Campaign calls keep their own script.
        config_task = None
        if prefetched is not None:
            allowed, early_config = prefetched
            config_task = asyncio.create_task(asyncio.sleep(0, result=early_config))
        else:
            if not self._campaign:
                other_party = self._from_number if self._direction != "outbound" else self._to_number
                config_task = asyncio.create_task(fetch_agent_config(customer_id, _to_e164(other_party)))
            script_test = bool(self._campaign and self._campaign.get("script_test"))
            allowed = await check_customer_minutes(customer_id) if customer_id and not script_test else True
        if not allowed:
            log.warning("call blocked: customer %s has no minutes remaining", customer_id)
            if config_task is not None:
                config_task.cancel()
            self.messages = [{"role": "system", "content": "(blocked -- out of minutes)"}]
            blocked_message = (
                "క్షమించండి అండి, ప్రస్తుతం ఈ సర్వీస్ అందుబాటులో లేదు. "
                "దయచేసి మమ్మల్ని నేరుగా సంప్రదించండి."
            )
            self.messages.append({"role": "assistant", "content": blocked_message})
            self._speak_task = asyncio.create_task(self._speak(blocked_message, end_call=True))
            return

        if config_task is not None:
            config = await config_task
            if config["script"]:
                greeting, instructions, label = config["script"]
                log.info("script for client '%s': %s", self.client["client_id"], label)
            elif self.client.get("prompt_module"):
                log.info("script for client '%s': code (%s)", self.client["client_id"], self.client["prompt_module"])
            if config["ai"]:
                self._apply_ai_settings(config["ai"])

        if greeting is None or instructions is None:
            # A database-only client whose script couldn't be loaded. Speaking
            # the fallback script beats a silent line -- the same trade-off
            # load_client_script makes for an unknown number.
            log.error("no script for client '%s' -- using the fallback script", self.client["client_id"])
            _, greeting, instructions = load_client_script(None)

        instructions = _instructions_no_livekit(instructions)
        # VOICEZEN TEST -- VoiceZen's own rules (always Telugu, one question
        # per reply, pricing, mishearing, endings) live in script_voicezen.py,
        # kept short because the whole script is re-read on every reply.
        self.messages = [{"role": "system", "content": instructions}]

        if self._legacy_engine:
            await self.stt.connect(
                self._stt_language,
                STT_PROMPT_GUNDUSOODI if (self.client or {}).get("client_id") == "gundusoodi" else STT_PROMPT)
        # Recorded in history immediately, not after _speak() finishes -- a
        # real call showed the bug this avoids: the caller's first reply
        # arrived before this line existed, so Gemini's context had no
        # assistant turn at all and it re-opened with the greeting instead of
        # answering, because as far as its history showed it hadn't spoken yet.
        self.messages.append({"role": "assistant", "content": greeting})
        self._speak_task = asyncio.create_task(self._speak(greeting))
        if not self._legacy_engine:
            # VOICEZEN TEST -- greeting first, then connect speech-to-text
            # while it plays: nobody answers a greeting they haven't heard.
            log.info("TIMING call greeting_started=%.2fs after the stream opened",
                     time.time() - self._started_at)
            if VOICEZEN_STT_PROVIDER == "cartesia" and CARTESIA_API_KEY:
                # Swapped before it ever connects, so no audio reaches the
                # unused Sarvam client.
                self.stt = CartesiaSTT(self._on_transcript, self._on_speech_start,
                                       self._on_detected_language)
            await self.stt.connect(self._stt_language, STT_PROMPT_VOICEZEN)

    async def on_media(self, pcm16_b64: str):
        raw = base64.b64decode(pcm16_b64)
        self._caller_audio_chunks.append(raw)
        await self.stt.send_audio(raw)

    async def _on_speech_start(self):
        """Sarvam VAD says the caller started talking. If the caller is still
        hearing audio, this is a barge-in: flush it."""
        self._speech_seen_at = time.time()
        if not self.playing:
            return
        # A caller cannot physically begin speaking a few hundred ms into
        # our own line just connecting -- this early, a VAD trigger is
        # call-setup noise, not the caller. MIN_BARGE_IN_MS is how early
        # is too early; anything past it is a real interruption, same as
        # before.
        # VOICEZEN TEST -- every client but VoiceZen keeps the original
        # behaviour: any speech_start while playing is treated as a real
        # barge-in, no minimum.
        if not self._legacy_engine and self._playback_started_at is not None:
            elapsed_ms = (time.time() - self._playback_started_at) * 1000
            if elapsed_ms < MIN_BARGE_IN_MS:
                log.info('barge-in ignored: %.0fms into playback, too early to be real speech', elapsed_ms)
                # VOICEZEN TEST -- a real call showed Sarvam transcribing
                # this exact same noise burst into text ("Booth.com") a
                # moment later, and the bot answered it as if the caller had
                # spoken. Ignoring the barge-in only stopped it from
                # interrupting playback; it did nothing to stop the
                # resulting transcript being treated as a real turn. Any
                # transcript fragment arriving in the same window this noise
                # was dismissed in is almost certainly the same noise, not
                # real speech, and is dropped in _on_transcript.
                self._ignored_noise_until = self._playback_started_at + (MIN_BARGE_IN_MS / 1000)
                return
        if not self._legacy_engine:
            # VOICEZEN TEST -- don't stop on the sound alone. A real call was
            # cut off four times by "హ్మ్"/"ఓకే"/noise, losing the whole
            # coaching-centre pitch. _on_transcript decides as soon as the
            # words arrive; if they haven't by BARGE_IN_CONFIRM_S, the caller
            # is still talking and that is a real interruption.
            if self._barge_timer is None or self._barge_timer.done():
                self._barge_timer = asyncio.create_task(self._confirm_barge_in())
            return
        await self._barge_in()

    async def _confirm_barge_in(self):
        await asyncio.sleep(BARGE_IN_CONFIRM_S)
        if self.playing:
            log.info("barge-in: caller still talking after %.1fs", BARGE_IN_CONFIRM_S)
            await self._barge_in()

    async def _barge_in(self):
        log.info("barge-in: clearing playback")
        # clearAudio is the ONLY thing that stops audio already queued inside
        # Vobiz. Cancelling the task just stops us sending more, and by now
        # there is usually nothing left to send -- the whole utterance was
        # handed over seconds ago.
        await self._send({"event": "clearAudio", "streamId": self.stream_id})
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
        if not self._legacy_engine:
            # A reply can be sending while another waits on the speak lock.
            for t in list(self._live_speak):
                if not t.done():
                    t.cancel()
        # Vobiz does NOT send playedStream for a checkpoint whose audio was
        # interrupted, so clear the state here or it would never clear.
        self.playing = False
        self._awaiting_checkpoint = None
        self._line_free_at = time.time()

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
        # VOICEZEN TEST -- only checked at the start of a new turn: a real
        # call showed the exact noise burst that a too-early barge-in
        # ignored getting transcribed as text ("Booth.com") a moment later
        # and answered as if it were real speech. Dropped here rather than
        # ever entering the pending buffer.
        if (not self._legacy_engine and not self._pending
                and self._ignored_noise_until is not None
                and time.time() < self._ignored_noise_until):
            log.info("transcript ignored: %r arrived in the same window as dismissed call-setup noise", text)
            return
        if not self._legacy_engine and not self._pending:
            timer = self._barge_timer
            waiting = timer is not None and not timer.done()
            ot = self._open_turn
            generating = ot is not None and ot["task"] is not None and not ot["task"].done()
            talking_left = self._line_free_at - time.time()
            if _is_filler(text) and (waiting or generating or talking_left > 0):
                # "హ్మ్" is never an answer, even at the end of a question.
                if waiting:
                    timer.cancel()
                log.info("ignored %r: filler while the bot is talking", text)
                return
            audible_for = (time.time() - self._playback_started_at
                           if self.playing and self._playback_started_at else 0.0)
            if _is_minor(text) and talking_left > 1.0 and audible_for > 1.0:
                # One word or an "ఓకే" with the bot well into a sentence:
                # listening, or noise the recogniser turned into a word
                # ("Mom", "माझी", "சரி" on a real call). Keep talking.
                # Only once the caller has actually been hearing the bot:
                # a word arriving while the reply is still being prepared is
                # the caller finishing their own sentence (call 970 lost
                # "...plans, I want those first" that way).
                if waiting:
                    timer.cancel()
                log.info("ignored %r: too short to interrupt the bot", text)
                return
            if waiting:
                # Real words while the bot is talking: stop now rather than
                # at the timer.
                timer.cancel()
                if self.playing:
                    await self._barge_in()
        log.info("caller (partial): %s", text)
        if not self._pending:
            self._first_fragment_at = time.time()
        self._pending.append(text)
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._flush_turn_after_debounce())

    async def _flush_turn_after_debounce(self, extended: bool = False):
        # VOICEZEN TEST -- 0.9s is the original, pre-Sep-2026 debounce; every
        # client but VoiceZen still gets it.
        debounce_s = TURN_DEBOUNCE_S if not self._legacy_engine else 0.9
        if extended:
            debounce_s = TRAILING_CONNECTOR_WAIT_S
        try:
            await asyncio.sleep(debounce_s)
        except asyncio.CancelledError:
            return  # more speech arrived; a newer timer owns the turn
        text = " ".join(self._pending).strip()
        if not text:
            return
        if not self._legacy_engine and not extended and _ends_mid_thought(text):
            # VOICEZEN TEST -- "అంటే..." and a pause to think is not a
            # question. Call 970 answered a lone "అంటే" and talked over the
            # rest of the caller's sentence. Wait longer; if nothing more
            # comes, answer what was said.
            log.info("waiting for more: %r ends mid-thought", text)
            self._debounce_task = asyncio.create_task(self._flush_turn_after_debounce(extended=True))
            return
        self._pending.clear()
        # A separate task from here on: once the wait is over the turn
        # is committed, and a fragment arriving moments later must not be
        # able to cancel a reply already being generated or spoken.
        # VOICEZEN TEST -- every client but VoiceZen stays on the original,
        # cancellable inline call, including the drop-on-late-fragment bug
        # that fix addressed, until Gundusoodhi is explicitly moved onto it.
        if self._legacy_engine:
            await self._handle_turn(text)
            return
        prev = self._open_turn
        generating = prev is not None and prev["task"] is not None and not prev["task"].done()
        still_talking = self._line_free_at - time.time()
        if _is_backchannel(text) and (generating or still_talking > 1.0):
            # "Okay"/"Hmm" while the bot is mid-answer is listening, not a
            # new question. Under a second left, it's more likely an answer
            # to the question just asked, so it goes through as a turn.
            log.info("ignored %r: acknowledgement while the bot is still talking", text)
            return
        text = self._supersede_open_turn(text)
        turn = {"text": text, "msg_start": None, "heard_at": None,
                "task": None, "speak_task": None, "rollback": False}
        self._open_turn = turn
        turn["task"] = asyncio.create_task(self._handle_turn(text, turn))

    def _supersede_open_turn(self, text: str) -> str:
        """VOICEZEN TEST -- the caller said something new while the bot still
        had an answer in progress. Stops that answer and returns the text the
        new turn should answer.

        If the caller hadn't heard any of the old answer, it is removed from
        the history (the model would otherwise believe it said something it
        never did) and the caller's earlier words are merged into this turn.
        If they had heard part of it, it stays in the history and only the
        rest is cut, the same as a barge-in.
        """
        prev = self._open_turn
        now = time.time()
        busy = [t for t in ((prev or {}).get("task"), (prev or {}).get("speak_task"))
                if t is not None and not t.done()]
        line_busy = now < self._line_free_at
        if not busy and not line_busy:
            return text
        unheard = prev is not None and (prev["heard_at"] is None or prev["heard_at"] > now)
        log.info("caller spoke again while the last reply was %s -- replacing it",
                 "not yet heard" if unheard else "still playing")
        if prev is not None:
            prev["rollback"] = unheard
            for t in busy:
                t.cancel()
        for t in list(self._live_speak):
            if not t.done():
                t.cancel()
        if line_busy:
            # Not awaited: this runs between committing a turn and starting
            # it, and a cancel landing on an await here would lose the turn.
            asyncio.create_task(self._send({"event": "clearAudio", "streamId": self.stream_id}))
        self.playing = False
        self._awaiting_checkpoint = None
        self._line_free_at = now
        if unheard:
            if prev["task"] is not None and prev["task"].done() and prev["msg_start"] is not None:
                # Finished generating, so its own cancel handler won't run.
                del self.messages[prev["msg_start"]:]
            return (prev["text"] + " " + text).strip()
        return text

    def _note_line_audio(self, nbytes: int, turn: dict | None) -> None:
        """Advances the estimate of when the line will finish playing."""
        start = max(time.time(), self._line_free_at)
        if turn is not None and turn["heard_at"] is None:
            turn["heard_at"] = start
        self._line_free_at = start + nbytes / (TTS_SAMPLE_RATE * 2)

    async def _handle_turn(self, text: str, turn: dict | None = None):
        # user_speech_end / stt_transcript_ready. first_fragment_at is the
        # honest proxy for 'the caller stopped talking' -- it is set the
        # instant Sarvam sends its FIRST final for this turn, before the
        # debounce wait and before any later fragments are merged in.
        #
        # transcript_at is a LOCAL value from here on, threaded through to
        # _stream_turn and _speak_queue as a parameter rather than read back
        # from a shared self._transcript_at. A real VoiceZen call crashed
        # (TypeError: float - NoneType) because two turns now overlap (see
        # the VOICEZEN TEST note above) -- turn B's value was wiped by turn
        # A's still-running _speak_queue resetting the same shared attribute
        # to None right before turn B read it back.
        transcript_at = time.time()
        stt_ready_after = (
            transcript_at - self._first_fragment_at if self._first_fragment_at else None
        )
        end_to_ready = (
            transcript_at - self._speech_seen_at if self._speech_seen_at else None
        )
        speech_seen_at = self._speech_seen_at
        log.info(
            "TIMING turn stt_transcript_ready=%s speech_to_ready=%s debounce=%.2fs",
            f'{stt_ready_after:.2f}s' if stt_ready_after is not None else 'n/a',
            f'{end_to_ready:.2f}s' if end_to_ready is not None else 'n/a',
            TURN_DEBOUNCE_S if not self._legacy_engine else 0.9,
        )
        log.info("caller: %s", text)
        async with self._turn_lock:
            msg_start = len(self.messages)
            if turn is not None:
                turn["msg_start"] = msg_start
            try:
                await self._answer_turn(text, transcript_at, speech_seen_at, turn)
            except asyncio.CancelledError:
                # Replaced by a newer turn before the caller heard any of it.
                if turn is not None and turn["rollback"]:
                    del self.messages[msg_start:]
                raise

    async def _answer_turn(self, text: str, transcript_at: float,
                           speech_seen_at: float | None, turn: dict | None):
        self.messages.append({"role": "user", "content": text})
        # Streaming first: it starts speaking the opening sentence while
        # the model is still writing, which is where most of the old
        # 2.2-2.6s silence went. Falls back to waiting for the whole
        # reply if the stream fails before a word has been spoken.
        try:
            await self._stream_turn(transcript_at, speech_seen_at, turn)
            return
        except Exception:
            log.exception("streaming reply failed -- falling back to a whole reply")
        try:
            reply = await asyncio.to_thread(llm_reply, self._messages_for_llm(), 3, self._llm_model)
        except Exception:
            log.exception("llm failed")
            return
        self.messages.append({"role": "assistant", "content": reply})
        spoken = reply.replace(END_MARKER, "").strip()
        end_call = _reply_wants_end_call(END_MARKER in reply, spoken, self._legacy_engine, self._last_caller_words())
        log.info("bot: %s%s", spoken, "  [END_CALL]" if end_call else "")
        self._speak_task = asyncio.create_task(self._speak(spoken, end_call=end_call, turn=turn))
        if turn is not None:
            turn["speak_task"] = self._speak_task

    def _last_caller_words(self) -> str | None:
        """What the caller said most recently, for the goodbye check."""
        return next((m["content"] for m in reversed(self.messages) if m["role"] == "user"), None)

    def _messages_for_llm(self) -> list[dict]:
        """The history sent to the model. For VoiceZen (the non-legacy
        engine) a note naming the caller's language is added for this one
        request -- never stored in the history. Every other client gets the
        history unchanged."""
        if self._legacy_engine:
            return self.messages
        lang = _caller_language(self._last_caller_words())
        if not lang:
            return self.messages
        return self.messages + [{"role": "system", "content": (
            f"The caller's latest message is in {lang}. Write this whole reply in {lang} only -- "
            f"use the {lang} version of any example line; an example that exists only in Telugu must be fully translated into {lang}, not copied. Not a single sentence in another language.")}]

    async def _stream_turn(self, transcript_at: float, speech_seen_at: float | None,
                           turn: dict | None = None):
        """Speaks the reply sentence by sentence, as the model writes it.

        Raises before anything is spoken if the stream cannot start, so the
        caller can fall back to the whole-reply path. Once a sentence has
        been said, falling back would repeat it, so a later failure just ends
        the turn with what was already spoken.

        transcript_at/speech_seen_at are this turn's own values, captured by
        the caller before this turn's _speak_queue task can outlive the
        _turn_lock and race a later turn over shared instance state.
        """
        model = self._llm_model if self._llm_model in SELECTABLE_LLM_MODELS else LLM_MODEL
        queue: asyncio.Queue = asyncio.Queue()
        spoken_any = False
        full = ""
        buffer = ""

        # One task does the speaking, in order, while this one keeps reading
        # from the model -- that overlap is the whole point.
        self._speak_task = asyncio.create_task(
            self._speak_queue(queue, transcript_at, speech_seen_at, turn))
        if turn is not None:
            turn["speak_task"] = self._speak_task
        # VOICEZEN TEST -- Groq only ever runs for VoiceZen; every other
        # client streams from Gemini exactly as before Groq existed.
        stream = (llm_stream if not self._legacy_engine else gemini_stream)
        # ADMIN -- a Groq model chosen for this client in the dashboard: Groq
        # first, this client's Gemini default (LLM_MODEL) as the fallback.
        if SELECTABLE_LLM_MODELS.get(model, {}).get("provider") == "groq":
            stream = functools.partial(llm_stream, groq_model=model)
            model = LLM_MODEL
        # VOICEZEN TEST -- VOICEZEN_LLM names a Gemini model to use for
        # VoiceZen instead of Groq, without touching LLM_MODEL (Gundusoodi's).
        if not self._legacy_engine and VOICEZEN_LLM.startswith("gemini"):
            stream, model = gemini_stream, VOICEZEN_LLM
        # VOICEZEN TEST -- one question per reply, enforced here rather than
        # trusted to the model: call 996 on gpt-oss-120b spoke four questions
        # run together as one 15-second reply. Once a question has been
        # spoken the rest of the reply is dropped unsaid, and the history
        # keeps only what the caller actually heard.
        said: list[str] = []
        asked = False
        gen = stream(self.http, self._messages_for_llm(), model)
        try:
            async for piece in gen:
                full += piece
                if buffer == '' and full == piece:
                    log.info("TIMING turn llm_first_token=%.2fs after transcript",
                             time.time() - transcript_at)
                buffer += piece
                while True:
                    sentence, buffer = _next_sentence(buffer)
                    if not sentence:
                        break
                    say = sentence.replace(END_MARKER, "").strip()
                    if say and not self._legacy_engine:
                        say = _speak_phone_digits(say)
                    if say:
                        spoken_any = True
                        said.append(say)
                        await queue.put(say)
                        if not self._legacy_engine and say.endswith("?"):
                            asked = True
                            break
                if asked:
                    # Stops the model's response now rather than leaving it
                    # streaming, unread, until it finishes on its own.
                    await gen.aclose()
                    break
            tail = buffer.replace(END_MARKER, "").strip()
            if tail and not self._legacy_engine:
                tail = _speak_phone_digits(tail)
            if tail and not asked:
                spoken_any = True
                said.append(tail)
                await queue.put(tail)
            if asked:
                rest = (full[len(" ".join(said)):] if full.startswith(" ".join(said)) else buffer).replace(END_MARKER, "").strip()
                if rest:
                    log.info("dropped the rest of the reply after its question: %r", rest[:200])
                full = " ".join(said)
        except Exception:
            if not spoken_any:
                # Nothing was said, so the fallback can safely say it all.
                await queue.put(None)
                raise
            log.exception("stream broke mid-reply -- ending the turn with what was said")

        if not spoken_any and not full.strip():
            await queue.put(None)
            raise RuntimeError("stream produced no text")

        self.messages.append({"role": "assistant", "content": full})
        spoken_full = full.replace(END_MARKER, "").strip()
        end_call = _reply_wants_end_call(END_MARKER in full, spoken_full, self._legacy_engine, self._last_caller_words())
        log.info("bot: %s%s", spoken_full, "  [END_CALL]" if end_call else "")
        # None means "no more sentences"; the speaker hangs up after it if
        # the reply asked for the call to end.
        await queue.put(("END_CALL",) if end_call else None)

    async def _speak_queue(self, queue: "asyncio.Queue", transcript_at: float,
                            speech_seen_at: float | None, turn: dict | None = None):
        """Synthesises and sends each sentence in turn, until told to stop.

        One task for the whole turn, so a barge-in cancelling self._speak_task
        stops the entire reply -- not just the sentence being spoken.
        """
        end_call = False
        me = asyncio.current_task()
        self._live_speak.add(me)
        me.add_done_callback(self._live_speak.discard)
        # Taken before anything else, so a cancel while waiting holds nothing.
        lock = None if self._legacy_engine else self._speak_lock
        if lock is not None:
            await lock.acquire()
        self.speaking = True
        self._begin_bot_audio()
        self._checkpoint_n += 1
        name = f"turn-{self._checkpoint_n}"
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, tuple):
                    end_call = True
                    break
                async for chunk in synthesize(self.http, item, self._tts_language,
                                              self._voice_overrides, self._tts_speed,
                                              legacy=self._legacy_engine):
                    self._collect_bot_audio(chunk)
                    if transcript_at is not None:
                        now = time.time()
                        log.info(
                            "TIMING turn tts_first_chunk_emitted=%.2fs telephony_playback_started=%.2fs total_turnaround=%.2fs",
                            now - transcript_at,
                            now - transcript_at,
                            now - (speech_seen_at or transcript_at),
                        )
                        transcript_at = None
                    frame = 960
                    for i in range(0, len(chunk), frame):
                        if not self.playing:
                            self._playback_started_at = time.time()
                        self.playing = True
                        self._note_line_audio(len(chunk[i:i + frame]), turn)
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
            self.speaking = False
            if lock is not None:
                lock.release()

        if end_call:
            for _ in range(300):
                if not self.playing:
                    break
                await asyncio.sleep(0.1)
            await self._send({"event": "stop", "streamId": self.stream_id})
            self._done.set()

    def _begin_bot_audio(self) -> None:
        """Marks where in the caller timeline this turn starts speaking."""
        offset = sum(len(c) for c in self._caller_audio_chunks)
        self._bot_audio_segment = bytearray()
        self._bot_audio_segments.append((offset, self._bot_audio_segment))

    def _both_sides(self) -> bytes:
        """The whole call as one mono track: the caller, with the agent mixed in.

        The caller's audio is the timeline -- the phone line sends it
        continuously, silence included -- so the agent's turns are laid over
        it at the offset where each began.

        One known imprecision: the agent's audio is sent to the phone faster
        than it plays, so a long turn drifts slightly ahead of where the
        caller actually heard it. Each turn starts in the right place, which
        is what matters for judging a reply's timing and sound.
        """
        caller = b''.join(self._caller_audio_chunks)
        if not self._bot_audio_segments:
            return caller
        try:
            mixed = bytearray(caller)
            for offset, segment in self._bot_audio_segments:
                _mix_into(mixed, offset, _resample_24k_to_16k(bytes(segment)))
            return bytes(mixed)
        except Exception:
            # A recording of one side beats no recording at all.
            log.exception('could not mix the agent audio into the recording')
            return caller

    def _collect_bot_audio(self, chunk: bytes) -> None:
        if self._bot_audio_segment is not None:
            self._bot_audio_segment += chunk

    async def _speak(self, text: str, end_call: bool = False, turn: dict | None = None):
        me = asyncio.current_task()
        self._live_speak.add(me)
        me.add_done_callback(self._live_speak.discard)
        # See _speak_lock in __init__.
        lock = None if self._legacy_engine else self._speak_lock
        if lock is not None:
            await lock.acquire()
        self.speaking = True
        self._begin_bot_audio()
        self._checkpoint_n += 1
        name = f"turn-{self._checkpoint_n}"
        try:
            async for chunk in synthesize(self.http, text, self._tts_language,
                                          self._voice_overrides, self._tts_speed,
                                          legacy=self._legacy_engine):
                self._collect_bot_audio(chunk)
                # ~20ms frames at 24kHz/16-bit mono = 960 bytes.
                frame = 960
                for i in range(0, len(chunk), frame):
                    # From the first frame on, the caller is hearing us --
                    # even though we finish SENDING seconds before they finish
                    # HEARING. This stays true until playedStream.
                    if not self.playing:
                        self._playback_started_at = time.time()
                    self.playing = True
                    self._note_line_audio(len(chunk[i:i + frame]), turn)
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
            if lock is not None:
                lock.release()

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
        if getattr(self, "_barge_timer", None) is not None and not self._barge_timer.done():
            self._barge_timer.cancel()
        if self._speak_task:
            self._speak_task.cancel()
            # Awaited, not just cancelled: a cancelled task collected before
            # it has finished unwinding logs 'Task was destroyed but it is
            # pending', which is noise that hides real errors in the log.
            try:
                await self._speak_task
            except (asyncio.CancelledError, Exception):
                pass
        # Otherwise a pending debounce fires after the caller has hung up and
        # tries to answer a turn on a closed socket.
        if self._debounce_task:
            self._debounce_task.cancel()
        if self._time_limit_task:
            self._time_limit_task.cancel()
        await self.stt.close()
        await self.http.close()
        await self._report()

    async def _enforce_instant_lead_time_limit(self):
        """"Call Me Now" (website instant-lead) calls only -- cuts the call
        at INSTANT_LEAD_TIME_LIMIT_SECONDS regardless of where the
        conversation is. Every other call type (Gundusoodi inbound,
        scheduled/batch campaigns) is unaffected; see start(), which only
        launches this task when self._campaign["instant_lead"] is true.

        Speaks a short closing line first rather than dropping the line
        silently -- a call that just goes dead reads as a failure, not a
        deliberate wrap-up.
        """
        try:
            await asyncio.sleep(INSTANT_LEAD_TIME_LIMIT_SECONDS)
        except asyncio.CancelledError:
            return
        if self._done.is_set():
            return
        log.info("instant-lead call hit the %ss time limit -- wrapping up",
                  INSTANT_LEAD_TIME_LIMIT_SECONDS)
        # Cut off whatever the bot is mid-saying so the closing line isn't
        # queued behind it -- the limit is on wall-clock time, not on the
        # conversation reaching a natural pause.
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
            try:
                await self._speak_task
            except (asyncio.CancelledError, Exception):
                pass
        closing = INSTANT_LEAD_TIME_LIMIT_CLOSING.get(
            self._tts_language, INSTANT_LEAD_TIME_LIMIT_CLOSING["te"])
        self.messages.append({"role": "assistant", "content": closing})
        self._speak_task = asyncio.create_task(self._speak(closing, end_call=True))

    async def _start_vobiz_recording(self) -> None:
        """Ask Vobiz to record this call, both sides, at the carrier.

        We can't build a correct two-sided recording ourselves: TTS frames
        are pushed to Vobiz as fast as the socket takes them, seconds ahead
        of the caller actually hearing them (see _speak), and a barge-in
        discards audio we already sent. So our own send timestamps say
        nothing about when anything was heard. Vobiz records where the two
        legs actually meet, which is the only place the real timeline
        exists.

        Stereo, so the caller and the agent land on separate channels and
        can be told apart when reviewing.
        """
        if not self._call_uuid:
            return
        auth_id, auth_token = self._vobiz_auth
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{VOBIZ_API}/Account/{auth_id}/Call/{self._call_uuid}/Record/",
                    headers={"X-Auth-ID": auth_id, "X-Auth-Token": auth_token,
                             "Content-Type": "application/json"},
                    json={"file_format": "mp3", "record_channel_type": "Stereo",
                          "time_limit": VOBIZ_RECORD_TIME_LIMIT},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    body = await r.text()
                    if 200 <= r.status < 300:
                        log.info("recording started: call=%s", self._call_uuid)
                    else:
                        log.warning("recording start failed: HTTP %s %s", r.status, body[:200])
        except Exception:
            log.exception("recording start threw")

    async def _fetch_vobiz_recording(self) -> tuple[str, str] | None:
        """Vobiz's own recording of this call as (base64, format).

        Deleted from Vobiz as soon as it's been pulled down. Vobiz bills
        recording storage for as long as it holds the file, so keeping our
        copy in our own bucket and letting theirs go immediately is what
        stops a per-call storage charge accruing every month. Our copy is
        what the dashboard plays, and what the 7-day cleanup expires.

        Returns None if the recording never appeared, which leaves the
        caller-only capture as the fallback.
        """
        if not self._call_uuid:
            return None
        auth_id, auth_token = self._vobiz_auth
        headers = {"X-Auth-ID": auth_id, "X-Auth-Token": auth_token}
        base = f"{VOBIZ_API}/Account/{auth_id}/Recording/"
        try:
            async with aiohttp.ClientSession() as s:
                # The file is still being written when the call ends, so this
                # waits for it rather than assuming it's there.
                record = None
                for attempt in range(VOBIZ_RECORD_POLL_ATTEMPTS):
                    async with s.get(base, headers=headers,
                                     params={"call_uuid": self._call_uuid},
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            objects = (await r.json()).get("objects") or []
                            if objects and objects[0].get("recording_url"):
                                record = objects[0]
                                break
                    await asyncio.sleep(VOBIZ_RECORD_POLL_DELAY)
                if not record:
                    log.warning("no Vobiz recording for call=%s after %ss",
                                self._call_uuid,
                                VOBIZ_RECORD_POLL_ATTEMPTS * VOBIZ_RECORD_POLL_DELAY)
                    return None

                async with s.get(record["recording_url"], headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status != 200:
                        log.warning("recording download failed: HTTP %s", r.status)
                        return None
                    audio = await r.read()

                recording_id = record.get("recording_id")
                if recording_id:
                    async with s.delete(f"{base}{recording_id}/", headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                        log.info("recording %s pulled (%d bytes) and deleted from Vobiz: HTTP %s",
                                 recording_id, len(audio), r.status)

                fmt = (record.get("recording_format") or "mp3").lower()
                return base64.b64encode(audio).decode("ascii"), fmt
        except Exception:
            log.exception("fetching Vobiz recording threw")
            return None

    def _encode_recording_b64(self) -> str | None:
        """Caller-side audio only (what STT received), as a base64 WAV.

        The fallback for when Vobiz's own recording isn't available. It has
        the agent's replies missing entirely -- silence wherever the bot was
        speaking -- so it's worth having but not worth preferring.
        """
        if not self._caller_audio_chunks:
            return None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit PCM, matches what Sarvam receives
            wf.setframerate(STT_SAMPLE_RATE)
            wf.writeframes(self._both_sides())
        return base64.b64encode(buf.getvalue()).decode("ascii")

    async def _report(self):
        """Send the finished call to the VoiceZen Dashboard. Runs after
        everything else has torn down, matching voicezen.py's own stated
        design: nothing here can affect a call that has already ended."""
        if self._started_at is None or not self._client_number:
            return  # never reached start() -- nothing real to report
        duration = time.time() - self._started_at
        turns = [m for m in self.messages if m["role"] != "system"]
        transcript = "\n".join(f"[{m['role']}] {m['content']}" for m in turns)

        # Vobiz's recording has both sides; ours has only the caller, with
        # silence wherever the agent spoke. Prefer theirs, keep ours as the
        # fallback so a call is never left with no audio at all.
        recording_format = "wav"
        recording_b64 = None
        if self._campaign and not self._campaign.get("script_test"):
            fetched = await self._fetch_vobiz_recording()
            if fetched:
                recording_b64, recording_format = fetched
        if recording_b64 is None:
            recording_b64 = self._encode_recording_b64()

        if self._campaign:
            await self._report_campaign_call(duration, transcript, recording_b64,
                                             recording_format)
            return

        summary = None
        caller_name = None
        captured_data = None
        if transcript.strip():
            try:
                raw = await asyncio.to_thread(
                    llm_reply,
                    [
                        {"role": "system", "content": (
                            "Read this phone call transcript. Reply with ONLY a single "
                            "JSON object, no markdown, no code fences, no extra text, "
                            "with exactly these keys:\n"
                            '  "caller_name": the caller\'s name if they stated one, else null\n'
                            '  "summary": a summary for a business dashboard, in English, '
                            "at most four sentences -- what the caller wanted, any details "
                            "they gave, and the outcome. If they placed an order or made a "
                            "complaint, say so explicitly.\n"
                            '  "subscription_plan": one of "print 6 months", "print 1 year", '
                            '"digital 1 month", "digital 1 year" if the caller confirmed an '
                            "order for one of these EXACT plans, else null\n"
                            '  "house_number", "street_name", "city", "district", "pincode": '
                            "the caller's delivery address fields if they gave a print order "
                            "or address-change address (exactly as they said each one), else null\n"
                            '  "payment_issue": true if they reported a payment made but not '
                            "confirmed, else false\n"
                            '  "delivery_complaint": true if they reported this month\'s '
                            "magazine not arriving, else false\n"
                            '  "address_change": true if an existing subscriber asked to '
                            "change their address, else false\n"
                            '  "interested_in_magazine": true if the caller asked about the '
                            "magazine, its plans or prices, or said they want it -- even if "
                            "they gave no address -- else false"
                        )},
                        {"role": "user", "content": transcript[:12000]},
                    ],
                )
                parsed = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
                caller_name = parsed.get("caller_name") or None
                summary = parsed.get("summary") or None

                plan = parsed.get("subscription_plan") or None
                has_address = any(parsed.get(k) for k in ("house_number", "street_name", "city", "district", "pincode"))
                nodes_visited = []
                if has_address:
                    nodes_visited.append("Address Confirmation" if plan else "Print Copy & Address Collection")
                if plan and plan.startswith("digital"):
                    nodes_visited.append("Digital Subscription")
                if parsed.get("payment_issue"):
                    nodes_visited.append("Payment Issue")
                if parsed.get("delivery_complaint"):
                    nodes_visited.append("Delivery Complaint")
                if parsed.get("address_change"):
                    nodes_visited.append("Address Change")

                # Shaped to match what the existing "Gundusoodhi - post-call
                # WhatsApp" n8n workflow already expects (it used to read this
                # same shape from Dograh's own database) -- reusing its
                # message-building logic as-is, only the data source changes.
                captured_data = {
                    "subscription_plan": plan,
                    "extracted_variables": {
                        "customer_name": caller_name,
                        "house_number": parsed.get("house_number"),
                        "street_name": parsed.get("street_name"),
                        "city": parsed.get("city"),
                        "district": parsed.get("district"),
                        "pincode": parsed.get("pincode"),
                    },
                    "nodes_visited": nodes_visited,
                    # Asked about the magazine or its prices without ordering:
                    # post-call-whatsapp sends these callers the plans offer.
                    "interested_in_magazine": bool(parsed.get("interested_in_magazine")),
                }
            except Exception:
                log.exception("call summary/extraction failed -- reporting without one")
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
                recording_base64=recording_b64,
                captured_data=captured_data,
            )
        except Exception:
            log.exception("call report failed")

    async def _report_campaign_call(self, duration: float, transcript: str,
                                     recording_b64: str | None = None,
                                     recording_format: str = "wav") -> None:
        """Campaign calls report to campaign-outcome, not voicezen -- the
        customer is already known directly (campaigns.customer_id), so this
        skips the dialled-number lookup entirely rather than trying to force
        a campaign call through a path built for a client's own number."""
        if self._campaign.get("script_test"):
            log.info("script test call finished (%.0fs) -- not logged or billed", duration)
            return
        if self._campaign.get("is_test"):
            await self._report_test_call(duration, transcript)
            return
        outcome = None
        summary = None
        address = None
        if transcript.strip():
            try:
                raw = await asyncio.to_thread(
                    llm_reply,
                    [
                        {"role": "system", "content": (
                            "Read this outbound sales call transcript. Reply "
                            "with exactly three lines, nothing else:\n"
                            "Line 1: a short outcome tag -- one of: interested, "
                            "maybe, not_interested, callback_requested, "
                            "no_answer, wrong_number, do_not_call, unclear\n"
                            "  interested = they clearly want this or asked "
                            "how to proceed/sign up\n"
                            "  maybe = genuine but hedged interest -- they "
                            "asked real questions, didn't reject it, but "
                            "didn't commit either (\"maybe later\", \"let me "
                            "think\", \"send me details\"); this is a 50/50, "
                            "worth a follow-up, not a firm yes\n"
                            "  unclear = the call itself gave too little to "
                            "judge interest at all (cut short, garbled, "
                            "barely any conversation) -- NOT the same as "
                            "maybe, which requires an actual hedged response\n"
                            "Line 2: the caller's full delivery address if they "
                            "gave one during the call (combine all the parts "
                            "into one line), otherwise the literal word none\n"
                            "Line 3: a one-to-two sentence note for a sales "
                            "dashboard on what happened and any next step."
                        )},
                        {"role": "user", "content": transcript[:12000]},
                    ],
                )
                lines = raw.split("\n", 2)
                outcome = lines[0].strip() or None
                if len(lines) > 1 and lines[1].strip().lower() != "none":
                    address = lines[1].strip()
                summary = lines[2].strip() if len(lines) > 2 else None
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
                        "address": address,
                        "language": self._campaign.get("language"),
                        "recording_base64": recording_b64,
                        "recording_format": recording_format,
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

    async def _report_test_call(self, duration: float, transcript: str) -> None:
        """In-browser test calls: just one call_logs row, billed the same as
        any real call (no discount). No campaign_contacts row exists for
        these (there's no real contact, just whoever is testing), so this
        skips straight to report-test-call rather than campaign-outcome."""
        if not REPORT_TEST_CALL_URL or not VOICE_AGENT_SECRET:
            log.error("test call finished but REPORT_TEST_CALL_URL/secret "
                      "not configured -- this test call's minutes are lost")
            return
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    REPORT_TEST_CALL_URL,
                    headers={
                        "Content-Type": "application/json",
                        "x-agent-secret": VOICE_AGENT_SECRET,
                        "apikey": VOICEZEN_ANON_KEY,
                        "Authorization": f"Bearer {VOICEZEN_ANON_KEY}",
                    },
                    json={
                        "customer_id": self._campaign.get("customer_id"),
                        "call_duration": duration,
                        "transcript": transcript or None,
                        "language": self._campaign.get("language"),
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        log.error("report-test-call failed: HTTP %s %s", r.status, await r.text())
                    else:
                        log.info("test call reported: customer=%s duration=%.0fs",
                                  self._campaign.get("customer_id"), duration)
        except Exception:
            log.exception("report-test-call request failed")


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


@app.websocket("/test-media")
async def test_media(ws: WebSocket):
    """In-browser test calls -- a browser microphone in place of a real
    Vobiz phone call. Speaks the IDENTICAL JSON protocol as /media (start /
    media / playedStream / clearedAudio events, playAudio/checkpoint sent
    back) so CallSession's turn-taking, barge-in and TTS logic all work
    completely unchanged; the only new part is the auth handshake up front,
    since a browser has no shared secret to present the way server-to-server
    calls (e.g. /dial) do.
    """
    await ws.accept()

    try:
        raw = await ws.receive_text()
        token = json.loads(raw).get("token")
    except Exception:
        await ws.close(code=4000)
        return
    if not token or not VALIDATE_TEST_SESSION_URL or not VOICE_AGENT_SECRET:
        await ws.send_text(json.dumps({"event": "error", "message": "test calling is not configured"}))
        await ws.close(code=4000)
        return

    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(
                VALIDATE_TEST_SESSION_URL,
                headers={"Content-Type": "application/json", "x-agent-secret": VOICE_AGENT_SECRET,
                         "apikey": VOICEZEN_ANON_KEY, "Authorization": f"Bearer {VOICEZEN_ANON_KEY}"},
                json={"token": token},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    await ws.send_text(json.dumps({"event": "error", "message": "invalid or expired test session"}))
                    await ws.close(code=4001)
                    return
                validated = await r.json()
        except Exception:
            log.exception("validate-test-session request failed")
            await ws.send_text(json.dumps({"event": "error", "message": "could not start test call"}))
            await ws.close(code=4000)
            return

    call_uuid = str(uuid.uuid4())
    campaign = dict(validated["campaign"])
    campaign["campaign_id"] = None
    campaign["contact_id"] = None
    campaign["customer_id"] = validated["customer_id"]
    campaign["is_test"] = True
    pending_call_info[call_uuid] = {"campaign": campaign, "direction": "outbound", "from": "test", "to": "test"}

    await ws.send_text(json.dumps({"event": "authed", "callId": call_uuid}))

    session = CallSession(ws)
    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            event = data.get("event")

            if event == "start":
                session.stream_id = data["start"]["streamId"]
                session._call_uuid = data["start"].get("callId")
                log.info("test call stream started: call=%s", session._call_uuid)
                await session.start()

            elif event == "media":
                await session.on_media(data["media"]["payload"])

            elif event == "playedStream":
                session._on_played(data.get("name"))

            elif event == "clearedAudio":
                log.info("test call barge-in: playback queue flushed")

    except WebSocketDisconnect:
        log.info("test call websocket closed")
    finally:
        await session.close()


@app.get("/health")
async def health():
    return {"status": "ok"}
