"""Stamp per-client metadata onto the inbound SIP dispatch rule.

The agent needs to know which number was dialled so the log-call function can
resolve the customer from it. LiveKit's own SIP participant attributes are not
exposed as named constants in the SDK, so rather than guess at a key, the
number is put here -- on the dispatch rule we control -- and arrives in
ctx.job.metadata, a path agent.py already reads for outbound calls.

One dispatch rule per client, each carrying its own number. Adding a client
means creating their rule; the agent code does not change.
"""

import asyncio
import json

from dotenv import load_dotenv

load_dotenv()

from livekit import api  # noqa: E402

RULE_ID = "SDR_fmFuDXBTCJNP"
METADATA = {
    "dialed_number": "+918071580257",
    "customer_id": 3,
    "client": "gundusoodi",
}


async def main() -> None:
    async with api.LiveKitAPI() as lk:
        # The SDK helper replaces the rule wholesale, so read the current one
        # first and add to it -- otherwise trunk_ids and the room prefix would
        # be wiped and inbound calls would stop routing.
        rules = await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
        existing = next(r for r in rules.items if r.sip_dispatch_rule_id == RULE_ID)

        existing.metadata = json.dumps(METADATA)
        for k, v in METADATA.items():
            existing.attributes[k] = str(v)
        # Belt and braces: the same values as agent metadata, so they arrive in
        # ctx.job.metadata too.
        if existing.room_config and existing.room_config.agents:
            for a in existing.room_config.agents:
                a.metadata = json.dumps(METADATA)

        await lk.sip.update_dispatch_rule(RULE_ID, existing)
        rules = await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
        for rule in rules.items:
            print(f"{rule.sip_dispatch_rule_id}")
            print(f"   attributes: {dict(rule.attributes)}")
            print(f"   metadata  : {rule.metadata}")


asyncio.run(main())
