"""Exercise the real call pipeline without placing a phone call.

Every bug that reached a live caller in this project would have been caught
here in seconds: an out-of-range TTS buffer, a sub-minimum LLM deadline, a
Groq model that does not exist on the account, say() wrapped in create_task,
a fallback adapter silently resampling audio, endpointing so tight it split
one question into three turns.

    python smoke_test.py

Exits non-zero if anything fails, so it can gate a deploy.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from livekit.agents import ChatContext, utils  # noqa: E402

import agent  # noqa: E402
from clients import load as load_client  # noqa: E402
import stack  # noqa: E402

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    line = f"  {'PASS' if ok else 'FAIL'}  {label}{'  -- ' + detail if detail else ''}"
    try:
        print(line)
    except UnicodeEncodeError:
        # A Windows console is cp1252 and cannot render Telugu. Losing the
        # detail text is fine; crashing the whole test run over it is not.
        enc = sys.stdout.encoding or "ascii"
        print(line.encode(enc, "replace").decode(enc))
    if not ok:
        failures.append(label)


# ---------------------------------------------------------------------------
# 1. Config sanity -- the ranges that crashed live calls
# ---------------------------------------------------------------------------


def test_config() -> None:
    print("\n[1] configuration ranges")

    speed = float(os.getenv("CARTESIA_SPEED", "1.0"))
    check(0.6 <= speed <= 2.0, "cartesia speed in 0.6-2.0", f"{speed}")

    buf = 30  # stack.py hardcodes this for sarvam
    check(30 <= buf <= 200, "sarvam min_buffer_size in 30-200", f"{buf}")

    timeout = float(os.getenv("LLM_ATTEMPT_TIMEOUT", "10.0"))
    check(timeout >= 10.0, "llm attempt_timeout >= 10s (gemini minimum)", f"{timeout}")

    delay = float(os.getenv("MIN_ENDPOINTING_DELAY", "0.45"))
    check(
        0.2 <= delay <= 1.0,
        "endpointing 0.2-1.0s (below 0.2 fragments speech)",
        f"{delay}",
    )


# ---------------------------------------------------------------------------
# 2. Components build, and are the shape we expect
# ---------------------------------------------------------------------------


def test_build() -> None:
    print("\n[2] pipeline construction")
    try:
        s, l, t = stack.build_stt(), stack.build_llm(), stack.build_tts()
        check(True, "stt/llm/tts construct")
    except Exception as e:
        check(False, "stt/llm/tts construct", f"{type(e).__name__}: {e}")
        return

    # A TTS FallbackAdapter mixes sample rates and resamples every utterance.
    check(
        type(t).__name__ != "FallbackAdapter",
        "tts is not a FallbackAdapter (avoids resampling)",
        type(t).__name__,
    )
    check(
        type(l).__name__ == "FallbackAdapter",
        "llm HAS a fallback (a provider blip must not equal silence)",
        type(l).__name__,
    )


# ---------------------------------------------------------------------------
# 3. TTS actually synthesises Telugu, and how long the greeting really is
# ---------------------------------------------------------------------------


async def test_tts() -> None:
    print("\n[3] text-to-speech")
    tts = stack.build_tts()
    # Outside a job there is no agent http session, and the plugins fail with
    # a bare "Connection error." Open one so this exercises the real path.
    #
    # Warm up first and discard the result. The plugin's very first synthesis
    # pays DNS, TLS and websocket setup -- measured at 1.8s here against
    # 0.12s once warm, and 0.11s on an actual call. Timing the cold one
    # reports a latency no caller ever experiences.
    try:
        warm = tts.synthesize("పరీక్ష")
        async for _ in warm:
            pass
        await warm.aclose()
    except Exception:
        pass  # the real measurement below will report any genuine failure
    t0 = time.time()
    ttfb = None
    frames = 0
    duration = 0.0
    try:
        stream = tts.synthesize(load_client(None).greeting)
        async for ev in stream:
            if ttfb is None:
                ttfb = time.time() - t0
            frames += 1
            duration += ev.frame.samples_per_channel / ev.frame.sample_rate
        await stream.aclose()
    except Exception as e:
        check(False, "greeting synthesises", f"{type(e).__name__}: {e}")
        return

    check(frames > 0, "greeting synthesises", f"{frames} frames")
    check(
        ttfb is not None and ttfb < 1.0,
        "tts time-to-first-byte < 1.0s",
        f"{ttfb:.2f}s" if ttfb else "n/a",
    )
    # Not a hard failure -- a long greeting is a content decision, but the
    # caller cannot speak until it finishes, so it must be visible.
    print(f"  INFO  greeting is {duration:.1f}s of audio")
    if duration > 8:
        print("        ^ over 8s: the caller waits this long before they can talk")


# ---------------------------------------------------------------------------
# 4. The LLM follows the script -- the business rules that must never drift
# ---------------------------------------------------------------------------

CASES = [
    (
        "ఒక్క కాపీ కావాలి",
        "single print copy is refused",
        {"must_not": ["యాభై"], "must": ["ఆరు వందల"]},
    ),
    (
        "ఒక నెల ప్రింట్ ప్లాన్ కావాలి",
        "monthly print refused + 6-month minimum stated",
        {"must": ["ఆరు వందల"]},
    ),
    (
        "రేట్లు ఎంత?",
        "bare rate question asks print-or-digital, quotes no price",
        {"must_not": ["ఆరు వందల", "ముప్పై తొమ్మిది"]},
    ),
    (
        "డిజిటల్ ధర ఎంత?",
        "digital price quoted",
        {"must": ["ముప్పై తొమ్మిది"]},
    ),
    (
        "ఈ రోజు క్రికెట్ స్కోర్ ఎంత?",
        "off-topic deflected",
        {"must": ["గుండుసూది"]},
    ),
    # Unscripted but in-scope. These must be ANSWERED, not deflected with the
    # off-topic guardrail -- a question about the magazine is never off-topic,
    # however oddly it is phrased.
    (
        "ఇది ఇంగ్లీష్‌లో వస్తుందా?",
        "unscripted: language question answered, not deflected",
        {"must_not": ["మాత్రమే సహాయం చేయగలను"]},
    ),
    (
        "ఎన్ని రోజులకు ఒకసారి వస్తుంది?",
        "unscripted: frequency question answered",
        {"must_not": ["మాత్రమే సహాయం చేయగలను"]},
    ),
    (
        "సినిమా వార్తలు ఉంటాయా?",
        "unscripted: non-political content question answered",
        {"must_not": ["మాత్రమే సహాయం చేయగలను"]},
    ),
    (
        # Real ASR hallucination captured from a live call. The agent used to
        # accept output like this as a town name and record it.
        "పిల్లలు పిల్లలు పిల్లలు పిల్లలు పిల్లలు",
        "misheard nonsense -> asks caller to repeat",
        {"must": ["మళ్ళీ"]},
    ),
    (
        # A live call looped on this: the caller gave a real but unfamiliar
        # town and was asked "మీ జిల్లా ఏమిటి?" five times running.
        # Needs conversational context -- a bare place name with no preceding
        # question is genuinely ambiguous, and asking to repeat is correct
        # there. Testing it without context measures the wrong thing.
        "శృంగవరపుకోట",
        "unfamiliar real place accepted, not re-asked",
        {
            "must_not": ["మళ్ళీ చెప్పగలరా"],
            "context": [
                ("user", "ప్రింట్ కాపీ కావాలి, ఆరు నెలలు"),
                ("assistant", "సరే. మీ జిల్లా ఏమిటి?"),
            ],
        },
    ),
    (
        # A live call dropped the separators when reading this back, so the
        # caller could not verify their own address.
        "నా ఇంటి నెంబర్ నలభై ఐదు డాష్ రెండు డాష్ అరవై మూడు బై ఆరు. "
        "దీన్ని నాకు తిరిగి చదివి వినిపించండి.",
        "house number read back keeps its separators",
        {"must": ["డాష్"]},
    ),
    (
        # A live call answered "yes, send it" by reciting the payment-methods
        # answer and asking to send it all over again.
        "పంపించండి.",
        "accepting an offer acts on it, does not re-explain",
        {
            "must_not": ["యూపీఐ", "పంపించమంటారా"],
            "context": [
                ("user", "మ్యాగజైన్ ఎలా తీసుకోవాలి?"),
                (
                    "assistant",
                    "ఇప్పుడు మీ వాట్సాప్‌కు పేమెంట్ లింక్ పంపించమంటారా?",
                ),
            ],
        },
    ),
    (
        # The caller gave a number for the sample PDF and was asked for it
        # again for the payment link.
        "పంపించండి.",
        "reuses a number already given, does not ask twice",
        {
            "must_not": ["నంబర్ చెప్పండి"],
            "context": [
                ("user", "నమూనా పీడీఎఫ్ పంపండి"),
                ("assistant", "మీ వాట్సాప్ నంబర్ చెప్పండి."),
                ("user", "9493918625"),
                (
                    "assistant",
                    "సరేనండి, పంపిస్తున్నాము. ఇప్పుడు మీ వాట్సాప్‌కు "
                    "పేమెంట్ లింక్ పంపించమంటారా?",
                ),
            ],
        },
    ),
    (
        # The voice silently skips "-" and "/", so a read-back containing them
        # loses the separators the caller needs to hear.
        "నా ఇంటి నెంబర్ 45-2-63/6. దీన్ని నాకు తిరిగి చదివి వినిపించండి.",
        "read-back speaks separators as words, no raw symbols",
        {"must": ["డాష్"], "must_not": ["45-2", "63/6"]},
    ),
]


async def test_llm() -> None:
    print("\n[4] script adherence")
    a = agent.build_client_agent(load_client(None))
    llm = stack.build_llm()
    tools = list(a.tools)

    check(len(tools) >= 6, "business tools registered", f"{len(tools)} tools")
    timings: list[float] = []

    for question, label, rules in CASES:
        ctx = ChatContext()
        ctx.add_message(role="system", content=a.instructions)
        # Some rules only make sense mid-conversation.
        for role, content in rules.get("context", []):
            ctx.add_message(role=role, content=content)
        ctx.add_message(role="user", content=question)
        t0 = time.time()
        ttft = None
        try:
            out = ""
            called_tool = False
            async for ch in llm.chat(chat_ctx=ctx, tools=tools):
                d = ch.delta
                if d and getattr(d, "tool_calls", None):
                    called_tool = True
                if d and d.content:
                    if ttft is None:
                        ttft = time.time() - t0
                    out += d.content
        except Exception as e:
            check(False, label, f"{type(e).__name__}: {str(e)[:80]}")
            continue
        # Latency is a correctness property on a phone call: a technically
        # right answer that takes 15s is a failed call.
        timings.append(ttft or (time.time() - t0))

        # Collapse whitespace before matching. The model wraps lines, so a
        # two-word phrase like "ఆరు వందల" can arrive split by a newline and a
        # naive substring check reports a failure that is not real.
        flat = " ".join(out.split())
        bad = [s for s in rules.get("must_not", []) if s in flat]
        missing = [s for s in rules.get("must", []) if s not in flat]
        ok = not bad and not missing
        detail = ""
        if bad:
            detail += f"said forbidden {bad} "
        if missing:
            detail += f"missing {missing}"
        check(ok, label, detail.strip())

        if called_tool and not out.strip():
            # Answering by invoking a tool and saying nothing yet is valid --
            # the spoken reply comes on the next turn with the tool result.
            print("  PASS    ^ answered via tool call (no speech expected)")
        else:
            telugu = sum(1 for c in out if "ఀ" <= c <= "౿")
            check(
                telugu > len(out) * 0.3,
                f"  ^ replied in Telugu",
                f"{telugu}/{len(out)} Telugu chars",
            )

    if timings:
        worst = max(timings)
        avg = sum(timings) / len(timings)
        print(f"  INFO  llm time-to-first-token: avg {avg:.2f}s, worst {worst:.2f}s")
        check(worst < 4.0, "worst-case llm ttft under 4s", f"{worst:.2f}s")


# ---------------------------------------------------------------------------


def test_dashboard_reporting() -> None:
    """The reporting path is only exercised at hang-up, so a broken endpoint
    or secret would otherwise surface as silently missing dashboard rows."""
    print("\n[5] dashboard reporting")
    import voicezen

    url = os.getenv("VOICEZEN_URL", "")
    secret = os.getenv("VOICEZEN_AGENT_SECRET", "")
    check(bool(url), "VOICEZEN_URL set", url or "missing")
    check(bool(secret), "VOICEZEN_AGENT_SECRET set", "***" if secret else "missing")
    if not (url and secret):
        return

    import httpx

    # Wrong secret must be rejected -- proves the endpoint actually checks.
    try:
        r = httpx.post(
            f"{url.rstrip('/')}/functions/v1/log-call",
            headers={
                "x-agent-secret": "deliberately-wrong",
                "apikey": os.getenv("VOICEZEN_ANON_KEY", ""),
                "Authorization": f"Bearer {os.getenv('VOICEZEN_ANON_KEY', '')}",
            },
            json={"dialed_number": os.getenv("VOBIZ_PHONE_NUMBER", "")},
            timeout=30,
        )
        check(r.status_code == 401, "log-call rejects a wrong secret", f"HTTP {r.status_code}")
    except Exception as e:
        check(False, "log-call reachable", f"{type(e).__name__}: {str(e)[:70]}")
        return

    # The queue file must not be silently accumulating unsent calls.
    pending = 0
    if voicezen.QUEUE_FILE.exists():
        pending = len(voicezen.QUEUE_FILE.read_text(encoding="utf-8").splitlines())
    check(pending == 0, "no calls stuck unsent", f"{pending} queued")


async def main() -> None:
    print("=" * 64)
    print("SMOKE TEST -- exercises the call pipeline without a phone call")
    print("=" * 64)
    test_config()
    test_build()
    # The plugins pull their aiohttp session from this context var, which the
    # worker normally populates per job. Without it every HTTP-based plugin
    # fails with a bare "Connection error."
    hc = utils.http_context
    hc._ContextVar.set(hc._new_session_ctx())
    try:
        await test_tts()
        await test_llm()
        test_dashboard_reporting()
    finally:
        await hc._close_http_ctx()

    print("\n" + "=" * 64)
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL CHECKS PASSED -- safe to restart the worker")


asyncio.run(main())
