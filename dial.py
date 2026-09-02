"""Place an outbound call.

    python dial.py +919876543210 "Confirm their dental appointment on Thursday"

This creates an agent job with the number in its metadata. The worker
(agent.py) joins the room first and then dials out, so the agent is already
listening the moment the callee picks up.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

from dotenv import load_dotenv
from livekit import api

load_dotenv()

AGENT_NAME = "voice-assistant"


async def dial(phone_number: str, task: str) -> None:
    room_name = f"outbound-{uuid.uuid4().hex[:12]}"

    async with api.LiveKitAPI() as lk:
        dispatch = await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME,
                room=room_name,
                metadata=json.dumps({"phone_number": phone_number, "task": task}),
            )
        )

    print(f"dispatched {dispatch.id} -> {phone_number} in room {room_name}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    number = sys.argv[1]
    reason = sys.argv[2] if len(sys.argv) > 2 else "a general check-in"
    asyncio.run(dial(number, reason))
