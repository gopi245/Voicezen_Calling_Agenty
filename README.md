# Vobiz AI Voice Calling Agent

Inbound + outbound AI phone agent on **+91 80715 80257**, built on LiveKit
Agents with Sarvam (STT/TTS) and Gemini 3.5 Flash-Lite (LLM).

```
PSTN ── Vobiz SIP trunk ── LiveKit SIP ── LiveKit room ── agent.py
                                                            ├── Sarvam saaras:v3  (STT)
                                                            ├── Gemini 3.5 Flash-Lite
                                                            └── Sarvam bulbul:v3   (TTS)
```

## Why this stack

Vobiz publishes a first-party LiveKit integration, and LiveKit is the only
open-source voice framework with **native SIP** — trunk credentials go straight
in, no media gateway of your own. Same worker serves both directions.

For an Indian number specifically:

- **Sarvam STT/TTS** — built for Indian languages and Indian-accented English,
  with natural Indian voices. An American-accented TTS on an Indian support
  line is immediately jarring to callers. Language is set to auto-detect, so
  Hindi/English code-switching mid-sentence works.
- **Gemini 3.5 Flash-Lite** — ~350 output tokens/sec, Google's lowest-latency
  model, and the LLM is the largest remaining slice of the turn budget.

## Latency — measured on a real call

Target was sub-500ms. Here is what the first live call actually produced,
from the worker's own metrics:

| Stage | Predicted | **Measured** |
|---|---|---|
| Endpointing | 70 ms | **70–80 ms** ✅ |
| LLM time-to-first-token | 150–300 ms | **690–910 ms** ❌ |
| TTS time-to-first-byte | 80–150 ms | **380–1110 ms** ❌ |
| Agent's spoken reply length | — | **up to 8 s** ❌ |

The endpointing design worked exactly as intended. Everything else did not,
and the cause is not the models — it is **where the worker runs**.

Raw TCP connect time from a laptop on Indian broadband:

| Host | 1 RTT |
|---|---|
| `generativelanguage.googleapis.com` | 220 ms |
| `api.groq.com` | 217 ms |
| `api.sarvam.ai` | 152 ms |

Every LLM and TTS call pays that. Gemini's TTFT is ~0.85s of which ~0.22s is
pure network — and disabling thinking changes nothing (measured: `low`,
`minimal` and default are all ~0.85s). The model is not the bottleneck.

**Sub-500ms is not reachable from a laptop.** It needs the worker on a cloud
VM in Mumbai or Bangalore, where RTT to these APIs is 5–30ms instead of
220ms. That single change removes ~400ms per turn and is worth more than every
other optimisation combined. Until then, expect 1.5–2s.

### The dominant term is reply length, not pipeline latency

Second call, measured per turn:

| turn | → first word | agent speaks | total caller experiences |
|---|---|---|---|
| 1 | 1.27 s | 2.69 s | **3.96 s** |
| 2 | 1.66 s | 3.03 s | **4.69 s** |
| 3 | 1.30 s | 7.97 s | **9.27 s** |

Time-to-first-word is ~1.3s. Everything after that is the agent talking. A
33-token reply became 8 seconds of audio, so reply length outweighed the
entire STT→LLM→TTS chain by roughly 6x.

This is the counter-intuitive part of voice agents: **the cheapest latency win
is not a faster model, it is a shorter answer.** A polite "keep replies to one
or two sentences" in the prompt does not hold — it needs a hard word limit in
the prompt *and* a `max_output_tokens` ceiling backing it up.

### Why sub-500ms was dropped as a target

Speech-to-speech (Gemini Live) was built and tested — it is the only way past
the cascaded pipeline's ~1s floor, because it replaces three sequential
network hops with one. It was **rejected on cost**: realtime and native-audio
models bill audio tokens and cost far more per minute than Flash-Lite driving
a cascaded pipeline. There is no Flash-Lite *Live* model, so cheap and
speech-to-speech are mutually exclusive.

The code is still there — `PIPELINE=realtime` in `.env` switches to it, and
`build_realtime()` in `stack.py` holds the config — but `cascaded` is the
default and the supported path.

**The realistic target for this architecture is ~800ms**, reached by moving
the worker to a Mumbai or Bangalore VM (removes ~220ms RTT per API hop). From
a laptop, expect ~1.3s to first word. 500ms is not achievable on a cascaded
pipeline at Flash-Lite prices, and it is more honest to design the call flow
around ~1s than to keep chasing it.

### What was tuned after the first call

- **Reply length capped** — `max_output_tokens=120` plus a stricter brevity
  rule in the prompt. A 28-token reply became 8 seconds of audio the caller
  had to sit through; long answers *feel* slow even when they start fast. This
  was a large part of the perceived 3–5s.
- **TTS buffer cut** — `min_buffer_size` 50 → 10, so the first audio chunk
  ships sooner. Raise it back if prosody sounds choppy.
- **`LLM_PROVIDER=sarvam` is now the faster option while running in India**
  (measured 0.61–0.77s TTFT vs Gemini's 0.80–0.90s) purely because it is
  India-hosted. On a Mumbai VM this ordering may well flip — re-measure there.

`agent.py` logs `end_of_utterance_delay`, `llm_ttft` and `tts_ttfb` per turn.
Tune against those, not against any table.

**Swapping providers** — all from `.env`, no code change:
`LLM_PROVIDER=groq` (lowest TTFT available to you, worth trying if Gemini is
your bottleneck), `LLM_PROVIDER=sarvam` (Indic-native, better Hindi nuance,
slower), `STT_PROVIDER=deepgram` (falls back to semantic turn detection and a
0.35s delay automatically — the code handles the switch).

## Status — provisioned and verified

| Resource | Value |
|---|---|
| Vobiz trunk | `8a94fdf6-a2d1-46cf-9181-39757020b94f` (`8a94fdf6.sip.vobiz.ai`, both directions, active) |
| SIP credential | `lk-agent-301910` — generated, linked to trunk |
| Number | `+918071580257` assigned to the trunk |
| LiveKit outbound trunk | `ST_3dtXky2sL57Z` |
| LiveKit inbound trunk | `ST_QxaRVkb6eZmf` |
| Dispatch rule | `SDR_fmFuDXBTCJNP` → agent `voice-assistant` |
| Worker region | **India South** — co-located with the number |
| Keys verified | Gemini, Sarvam, Groq, Deepgram all return 200 |

Everything is wired. Nothing is left to configure.

## Setup (already done — for rebuilding from scratch)

```bash
pip install -r requirements.txt
python provision.py trunk        # → VOBIZ_TRUNK_ID, VOBIZ_SIP_DOMAIN
python provision.py credential   # → VOBIZ_SIP_USERNAME / _PASSWORD, links to trunk
python provision.py number       # routes the PSTN number through the trunk
python provision.py outbound     # → LIVEKIT_OUTBOUND_TRUNK_ID
python provision.py inbound sip:3ublsxh8257.sip.livekit.cloud
```

Each step prints values that go into `.env`. Order matters — `credential`,
`number` and `inbound` all need `VOBIZ_TRUNK_ID` from step one.

### Run

```bash
python agent.py dev
```

Call +91 80715 80257, or place an outbound call:

```bash
python dial.py +919876543210 "Confirm their appointment on Thursday"
```

## Files

| File | Purpose |
|---|---|
| `agent.py` | The worker. Inbound/outbound agents, tools, turn-detection tuning. |
| `stack.py` | STT/LLM/TTS provider selection, all env-driven. |
| `provision.py` | Vobiz ↔ LiveKit wiring. |
| `dial.py` | Places an outbound call. |

## Before going live

- **Change your Vobiz account password.** It was pasted into a chat transcript.
  It was not needed and was not used — the SIP credential was created through
  the API instead — but it is exposed regardless, and an account password is a
  bigger loss than an API key because it can be used to rotate everything else.
- **Rotate the API keys too.** Vobiz auth token, LiveKit secret,
  Gemini/Sarvam/Groq/Deepgram keys. `.env` is gitignored, but that does not
  undo the exposure. Re-run the `provision.py` steps after rotating the Vobiz
  token; the LiveKit trunk IDs survive a key rotation.
- Narrow `allowed_addresses` on the LiveKit inbound trunk from `0.0.0.0/0` to
  Vobiz's signalling IPs.
- Raise `max_concurrent_calls` (currently 10) and `cps_limit` on the Vobiz
  trunk to match expected volume, or calls will drop under load.
- Recording, transcription and PII redaction are trunk-level Vobiz flags, off
  by default here. Indian call-recording consent rules apply if you enable them.
- `AGENT_NAME` must match across `agent.py`, `dial.py` and the dispatch rule,
  or nobody answers.

## Sources

- [Vobiz LiveKit integration](https://vobiz.ai/docs/integrations/livekit)
- [Vobiz create-trunk API](https://vobiz.ai/docs/trunks/create-trunk)
- [Sarvam LiveKit voice agent guide](https://docs.sarvam.ai/api/integration/build-voice-agent-with-live-kit)
- [Sarvam TTS plugin reference](https://docs.livekit.io/agents/models/tts/plugins/sarvam/)
- [Gemini 3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
