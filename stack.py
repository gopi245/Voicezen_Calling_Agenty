"""STT / LLM / TTS selection for the voice pipeline.

Every provider here is swappable from .env with no code change, because the
right combination is something you find by measuring your own calls, not by
reading benchmarks.

Defaults are tuned for an Indian number (+91) on a Vobiz trunk:

  STT  Sarvam saaras:v3  -- Indian languages and Indian-accented English, and
                            its `flush_signal` speech-end events are what let
                            the endpointing delay drop to 70ms (see agent.py).
  LLM  Gemini 3.5 Flash-Lite -- ~350 output tokens/sec, Google's lowest-latency
                            model; the LLM is the largest remaining slice of
                            the turn budget.
  TTS  Sarvam bulbul:v3  -- natural Indian voices; an American-accented TTS on
                            an Indian support line is jarring to callers.
"""

from __future__ import annotations

import os

from livekit.plugins import deepgram, google, groq, sarvam


def build_stt():
    provider = os.getenv("STT_PROVIDER", "sarvam")

    if provider == "deepgram":
        # Fallback: strong general model, but weaker on Indian-accented
        # English than Saaras and it gives up the flush-signal endpointing.
        return deepgram.STT(model="nova-3", language="multi", interim_results=True)

    # Domain vocabulary to bias recognition. On a live call the recogniser
    # heard the brand name "గుండుసూది" as "ఉల్సూరి" and the agent then
    # answered the nonsense sensibly -- the failure was recognition, not
    # reasoning. A controlled test on clean 8kHz audio scored 84% across every
    # Sarvam model, so the model choice was never the issue; proper nouns over
    # a real phone line were. saaras models accept this prompt; saarika
    # ignores it.
    prompt = (
        "గుండుసూది, పిన్ మీడియా, మ్యాగజైన్, సబ్‌స్క్రిప్షన్, ప్రింట్, "
        "డిజిటల్, ఎడిషన్, డెలివరీ, పేమెంట్, వాట్సాప్, పిన్‌కోడ్, చిరునామా, "
        "ఇంటి నెంబర్, వీధి, ఊరు, జిల్లా, రూపాయలు, ఆరు నెలలు, ఏడాది"
    )

    return sarvam.STT(
        model=os.getenv("SARVAM_STT_MODEL", "saaras:v3"),
        prompt=prompt,
        # "unknown" auto-detects, which matters on a line that gets a mix of
        # English, Hindi and code-switching between them mid-sentence.
        language=os.getenv("SARVAM_STT_LANGUAGE", "unknown"),
        mode="transcribe",
        # Emits speech start/end events from Sarvam's internal VAD, which is
        # what makes turn_detection="stt" viable.
        flush_signal=True,
        # Require more confidence before treating audio as speech. The
        # recogniser was transcribing silence and line noise into invented
        # Telugu sentences, which the agent then dutifully recorded as a
        # caller's name. Raising the bar trades a little sensitivity for not
        # hallucinating. Tunable without a rebuild if it starts missing
        # genuinely quiet callers.
        positive_speech_threshold=float(os.getenv("STT_SPEECH_THRESHOLD", "0.6")),
        min_speech_frames=int(os.getenv("STT_MIN_SPEECH_FRAMES", "5")),
    )


def build_llm():
    """Primary LLM, wrapped in a fallback chain unless disabled.

    A live call died with Gemini returning 504 DEADLINE_EXCEEDED in a burst.
    The plugin retried, the retries took seconds, and the caller heard dead
    air and hung up -- a provider blip becomes a failed call. Measured on a
    healthy run the same path is 8/8 successful at ~0.99s, so this is about
    surviving the bad minutes, not the normal ones.

    Groq is second because it is a different provider on different
    infrastructure; a Google outage does not take it out too.
    """
    primary = _build_primary_llm()
    if os.getenv("LLM_FALLBACK", "1") != "1":
        return primary

    # The backup must be a DIFFERENT provider -- a chain of two Gemini
    # clients does nothing when Gemini is the thing that is down.
    if os.getenv("LLM_PROVIDER", "google") == "groq":
        backup = google.LLM(
            model=os.getenv("LLM_MODEL", "gemini-3.5-flash-lite"),
            max_output_tokens=int(os.getenv("LLM_MAX_TOKENS", "60")),
        )
    else:
        backup = groq.LLM(model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"))

    from livekit.agents import llm as agents_llm

    return agents_llm.FallbackAdapter(
        [primary, backup],
        # 10s is Gemini's MINIMUM allowed deadline -- anything lower is
        # rejected with "Manually set deadline Ns is too short", a 400 on
        # every single request, which silently kills the whole call. Do not
        # lower this to chase latency.
        attempt_timeout=float(os.getenv("LLM_ATTEMPT_TIMEOUT", "10.0")),
        max_retry_per_llm=1,
    )


def _build_primary_llm():
    provider = os.getenv("LLM_PROVIDER", "google")

    if provider == "groq":
        # Groq's inference is the lowest time-to-first-token available here.
        # Worth trying if Gemini's TTFT is your bottleneck in the metrics.
        return groq.LLM(model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"))

    if provider == "sarvam":
        # Indic-native, and India-hosted -- measured TTFT from Bangalore was
        # 0.61-0.77s vs 0.80-0.90s for Gemini, purely on shorter network
        # distance. Worth preferring while the worker runs in India.
        return sarvam.LLM(model=os.getenv("SARVAM_LLM_MODEL", "sarvam-105b"))

    return google.LLM(
        model=os.getenv("LLM_MODEL", "gemini-3.5-flash-lite"),
        # A hard ceiling on spoken length, and the most effective latency knob
        # in this file. Measured on a live call: time-to-first-word was 1.3s
        # but the agent then spoke for 8.0s, so the caller experienced ~9s.
        # Reply length dominates everything else by roughly 6x.
        # 33 tokens produced 8s of audio, so ~60 tokens is a hard 15s ceiling
        # while the prompt aims for one sentence of about four seconds.
        max_output_tokens=int(os.getenv("LLM_MAX_TOKENS", "60")),
        temperature=0.6,
    )


def build_realtime():
    """Speech-to-speech: audio in, audio out, one model, one network hop.

    The cascaded path pays a separate round-trip and a separate model's
    time-to-first-token at each of STT, LLM and TTS. Measured on this setup
    that was 0.07 + 0.85 + 0.45 = ~1.3s to first word, and no amount of tuning
    removes the fact that it is three sequential hops.

    Gemini Live collapses them into one, which is the only way a cascaded
    architecture's ~1s floor comes down to ~400ms.

    What you give up: Sarvam's Indian voices and Indic/code-switching
    strength, in exchange for Gemini's voice set. It is also preview-tier.
    """
    from livekit.plugins.google.beta import realtime
    from google.genai import types

    return realtime.RealtimeModel(
        model=os.getenv("REALTIME_MODEL", "gemini-3.1-flash-live-preview"),
        # Gemini's own voices -- Sarvam speakers do not apply here.
        # Options include Puck, Charon, Kore, Fenrir, Aoede.
        voice=os.getenv("REALTIME_VOICE", "Kore"),
        temperature=0.6,
        # Transcribe both sides so the call still shows up in the logs. Without
        # this, a speech-to-speech call is opaque -- no transcript to debug.
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                # HIGH end-sensitivity ends the turn sooner, which is the
                # equivalent of the 70ms endpointing in the cascaded path.
                # Drop to LOW if it starts interrupting callers mid-sentence.
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                silence_duration_ms=int(os.getenv("REALTIME_SILENCE_MS", "100")),
            )
        ),
    )


def build_tts():
    """TTS, with the other provider as a fallback.

    Same reasoning as the LLM chain: if Cartesia has a bad minute the caller
    gets silence, and Sarvam speaks the same Telugu. The voice changes
    mid-call in that case, which is much better than no voice.
    """
    primary = _build_primary_tts()

    # OFF by default. Cartesia emits 24kHz and Sarvam 22.05kHz, so wrapping
    # both in a FallbackAdapter forces a common rate and resamples every
    # utterance ("resampling sarvam.TTS from 22050Hz to 24000Hz") -- which
    # audibly degraded the voice on a real call. An LLM fallback is worth its
    # cost because the alternative is silence; a TTS fallback is not worth
    # damaging every call to insure against a rare outage.
    if os.getenv("TTS_FALLBACK", "0") != "1":
        return primary

    from livekit.agents import tts as agents_tts

    other = (
        _build_sarvam_tts()
        if os.getenv("TTS_PROVIDER", "sarvam") == "cartesia"
        else None
    )
    if other is None:
        return primary
    return agents_tts.FallbackAdapter([primary, other])


def _build_primary_tts():
    if os.getenv("TTS_PROVIDER", "sarvam") == "cartesia":
        from livekit.plugins import cartesia

        # sonic-3 is the ONLY Cartesia model that speaks Telugu. sonic-2,
        # sonic-turbo and sonic all reject language="te" with a 400, even
        # though the Telugu voices are listed under the account.
        return cartesia.TTS(
            model=os.getenv("CARTESIA_MODEL", "sonic-3"),
            language=os.getenv("CARTESIA_LANGUAGE", "te"),
            voice=os.getenv(
                # Sindhu -- Conversational Partner. Eight other Telugu voices
                # exist; see README for the id list.
                "CARTESIA_VOICE",
                "07bc462a-c644-49f1-baf7-82d5599131be",
            ),
            # sonic-3 requires a FLOAT between 0.6 and 2.0 -- passing the raw
            # env string raises "speed must be a float for sonic-3".
            # The scripted greeting measured 12.07s of audio at 1.0, which is
            # a long time to hold a caller before they can speak. 1.15 trims
            # it without sounding rushed, which the script explicitly forbids.
            speed=float(os.getenv("CARTESIA_SPEED", "1.15")),
            # Defaults to True, but Cartesia only supports word timestamps for
            # en/de/es/fr on sonic models -- leaving it on for Telugu logs a
            # warning on every single synthesis.
            word_timestamps=False,
        )

    return _build_sarvam_tts()


def _build_sarvam_tts():
    return sarvam.TTS(
        model=os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"),
        target_language_code=os.getenv("SARVAM_LANGUAGE", "en-IN"),
        speaker=os.getenv("SARVAM_SPEAKER", "ritu"),
        # Smaller buffer => the first audio chunk ships sooner. Measured TTFB
        # on a live call was 0.4-1.1s at the default 50. 30 is the floor the
        # plugin accepts (valid range 30-200; below that it raises and the job
        # crashes mid-call). Raise it back toward 50 if prosody sounds choppy.
        min_buffer_size=30,
        max_chunk_length=100,  # valid range 50-500
        # Quicker delivery. The caller sits through every word, so this
        # shortens the reply without cutting content. Above ~1.3 it starts to
        # sound rushed on a narrowband phone line.
        pace=float(os.getenv("SARVAM_PACE", "1.2")),
    )
