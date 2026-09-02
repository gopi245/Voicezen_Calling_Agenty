"""Compare Sarvam STT models under realistic phone-line degradation.

An earlier run on CLEAN 8kHz audio scored 84% for every model, which wrongly
suggested the recogniser was fine. Real calls arrive as 8kHz G.711 mu-law with
line noise, and that is where the transcripts fell apart -- a town name came
back as "పిల్లలు పిల్లలు పిల్లలు" and a pause became an invented sentence.

This reproduces that degradation so the models can be compared on the audio
they actually get.
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


def synth(text: str, rate: int = 8000) -> np.ndarray:
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
                "sample_rate": rate,
            },
        },
        timeout=60,
    )
    r.raise_for_status()
    return np.frombuffer(r.content, dtype=np.int16)


def mulaw_roundtrip(pcm: np.ndarray) -> np.ndarray:
    """G.711 mu-law encode/decode -- the codec every PSTN call goes through.

    audioop was removed in Python 3.13, so this is the standard algorithm
    written out directly.
    """
    # The reference ITU G.711 algorithm. Two details are easy to get wrong and
    # both silently produce ~0 dB noise that still "looks like audio":
    # the encoder works on pcm >> 2 (so CLIP is 8159 and BIAS is 0x21, not
    # 0x84), and the segment boundaries come from a lookup table rather than a
    # bit-position calculation.
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


def add_noise(pcm: np.ndarray, snr_db: float) -> np.ndarray:
    sig_power = np.mean(pcm.astype(np.float64) ** 2)
    if sig_power == 0:
        return pcm
    noise_power = sig_power / (10 ** (snr_db / 10))
    noise = np.random.normal(0, np.sqrt(noise_power), len(pcm))
    return np.clip(pcm + noise, -32768, 32767).astype(np.int16)


def to_wav(pcm: np.ndarray, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def transcribe(wav: bytes, model: str, use_prompt: bool) -> str:
    data = {"model": model, "language_code": "te-IN"}
    if use_prompt:
        data["prompt"] = PROMPT
    r = httpx.post(
        "https://api.sarvam.ai/speech-to-text",
        headers={"api-subscription-key": SARVAM},
        files={"file": ("a.wav", wav, "audio/wav")},
        data=data,
        timeout=90,
    )
    if r.status_code != 200:
        return f"<HTTP {r.status_code}>"
    return (r.json().get("transcript") or "").strip()


def score(said: str, got: str) -> float:
    a = set(said.replace("?", "").replace(".", "").split())
    b = set(got.replace("?", "").replace(".", "").split())
    return len(a & b) / len(a) if a else 0.0


CONFIGS = [
    ("saarika:v2.5", "saarika:v2.5", False),
    ("saaras:v3", "saaras:v3", False),
    ("saaras:v3 +prompt", "saaras:v3", True),
]

CONDITIONS = [
    ("clean 8k", lambda p: p),
    ("8k + mulaw", lambda p: mulaw_roundtrip(p)),
    ("8k + mulaw + noise", lambda p: mulaw_roundtrip(add_noise(p, 18))),
]


def main() -> None:
    totals: dict[tuple[str, str], list[float]] = {}
    for cond_name, degrade in CONDITIONS:
        print(f"\n### {cond_name}")
        for phrase in PHRASES:
            pcm = degrade(synth(phrase))
            wav = to_wav(pcm)
            line = f"  {phrase[:34]:36}"
            for label, model, up in CONFIGS:
                got = transcribe(wav, model, up)
                s = score(phrase, got)
                totals.setdefault((cond_name, label), []).append(s)
                line += f" {label.split()[0][:8]}={s:3.0%}"
            print(line)

    print("\n" + "=" * 70)
    print(f"{'condition':22} " + " ".join(f"{c[0]:>18}" for c in CONFIGS))
    for cond_name, _ in CONDITIONS:
        row = f"{cond_name:22} "
        for label, _, _ in CONFIGS:
            v = totals.get((cond_name, label), [])
            row += f" {sum(v)/len(v) if v else 0:17.0%}"
        print(row)


main()
