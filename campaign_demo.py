"""Demo: campaign-mode prompt assembled purely from launch-form fields.

Not tied to any client profile -- proves the "one generic template filled in
per campaign" idea from the plan, using the same production LLM as agent.py.
Nothing here talks to LiveKit, Vobiz, or the database; it's a text-only
simulation of what the AI would say on a real call.

    python campaign_demo.py
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from livekit.agents import ChatContext  # noqa: E402

import stack  # noqa: E402

# Exactly the sample values shown as placeholders in the "Launch a New
# Campaign" form.
DENTAL_CAMPAIGN = {
    "business_name": "Bright Smile Dental",
    "offering": "A free teeth-whitening consultation this month, normally Rs 1,500",
    "call_to_action": "Book them a consultation slot for this week or next",
    "objection_handling": "If they say it's too expensive -- it's free, no cost",
    "language": "English",
}

# A second, unrelated business -- proves the template isn't secretly tuned to
# dental clinics. Also in Telugu, to prove the language field actually
# changes what the agent speaks, not just a label.
INSURANCE_CAMPAIGN = {
    "business_name": "Suraksha Life Insurance",
    "offering": "A term life insurance plan starting at Rs 500 per month, covering up to 50 lakhs",
    "call_to_action": "Get their email or WhatsApp number so an advisor can send a personalised quote",
    "objection_handling": (
        "If they say they already have insurance -- ask if it covers at least 50 lakhs, "
        "most old policies don't. If they say it's a scam call -- offer to send details in "
        "writing first, no payment needed on this call."
    ),
    "language": "Telugu",
}

TEMPLATE = """You are an AI calling agent making an OUTBOUND call on behalf of {business_name}.

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
"""


async def run_scenario(llm, campaign: dict, title: str, caller_turns: list[str]) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")
    system_prompt = TEMPLATE.format(**campaign)
    chat_ctx = ChatContext()
    chat_ctx.add_message(role="system", content=system_prompt)

    # Outbound calls open with the agent speaking first.
    chat_ctx.add_message(role="user", content="[call connected -- speak first]")
    async with llm.chat(chat_ctx=chat_ctx) as stream:
        reply = "".join([c.delta.content async for c in stream if c.delta and c.delta.content])
    print(f"AGENT: {reply}")
    chat_ctx.add_message(role="assistant", content=reply)

    for turn in caller_turns:
        print(f"CALLER: {turn}")
        chat_ctx.add_message(role="user", content=turn)
        async with llm.chat(chat_ctx=chat_ctx) as stream:
            reply = "".join([c.delta.content async for c in stream if c.delta and c.delta.content])
        print(f"AGENT: {reply}")
        chat_ctx.add_message(role="assistant", content=reply)


DENTAL_SCENARIOS = [
    ("Interested caller", ["Oh a free consultation? What's involved?", "Okay sounds good, book me in"]),
    ("Price objection", ["How much does this cost?"]),
    ("Not interested", ["No thanks, not interested", "Please take me off your list"]),
    ("Suspicious of the call", ["Wait, who is this? How did you get my number?"]),
    (
        "Multi-turn negotiation -- objects twice before agreeing",
        [
            "I don't have time for this right now",
            "Even if it's free, I don't trust these calls",
            "Okay fine, what times do you have this week?",
        ],
    ),
    (
        "Off-script question the form never covered",
        ["Does the whitening actually work on coffee stains? I drink a lot of coffee"],
    ),
    (
        "Asks for a human",
        ["Can I talk to an actual person instead of a bot?"],
    ),
    (
        "Wants a callback, not now",
        ["I'm driving right now, can someone call me back tomorrow evening instead?"],
    ),
]

INSURANCE_SCENARIOS = [
    (
        "Telugu: already has insurance objection",
        ["నాకు ఇప్పటికే ఇన్సూరెన్స్ ఉంది", "యాభై లక్షలు ఉందో లేదో నాకు తెలియదు"],
    ),
    (
        "Telugu: thinks it's a scam call",
        ["ఇది స్కామ్ కాల్ లాగా ఉంది, నేను డబ్బు ఇవ్వను"],
    ),
    (
        "Telugu: interested, gives contact",
        ["ఆసక్తి ఉంది, వివరాలు చెప్పండి", "నా వాట్సాప్ నంబర్ తీసుకోండి"],
    ),
]


async def main() -> None:
    llm = stack.build_llm()
    for title, turns in DENTAL_SCENARIOS:
        await run_scenario(llm, DENTAL_CAMPAIGN, title, turns)
    for title, turns in INSURANCE_SCENARIOS:
        await run_scenario(llm, INSURANCE_CAMPAIGN, title, turns)


if __name__ == "__main__":
    asyncio.run(main())
