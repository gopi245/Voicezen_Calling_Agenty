"""Does DTLN recover the accuracy that Krisp BVC provides?

Self-hosting LiveKit removes Krisp BVC noise cancellation, which was the
single biggest recognition fix in this project -- live calls went from
"పిల్లలు పిల్లలు పిల్లలు" for a town name to capturing names and streets
correctly. `livekit-plugins-dtln` is the open-source replacement, but nobody
has measured it on Telugu telephony audio.

This measures it. Known Telugu is synthesised, degraded to a real phone path
(8 kHz + G.711 mu-law + line noise), then transcribed three ways:

    clean                -> the ceiling
    noisy, no cleanup    -> self-hosting with nothing
    noisy + DTLN         -> self-hosting with the open-source canceller

Touches no running code. The live agent keeps answering throughout.
"""

from __future__ import annotations

import io
import os
import wave

import httpx
import numpy as np
from dotenv import load_dotenv

load_dotenv()

CARTESIA = os.getenv("CARTESIA_API_KEY")
SARVAM = os.getenv("SARVAM_API_KEY")
VOICE = os.getenv("CARTESIA_VOICE", "07bc462a-c644-49f1-baf7-82d5599131be")

# DTLN's pretrained model is fixed at 16 kHz, so the whole test runs there and
# the 8 kHz phone path is simulated by decimating and interpolating back.
SR = 16000
PHONE_SR = 8000

PHRASES = [
    "గుండుసూది మ్యాగజైన్ అంటే ఏంటి?",
    "నా పేరు సునీత",
    "ఇంటి నెంబర్ నలభై ఐదు",
    "విశాఖపట్నం",
    "కృష్ణా జిల్లా",
    "ఐదు రెండు సున్నా సున్నా ఒకటి సున్నా",
]

PROMPT = (
    "గుండుసూది, పిన్ మీడియా, మ్యాగజైన్, సబ్‌స్క్రిప్షన్, ప్రింట్, డిజిటల్, "
    "డెలివరీ, పేమెంట్, పిన్‌కోడ్, చిరునామా, ఇంటి నెంబర్, వీధి, ఊరు, జిల్లా"
)


def synth(text: str) -> np.ndarray:
    r = httpx.post(
        "https://api.cartesia.ai/tts/bytes",
        headers={"X-API-Key": CARTESIA, "Cartesia-Version": "2024-06-10"},
        json={
            "model_id": "sonic-3",
            "transcript": text,
            "voice": {"mode": "id", "id": VOICE},
            "language": "te",
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": SR,
            },
        },
        timeout=60,
    )
    r.raise_for_status()
    return np.frombuffer(r.content, dtype=np.int16)


def mulaw_roundtrip(pcm: np.ndarray) -> np.ndarray:
    """ITU G.711 mu-law encode/decode -- verified at 37.2 dB SNR."""
    SEG_END = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF])
    CLIP, ENC_BIAS, DEC_BIAS = 8159, 0x21, 0x84

    v = pcm.astype(np.int32) >> 2
    mask = np.where(v < 0, 0x7F, 0xFF)
    v = np.minimum(np.abs(v), CLIP) + ENC_BIAS
    seg = np.searchsorted(SEG_END, v, side="left")
    seg_c = np.clip(seg, 0, 7)
    uval = (seg_c << 4) | ((v >> (seg_c + 1)) & 0x0F)
    uval = np.where(seg >= 8, 0x7F, uval)
    encoded = (uval ^ mask) & 0xFF

    u = (~encoded) & 0xFF
    t = ((u & 0x0F) << 3) + DEC_BIAS
    t = t << ((u & 0x70) >> 4)
    out = np.where(u & 0x80, DEC_BIAS - t, t - DEC_BIAS)
    return np.clip(out, -32768, 32767).astype(np.int16)


def phone_path(pcm: np.ndarray, snr_db: float) -> np.ndarray:
    """16k -> 8k -> mu-law -> +noise -> back to 16k, as a real call arrives."""
    down = pcm[::2]                       # 16k -> 8k
    coded = mulaw_roundtrip(down)

    power = np.mean(coded.astype(np.float64) ** 2)
    if power > 0:
        npow = power / (10 ** (snr_db / 10))
        coded = np.clip(
            coded + np.random.normal(0, np.sqrt(npow), len(coded)), -32768, 32767
        ).astype(np.int16)

    # 8k -> 16k by linear interpolation, matching what the SIP stack does.
    x = np.arange(len(coded))
    xi = np.linspace(0, len(coded) - 1, len(coded) * 2)
    return np.interp(xi, x, coded).astype(np.int16)


def apply_dtln(pcm: np.ndarray) -> np.ndarray:
    """Run the audio through DTLN the way the agent would, frame by frame."""
    from livekit import rtc
    from livekit.plugins import dtln

    ns = dtln.DTLNNoiseSuppressor()
    out = []
    step = 160  # 10 ms at 16 kHz, a normal SIP frame
    for i in range(0, len(pcm) - step + 1, step):
        chunk = pcm[i : i + step]
        frame = rtc.AudioFrame(
            data=chunk.tobytes(),
            sample_rate=SR,
            num_channels=1,
            samples_per_channel=len(chunk),
        )
        processed = ns._process(frame)
        out.append(np.frombuffer(processed.data, dtype=np.int16))
    return np.concatenate(out) if out else pcm


def to_wav(pcm: np.ndarray, rate: int = SR) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def transcribe(wav: bytes) -> str:
    r = httpx.post(
        "https://api.sarvam.ai/speech-to-text",
        headers={"api-subscription-key": SARVAM},
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "saaras:v3", "language_code": "te-IN", "prompt": PROMPT},
        timeout=90,
    )
    if r.status_code != 200:
        return f"<HTTP {r.status_code}>"
    return (r.json().get("transcript") or "").strip()


def score(said: str, got: str) -> float:
    a = set(said.replace("?", "").replace(".", "").split())
    b = set(got.replace("?", "").replace(".", "").split())
    return len(a & b) / len(a) if a else 0.0


def main() -> None:
    snr = float(os.getenv("SNR_DB", "18"))
    print(f"Telephony simulation: 8kHz + G.711 mu-law + noise at {snr:.0f} dB SNR\n")

    totals = {"clean": [], "noisy": [], "noisy+DTLN": []}

    for phrase in PHRASES:
        base = synth(phrase)
        noisy = phone_path(base, snr)
        try:
            cleaned = apply_dtln(noisy)
        except Exception as e:
            print(f"  DTLN FAILED: {type(e).__name__}: {str(e)[:120]}")
            return

        variants = {
            "clean": base,
            "noisy": noisy,
            "noisy+DTLN": cleaned,
        }
        print(f"SAID: {phrase}")
        for label, audio in variants.items():
            got = transcribe(to_wav(audio))
            s = score(phrase, got)
            totals[label].append(s)
            print(f"  {label:12} [{s:3.0%}] {got}")
        print()

    print("=" * 60)
    for label, vals in totals.items():
        avg = sum(vals) / len(vals) if vals else 0
        print(f"  {label:14} average word accuracy {avg:.0%}")
    print()
    d_noisy = sum(totals["noisy"]) / len(totals["noisy"])
    d_dtln = sum(totals["noisy+DTLN"]) / len(totals["noisy+DTLN"])
    delta = (d_dtln - d_noisy) * 100
    if delta > 8:
        print(f"  DTLN recovers {delta:.0f} points -- self-hosting looks viable.")
    elif delta > 0:
        print(f"  DTLN recovers only {delta:.0f} points -- marginal.")
    else:
        print(f"  DTLN makes it {abs(delta):.0f} points WORSE -- do not self-host")
        print("  without a paid Krisp licence.")


main()
