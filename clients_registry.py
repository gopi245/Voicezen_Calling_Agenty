"""Which client owns which dialed number.

vobiz-native serves every client from one process. This is the lookup that
replaces what LiveKit's dispatch-rule metadata used to do: given the number
a caller dialed, find which client's script to load and which Vobiz account
to use for anything outbound.

To onboard a new client: add an entry here, and drop their script module
(same shape as script_gundusoodi.py -- GREETING and INSTRUCTIONS) next to it.

Each client's number is dedicated to them in BOTH directions: `dial_from` is
the caller ID for outbound calls placed on their behalf, and the same number
is the key callers reach them on inbound. Before this existed every outbound
call went out as Gundusoodi's number, so a VoiceZen lead got called by a
Telugu magazine's line -- and reached the magazine script if they rang back.
"""
from __future__ import annotations

import os

CLIENTS: dict[str, dict] = {
    "+918071580257": {
        "client_id": "voicezen",
        # VoiceZen's own line, for enquiries from voicezentech.com.
        # customer_id comes from .env because it is a dashboard row id, not a
        # code constant -- set VOICEZEN_CUSTOMER_ID once the VoiceZen customer
        # exists. Left unset, inbound calls here still connect and speak the
        # right script (reporting keys off dialed_number, not this), but two
        # things silently don't happen: the out-of-minutes check is skipped,
        # and an outbound campaign for this customer can't be matched back to
        # this number, so it dials out as DIAL_FROM_NUMBER instead.
        "customer_id": (
            int(os.environ["VOICEZEN_CUSTOMER_ID"])
            if os.getenv("VOICEZEN_CUSTOMER_ID", "").strip().isdigit()
            else None
        ),
        "dial_from": "+918071580257",
        "prompt_module": "script_voicezen",
        # Telugu only, at the owner's request (2026-10-12): back from
        # multilingual auto-detect to plain Telugu. Hindi callers have their
        # own line (+918065353946). Listening in te-IN still understands the
        # English business words Telugu callers mix in.
        "stt_language": "te-IN",
        "tts_language": "te",
        "reply_language": "Telugu",
        "multilingual": False,
        "vobiz_auth_id": os.getenv("VOBIZ_AUTH_ID", ""),
        "vobiz_auth_token": os.getenv("VOBIZ_AUTH_TOKEN", ""),
    },
    # Gundusoodi's former voice number, moved to VoiceZen on 2026-10-08 at the
    # owner's request (Gundusoodi keeps its WhatsApp number and history). Listed
    # AFTER VoiceZen's main line on purpose: dial_from_for_customer returns the
    # first match, so VoiceZen's outbound and demo calls keep going out as
    # +918071580257.
    "+918065353946": {
        "client_id": "voicezen",
        "customer_id": (
            int(os.environ["VOICEZEN_CUSTOMER_ID"])
            if os.getenv("VOICEZEN_CUSTOMER_ID", "").strip().isdigit()
            else None
        ),
        "dial_from": "+918065353946",
        "prompt_module": "script_voicezen_hi",
        # Hindi only, at the owner's request (2026-10-10): auto-detect turned a
        # Hindi caller into Punjabi on a real call and the agent answered in
        # Punjabi. Listening in hi-IN still understands English words.
        "stt_language": "hi-IN",
        "tts_language": "hi",
        "reply_language": "Hindi",
        # Kept on Raya at the owner's request (2026-10-09) while Google voices
        # are tried on the Telugu line.
        "tts_provider": "raya",
        "multilingual": False,
        "vobiz_auth_id": os.getenv("VOBIZ_AUTH_ID", ""),
        "vobiz_auth_token": os.getenv("VOBIZ_AUTH_TOKEN", ""),
    },
}


def lookup(dialed_number: str | None) -> dict | None:
    if not dialed_number:
        return None
    return CLIENTS.get(dialed_number)


def dial_from_for_customer(customer_id: int | None) -> str | None:
    """The caller ID an outbound call for this customer should go out as.

    Campaign and instant-lead calls carry a customer_id but not a number, so
    this is the only link back to whose line the call belongs on. Returns
    None when the customer isn't in the registry, leaving the caller to fall
    back to the service-wide default rather than guessing at someone's number.
    """
    if customer_id is None:
        return None
    for client in CLIENTS.values():
        if client.get("customer_id") == customer_id:
            return client.get("dial_from")
    return None
