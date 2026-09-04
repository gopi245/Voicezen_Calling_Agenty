"""Vobiz + LiveKit voice agent -- handles both inbound and outbound calls.

Inbound:  Vobiz routes the PSTN call to your LiveKit SIP URI, a dispatch rule
          puts it in a room, and this worker is dispatched by agent name.
Outbound: dial.py creates a job with {"phone_number": ...} metadata; this
          worker joins the room first, then places the SIP call.

Run with:  python agent.py dev      (local, hot reload)
           python agent.py start    (production)
"""

from __future__ import annotations

import json
import logging
import os
import time

from dotenv import load_dotenv
from livekit import api
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    metrics,
)
import clients
import voicezen
from stack import build_llm, build_realtime, build_stt, build_tts

load_dotenv()
logger = logging.getLogger("vobiz-agent")

AGENT_NAME = "voice-assistant"  # must match the LiveKit dispatch rule exactly


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------

BASE_STYLE = """You are speaking on a live phone call, so everything you say is
converted to speech and heard out loud.

Answer in ONE sentence of at most fifteen words. This is a hard limit, not a
guideline. Two sentences are allowed only when confirming details back to the
caller.

A caller cannot skim or skip ahead -- they sit through every word in real
time. Fifteen words takes about four seconds to say; thirty words takes nine,
and the call starts to feel broken. Brevity is the single thing that makes
this agent feel fast.

Answer the question asked and stop. Do not restate the question, do not
introduce yourself again, do not list options nobody asked for, do not explain
what you are about to do, and do not add a closing pleasantry to every turn.
If something genuinely needs more explanation, give the one-sentence version
and ask if they want the detail.

Never use markdown, bullet points, emoji, or symbols like * or # -- they get
read aloud or mangled. Write numbers, dates and currency the way a person would
say them ("two thirty in the afternoon", "four hundred rupees"), not as digits
and symbols.

Phone audio is lossy. If you did not catch something, ask the caller to repeat
it rather than guessing -- especially names, numbers and spellings. Read
anything important back to confirm it.

You cannot see anything. Do not refer to screens, links, or documents."""


class CallAgent(Agent):
    """Shared behaviour: hang up and warm-transfer tools."""

    def __init__(self, instructions: str, base_style: bool = True) -> None:
        # A scripted bot supplies its own complete voice rules -- BASE_STYLE's
        # fifteen-word limit would truncate its scripted answers mid-sentence.
        prompt = f"{BASE_STYLE}\n\n{instructions}" if base_style else instructions
        super().__init__(instructions=prompt)

    @function_tool()
    async def end_call(self, ctx: RunContext) -> str:
        """End the phone call. Use only after saying goodbye to the caller."""
        # Let the closing line finish playing before tearing down the room,
        # otherwise the caller hears the audio cut off mid-word.
        await ctx.session.drain()
        job_ctx = ctx.session.userdata["job_ctx"]
        await job_ctx.api.room.delete_room(
            api.DeleteRoomRequest(room=job_ctx.room.name)
        )
        return "call ended"

    @function_tool()
    async def transfer_call(self, ctx: RunContext, phone_number: str) -> str:
        """Transfer the caller to a human on another number.

        Args:
            phone_number: The destination in E.164 format, e.g. +919876543210.
        """
        job_ctx = ctx.session.userdata["job_ctx"]
        participant = ctx.session.userdata.get("sip_participant")
        if participant is None:
            return "no telephone participant on this call, transfer not possible"

        await ctx.session.say("Transferring you now, please hold.")
        await ctx.session.drain()
        try:
            # Vobiz supports cold transfer via SIP REFER.
            await job_ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=job_ctx.room.name,
                    participant_identity=participant.identity,
                    transfer_to=f"tel:{phone_number}",
                    play_dialtone=True,
                )
            )
        except Exception:
            logger.exception("SIP transfer failed")
            return "the transfer did not go through; stay on the line with the caller"
        return f"transferred to {phone_number}"


def build_client_agent(profile: clients.ClientProfile) -> CallAgent:
    """Build the agent for one client.

    The client's Tools mixin is combined with CallAgent at call time, so a new
    customer is a folder under clients/ -- this function never changes.
    base_style is off because a client profile supplies its own complete voice
    rules; the generic fifteen-word limit would truncate scripted answers.
    """
    agent_cls = type(
        f"{profile.client_id.title()}Agent", (profile.tools, CallAgent), {}
    )
    return agent_cls(profile.instructions, base_style=False)


class InboundAgent(CallAgent):
    def __init__(self) -> None:
        super().__init__(
            """Someone has just called the business line and you answered.

Greet them, find out what they need, and either answer it or take a message
with their name, number and reason for calling. If they ask for a human, use
the transfer_call tool. When the conversation is finished, say goodbye and use
the end_call tool."""
        )


class OutboundAgent(CallAgent):
    def __init__(self, task: str) -> None:
        super().__init__(
            f"""You placed this call, so the person who answered is not
expecting you. Open by saying who you are and why you are calling, in one
sentence, then pause for them to respond.

Your reason for calling: {task}

If they say it is a bad time, apologise, offer to call back, and use the
end_call tool. Respect a refusal immediately -- never push a second time. When
you are done, say goodbye and use the end_call tool."""
        )


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


def prewarm(proc) -> None:
    """Build the pipeline inside an IDLE worker process, before a call lands.

    Without this, the first thing an incoming call does is spawn a fresh
    Python process (multiprocessing uses "spawn", so every module is
    re-imported from scratch), import livekit + google + cartesia + sarvam,
    and open the provider connections. Measured on a real call, that was
    ~5 seconds between "received job request" and the greeting starting to
    synthesise -- the caller hears ringing, then silence, and often hangs up.

    Doing it here moves that cost into an idle process that is already warm
    when the call arrives.
    """
    # Keep the built components on the process, not just as a warm-up side
    # effect. Before this they were constructed here and thrown away, so every
    # call still paid to build three provider clients from scratch inside the
    # job -- part of the ~6s between "received job request" and the caller
    # hearing anything.
    proc.userdata["stt"] = build_stt()
    proc.userdata["llm"] = build_llm()
    tts = build_tts()
    proc.userdata["tts"] = tts

    # Sarvam's TTS plugin opens a fresh WebSocket per utterance (not once per
    # call), which measured ~1.7-2s on the greeting -- part of why the caller
    # hears nothing for several seconds after the call connects. The plugin
    # has its own connection pool with a prewarm() method built for exactly
    # this; without calling it, that pool sits empty until the first real
    # utterance pays to fill it. Guarded with hasattr since not every TTS
    # provider's plugin exposes this (e.g. Cartesia's does not).
    if hasattr(tts, "prewarm"):
        tts.prewarm()

    logger.info("worker process prewarmed")


def validate_pipeline() -> None:
    """Fail fast on bad config, before any caller hears it.

    The plugins validate their arguments in __init__, and that normally runs
    inside the job -- so an out-of-range setting sails through worker startup,
    crashes the moment a real call arrives, and the caller gets a carrier
    "not reachable" message with nothing to show it was our fault.

    prewarm_fnc is not enough: it only runs when a job process spawns, which
    in dev mode is when the first call comes in. Building the pipeline here,
    before the worker registers, is what makes a config error a startup error.
    """
    mode = os.getenv("PIPELINE", "cascaded")
    if mode == "realtime":
        build_realtime()
    else:
        build_stt()
        build_llm()
        build_tts()
    logger.info("pipeline config validated (%s)", mode)


def build_session(ctx: JobContext) -> AgentSession:
    if os.getenv("PIPELINE", "cascaded") == "realtime":
        # Speech-to-speech: the model does its own listening, turn detection
        # and speaking, so it takes no stt/tts/vad/turn_detection. Passing any
        # of them here would be ignored at best.
        return AgentSession(userdata={"job_ctx": ctx}, llm=build_realtime())

    extra: dict = {}

    if os.getenv("STT_PROVIDER", "sarvam") == "sarvam":
        # Sarvam's STT runs its own VAD and emits speech-end events directly
        # (stack.py sets flush_signal=True), so the turn boundary comes from
        # the recogniser rather than a separate silence timer. That is what
        # makes a 70ms delay safe.
        turn_detection = "stt"
        default_delay = "0.07"
    else:
        # Deepgram has no speech-end signal, so the turn boundary falls back to
        # silence plus a semantic turn detector -- which needs a real VAD and a
        # much longer delay. Using 0.07 here would cut callers off constantly.
        from livekit.plugins import silero
        from livekit.plugins.turn_detector.multilingual import MultilingualModel

        extra["vad"] = silero.VAD.load()
        turn_detection = MultilingualModel()
        default_delay = "0.35"

    # Reuse the prewarmed clients when the job landed on a warm process; fall
    # back to building them if it did not.
    warm = ctx.proc.userdata
    return AgentSession(
        userdata={"job_ctx": ctx},
        stt=warm.get("stt") or build_stt(),
        llm=warm.get("llm") or build_llm(),
        tts=warm.get("tts") or build_tts(),
        # The flat min_endpointing_delay / preemptive_generation /
        # turn_detection arguments are deprecated and removed in v2.0; they
        # warned on every single call. This is the same configuration in the
        # current shape.
        turn_handling={
            "turn_detection": turn_detection,
            "endpointing": {
                "min_delay": float(
                    os.getenv("MIN_ENDPOINTING_DELAY", default_delay)
                ),
                "max_delay": 3.0,
            },
            # Phone lines are noisy, and the defaults are far too eager for
            # one: min_words defaults to 0, so ANY fragment the STT emits --
            # line noise, a cough, a half-syllable -- counted as an
            # interruption and cut the agent off mid-sentence. Callers heard
            # the bot start talking and immediately stop.
            "interruption": {
                # No explicit "mode": "adaptive" needs a detector this
                # STT-endpointed configuration does not provide, and LiveKit
                # silently disables it ("interruption_detection is provided,
                # but it's not compatible ... and will be disabled"). Letting
                # the session auto-select keeps min_words actually in effect.
                "min_words": int(os.getenv("INTERRUPT_MIN_WORDS", "2")),
                "min_duration": float(os.getenv("INTERRUPT_MIN_SEC", "0.6")),
                # If it does cut out on noise, resume rather than sit silent.
                "resume_false_interruption": True,
                # Defaults to True, which THREW AWAY everything the caller
                # said during the non-interruptible greeting. On a live call
                # the greeting played, the caller asked their question over
                # it, the audio was discarded, the LLM never ran, and the line
                # went silent. Keep the greeting uninterruptible so noise
                # cannot cut it, but keep what the caller said and answer it
                # once the greeting finishes.
                "discard_audio_if_uninterruptible": False,
            },
            # Start generating on interim transcripts and throw the result
            # away if the caller keeps talking. Trades wasted tokens for
            # latency -- check llm_ttft in the metrics before keeping it.
            "preemptive_generation": {"enabled": True},
        },
        **extra,
    )


def _dialed_number(ctx: JobContext, session: AgentSession) -> str:
    """Which of our numbers the caller dialled.

    Set on the dispatch rule (see set_dispatch_metadata.py) rather than read
    from a LiveKit SIP attribute, because the SDK exposes no named constant
    for the dialled number and guessing a key would break silently. Checked in
    three places because LiveKit surfaces rule metadata via more than one
    route; falls back to the single-client env var.
    """
    for source in (ctx.job.metadata, getattr(ctx.room, "metadata", None)):
        if source:
            try:
                value = json.loads(source).get("dialed_number")
                if value:
                    return value
            except (json.JSONDecodeError, AttributeError):
                pass

    participant = session.userdata.get("sip_participant")
    attrs = dict(getattr(participant, "attributes", {}) or {})
    for key in ("dialed_number", "sip.trunkPhoneNumber", "sip.phoneNumber"):
        if attrs.get(key):
            return attrs[key]

    return os.getenv("VOBIZ_PHONE_NUMBER", "")


def _caller_number(session: AgentSession) -> str | None:
    participant = session.userdata.get("sip_participant")
    if participant is None:
        return None
    attrs = dict(getattr(participant, "attributes", {}) or {})
    # identity is set to "pstn-<number>" for outbound; inbound carries it here.
    return attrs.get("sip.phoneNumber") or getattr(participant, "identity", None)


async def _summarise(transcript: str) -> str | None:
    """One short LLM call after hang-up to fill the dashboard's summary column.

    Deliberately after the caller has gone, so its latency and failure modes
    cannot reach them. Returns None rather than raising -- a missing summary
    is much better than a lost call log.
    """
    if not transcript.strip() or os.getenv("CALL_SUMMARY", "1") != "1":
        return None
    try:
        from livekit.agents import ChatContext

        ctx = ChatContext()
        ctx.add_message(
            role="system",
            content=(
                "Summarise this phone call for a business dashboard, in English, "
                "in at most four sentences. State what the caller wanted, any "
                "details they gave (name, address, phone, plan), and the outcome. "
                "If they placed an order or made a complaint, say so explicitly."
            ),
        )
        ctx.add_message(role="user", content=transcript[:12000])
        out = ""
        async for chunk in build_llm().chat(chat_ctx=ctx):
            if chunk.delta and chunk.delta.content:
                out += chunk.delta.content
        return " ".join(out.split()) or None
    except Exception:
        logger.exception("could not generate a call summary")
        return None


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    metadata = json.loads(ctx.job.metadata) if ctx.job.metadata else {}
    dial_to = metadata.get("phone_number")

    session = build_session(ctx)

    usage = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:
        # end_of_utterance_delay / llm_ttft / tts_ttfb here are your real
        # latency budget. Watch these, not the marketing numbers.
        metrics.log_metrics(ev.metrics)
        usage.collect(ev.metrics)

    # Log both sides of the conversation, and keep them for the dashboard.
    # Without this a call leaves no record of what was actually said, so
    # "it did not answer properly" cannot be diagnosed -- only guessed at.
    turns: list[str] = []

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        item = getattr(ev, "item", None)
        if item is None:
            return
        text = getattr(item, "text_content", None)
        if not text:
            # text_content can be empty; content is a list of parts.
            parts = getattr(item, "content", None) or []
            text = " ".join(p for p in parts if isinstance(p, str))
        if text:
            role = getattr(item, "role", "?")
            logger.info("TRANSCRIPT [%s] %s", role, text)
            turns.append(f"[{role}] {text}")

    session.userdata["turns"] = turns
    session.userdata["started_at"] = time.monotonic()

    # Stop the billing clock the moment the caller hangs up, NOT at worker
    # shutdown. Everything after hang-up -- the summary LLM call, teardown,
    # the HTTP post -- used to fall inside the timed window: a 22s call was
    # reported as 43s, because generating the summary took ~21s and was
    # counted as call time. The caller is long gone by then and the carrier
    # bills none of it.
    @session.on("close")
    def _on_close(_ev) -> None:
        session.userdata.setdefault("ended_at", time.monotonic())

    async def _report_call() -> None:
        """Send the finished call to the dashboard. Runs after hang-up, so it
        can never affect the caller; wrapped so a reporting failure cannot
        take the worker down."""
        logger.info("call usage: %s", usage.get_summary())
        try:
            started = session.userdata.get("started_at", time.monotonic())
            # Falls back to now if the close event never fired (a crashed job),
            # which over-reports slightly rather than losing the call entirely.
            ended = session.userdata.get("ended_at", time.monotonic())
            duration = max(ended - started, 0)
            transcript = "\n".join(session.userdata.get("turns", []))
            await voicezen.report_call_async(
                dialed_number=_dialed_number(ctx, session),
                call_duration_seconds=duration,
                caller_number=_caller_number(session),
                transcript=transcript or None,
                call_summary=await _summarise(transcript),
                language=os.getenv("SARVAM_LANGUAGE", "te-IN"),
                captured_data=session.userdata.get("captured") or None,
            )
        except Exception:
            logger.exception("failed to report the call to the dashboard")

    ctx.add_shutdown_callback(_report_call)

    # Which customer this call belongs to comes from the dispatch rule, so a
    # new client is a folder plus a rule -- never a change here. The env var
    # remains as a single-client override for local testing.
    client_id = metadata.get("client") or os.getenv("AGENT_PROFILE") or None
    profile: clients.ClientProfile | None = None

    if dial_to and not client_id:
        agent = OutboundAgent(metadata.get("task", ""))
    elif client_id and client_id != "generic":
        profile = clients.load(client_id)
        agent = build_client_agent(profile)
        logger.info("serving client %r", profile.client_id)
    else:
        agent = InboundAgent()

    # Noise is the single biggest cause of misrecognition on this line.
    # Measured on synthetic Telugu at telephony quality: 92% accurate through
    # the G.711 codec alone, but 67% once line noise is added -- the codec is
    # harmless, the noise is not. BVCTelephony is Krisp background-voice
    # cancellation tuned for narrowband phone audio, applied to the caller's
    # track before it reaches the recogniser.
    room_input = RoomInputOptions()
    try:
        from livekit.plugins import noise_cancellation

        room_input = RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony()
        )
        logger.info("noise cancellation enabled (BVCTelephony)")
    except Exception as e:
        # Self-hosted LiveKit has no BVC; the call should still connect.
        logger.warning("noise cancellation unavailable, continuing without: %s", e)

    await session.start(room=ctx.room, agent=agent, room_input_options=room_input)

    if dial_to:
        await _place_outbound_call(ctx, session, dial_to)
    else:
        if profile is not None:
            # The greeting is scripted verbatim (node 1), so say it directly
            # rather than asking the model to produce a greeting -- that is
            # one less chance to paraphrase a fixed line, and it skips an LLM
            # round-trip at the most latency-visible moment of the call.
            # The published workflow marks the greeting node allow_interrupt:
            # false -- the caller has just heard the line connect, so early
            # line noise must not cut the welcome in half.
            # Speak first, resolve the participant afterwards. The greeting is
            # fixed text that needs nothing from the participant object, and
            # every step before the first audio is time the caller spends
            # listening to silence.
            # say() is NOT a coroutine -- it starts the speech and returns an
            # awaitable SpeechHandle. Wrapping it in create_task raises
            # "a coroutine was expected" and kills the job, which the caller
            # hears as an answered call with dead silence.
            speaking = session.say(profile.greeting, allow_interruptions=False)
            session.userdata["sip_participant"] = await ctx.wait_for_participant()
            await speaking
        else:
            session.userdata["sip_participant"] = await ctx.wait_for_participant()
            await session.generate_reply(
                instructions="Greet the caller warmly and ask how you can help."
            )


async def _place_outbound_call(
    ctx: JobContext, session: AgentSession, phone_number: str
) -> None:
    trunk_id = os.environ["LIVEKIT_OUTBOUND_TRUNK_ID"]
    identity = f"pstn-{phone_number}"

    try:
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=trunk_id,
                sip_call_to=phone_number,
                participant_identity=identity,
                # Block until the callee actually picks up, so the agent does
                # not start talking into a ringing line.
                wait_until_answered=True,
            )
        )
    except api.TwirpError as e:
        logger.error(
            "outbound call to %s failed: %s (sip status %s)",
            phone_number,
            e.message,
            e.metadata.get("sip_status"),
        )
        ctx.shutdown()
        return

    participant = await ctx.wait_for_participant(identity=identity)
    session.userdata["sip_participant"] = participant
    await session.generate_reply(
        instructions="Introduce yourself and state your reason for calling."
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    validate_pipeline()
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            # LiveKit keeps ZERO idle processes in dev mode by default
            # (dev_default=0, prod_default=4), so every call paid a full
            # process spawn. Keeping a couple warm is what removes the ~5s
            # answer delay; each one costs memory, so 2 is a reasonable floor
            # for a single-line demo. Raise it for concurrent calls.
            num_idle_processes=int(os.getenv("NUM_IDLE_PROCESSES", "2")),
            # Prewarm now builds three provider clients, which can exceed the
            # 10s default on a cold start.
            initialize_process_timeout=30.0,
            # Explicit agent name => the worker is only dispatched when a
            # dispatch rule or dial.py names it. Required for outbound.
            agent_name=AGENT_NAME,
        )
    )
