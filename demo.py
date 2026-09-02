"""Run every scenario the bot should handle and print the real answers.

Uses the production prompt, model and tools -- the only thing missing is the
phone line. Multi-turn scenarios keep their conversation state, so the address
flow and the number-reuse behaviour are exercised properly.

    python demo.py            -> writes demo_output.txt
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

load_dotenv()

from livekit.agents import ChatContext, utils  # noqa: E402

import agent  # noqa: E402
import gundusoodi  # noqa: E402
import stack  # noqa: E402

# Each scenario is a title plus the caller's turns in order.
SCENARIOS: list[tuple[str, list[str]]] = [
    ("What is the magazine", ["గుండుసూది మ్యాగజైన్ అంటే ఏంటి?"]),
    ("Subscription plans in general", ["సబ్‌స్క్రిప్షన్ ఎలా చేయాలి?"]),
    ("Rates, no format named", ["రేట్లు ఎంత?"]),
    ("Print rate", ["రేట్లు ఎంత?", "ప్రింట్"]),
    ("Digital rate", ["రేట్లు ఎంత?", "డిజిటల్"]),
    ("Single print copy (must refuse)", ["ఒక్క ప్రింట్ కాపీ కావాలి"]),
    ("Monthly print (must refuse)", ["ఒక నెల ప్రింట్ ప్లాన్ కావాలి"]),
    ("Three month print (must refuse)", ["మూడు నెలల ప్రింట్ కావాలి"]),
    ("Two year print (must refuse)", ["రెండు సంవత్సరాలు ప్రింట్ కావాలి"]),
    ("Six month digital (must refuse)", ["ఆరు నెలల డిజిటల్ కావాలి"]),
    ("Delivery timing", ["మ్యాగజైన్ ఎప్పుడు వస్తుంది?"]),
    ("Payment methods", ["పేమెంట్ ఎలా చేయాలి? ఫోన్‌పే ఉందా?"]),
    ("Old magazines", ["పాత మ్యాగజైన్లు దొరుకుతాయా?"]),
    ("Old PRINT copy (must refuse)", ["పాత ప్రింట్ కాపీ పంపగలరా?"]),
    ("Cinema content (must refuse)", ["సినిమా వార్తలు ఉంటాయా?"]),
    ("Unscripted: language", ["ఇది ఇంగ్లీష్‌లో వస్తుందా?"]),
    ("Unscripted: frequency", ["ఎన్ని రోజులకు ఒకసారి వస్తుంది?"]),
    ("Unscripted: read on phone", ["డిజిటల్ ఫోన్‌లో చదవొచ్చా?"]),
    ("Unknown fact (must not invent)", ["ఎడిటర్ ఎవరు? ఎన్ని పేజీలు?"]),
    ("Off-topic (must deflect)", ["ఈ రోజు క్రికెట్ స్కోర్ ఎంత?"]),
    (
        "Print order + full address",
        [
            "ప్రింట్ ఆరు నెలలు కావాలి",
            "నా పేరు సునీత",
            "నలభై ఐదు డాష్ రెండు డాష్ అరవై మూడు బై ఆరు",
            "ఎం జి రోడ్",
            "విజయవాడ",
            "కృష్ణా",
            "ఫైవ్ టూ జీరో జీరో వన్ జీరో",
            "అవును, సరిగ్గా ఉంది",
        ],
    ),
    (
        "Number reused, not asked twice",
        [
            "నమూనా పీడీఎఫ్ పంపండి",
            "9493918625",
            "పేమెంట్ లింక్ కూడా పంపండి",
        ],
    ),
    (
        "Payment issue",
        ["నిన్న పేమెంట్ చేసాను కానీ కన్ఫర్మేషన్ రాలేదు", "9493918625"],
    ),
    (
        "Delivery complaint",
        ["నాకు ఈ నెల మ్యాగజైన్ రాలేదు", "అదే నంబర్ 9493918625"],
    ),
    (
        "Address change",
        [
            "నేను వేరే ఊరికి మారాను, అడ్రస్ మార్చాలి",
            "సునీత",
            "పన్నెండు",
            "పార్క్ రోడ్",
            "గుంటూరు",
            "గుంటూరు",
            "ఐదు రెండు రెండు సున్నా సున్నా ఒకటి",
            "అవును సరైనదే",
        ],
    ),
    ("Misheard nonsense", ["పిల్లలు పిల్లలు పిల్లలు పిల్లలు"]),
    ("Goodbye", ["సరే, ధన్యవాదాలు, ఇంకేం లేదు"]),
]


async def run_scenario(llm, a, tools, title: str, turns: list[str], out: list[str]):
    out.append("=" * 72)
    out.append(f"### {title}")
    out.append("=" * 72)
    ctx = ChatContext()
    ctx.add_message(role="system", content=a.instructions)

    for turn in turns:
        out.append(f"\nCALLER: {turn}")
        ctx.add_message(role="user", content=turn)

        text, tool_names = "", []
        try:
            async for ch in llm.chat(chat_ctx=ctx, tools=tools):
                d = ch.delta
                if d and getattr(d, "tool_calls", None):
                    for tc in d.tool_calls:
                        n = getattr(tc, "name", None) or getattr(tc, "function", "?")
                        tool_names.append(str(n))
                if d and d.content:
                    text += d.content
        except Exception as e:
            out.append(f"BOT   : <ERROR {type(e).__name__}: {str(e)[:100]}>")
            continue

        clean = " ".join(text.split())
        if clean:
            out.append(f"BOT   : {clean}")
            ctx.add_message(role="assistant", content=clean)
        if tool_names:
            out.append(f"        [tool: {', '.join(sorted(set(tool_names)))}]")
            # Let the conversation continue past the tool call.
            ctx.add_message(role="assistant", content=clean or "సరే.")
    out.append("")


async def main() -> None:
    a = agent.GundusoodiAgent()
    llm = stack.build_llm()
    tools = list(a.tools)

    hc = utils.http_context
    hc._ContextVar.set(hc._new_session_ctx())

    out: list[str] = []
    out.append("GUNDUSOODI VOICE BOT -- FULL SCENARIO DEMO")
    out.append(f"model: {a.__class__.__name__} via production stack\n")
    out.append("GREETING (spoken on every inbound call):")
    out.append(f"BOT   : {gundusoodi.GREETING}\n")

    try:
        for title, turns in SCENARIOS:
            await run_scenario(llm, a, tools, title, turns, out)
    finally:
        await hc._close_http_ctx()

    with open("demo_output.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"wrote demo_output.txt ({len(out)} lines, {len(SCENARIOS)} scenarios)")


asyncio.run(main())
