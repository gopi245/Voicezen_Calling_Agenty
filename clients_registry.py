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
        # Background noise OFF (2026-10-15): with it on, replies after the
        # greeting never reached the caller on a real call -- see server
        # notes; re-enable only after that bug is fixed and a real call
        # confirms it.
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
    # SR Electrical Training Center, set up on 2026-10-12 at the owner's
    # request on the number that was VoiceZen's Hindi line (before that,
    # Gundusoodi's voice number). Telugu only. Runs on the newer call engine
    # (silence check, barge-in handling) like VoiceZen -- "engine": "new" --
    # and hands interested students to the admission team by live transfer.
    "+918065353946": {
        "client_id": "sr_electrical",
        "customer_id": (
            int(os.environ["SR_ELECTRICAL_CUSTOMER_ID"])
            if os.getenv("SR_ELECTRICAL_CUSTOMER_ID", "").strip().isdigit()
            else None
        ),
        "dial_from": "+918065353946",
        "prompt_module": "script_sr_electrical",
        # Background noise OFF (2026-10-15): with it on, replies after the
        # greeting never reached the caller on a real call -- see server
        # notes; re-enable only after that bug is fixed and a real call
        # confirms it.
        "engine": "new",
        "stt_language": "te-IN",
        "tts_language": "te",
        "reply_language": "Telugu",
        "tts_provider": "raya",
        "multilingual": False,
        # Speech-recognition hints: the words students actually say here.
        "stt_prompt": (
            "SR ఎలక్ట్రికల్, ఐఈఎం, ఐ ఈ ఎం, IEM, ఇండస్ట్రియల్ ఎలక్ట్రికల్ మెయింటెనెన్స్, హోమ్ ఆటోమేషన్, "
            "కోర్సు, ఫీజు, ప్లేస్‌మెంట్, జాబ్, సాలరీ, అడ్మిషన్, బీటెక్, ఈఈఈ, డిప్లొమా, ఐటీఐ, "
            "ఎలక్ట్రీషియన్, ప్రాక్టికల్స్, సర్టిఫికేట్, ట్రైనింగ్, బ్యాచ్, ఎల్ అండ్ టీ మెట్రో, ఫార్మా"
        ),
        # Live transfer target when the agent says [[TRANSFER]].
        # Spoken-text fixes: the model keeps writing ITI as "ఇటీవలి" (recent).
        # The I.E.M fee is chosen in code from what the student said they
        # studied -- the model once quoted the B.Tech fee to an ITI student.
        # First match wins; order matters (other branches before plain EEE).
        "qualification_lines": [
            (r"(బీటెక్|బి\.? ?టెక్|b\.? ?tech|డిప్లొమా|డిప్లమా|diploma|ఐటీఐ|ITI).{0,25}"
             r"(మెకానికల్|mechanical|సివిల్|civil|సీఎస్ఈ|CSE|కంప్యూటర్|computer|ఈసీఈ|ECE|ఐటీ|ఫిట్టర్|fitter|వెల్డర్|welder|"
             r"ఆటోమొబైల్|automobile|కెమికల్|chemical)",
             "మీరు కూడా జాయిన్ అవ్వొచ్చండి. ప్రాక్టికల్స్ కోర్సు ఫీజు పదిహేను వేల రూపాయలు, నలభై ఐదు రోజులు, సర్టిఫికేట్ ఇస్తామండి."),
            (r"ఐటీఐ|ITI|ఐ టి ఐ",
             "ఐటీఐ వాళ్ళకి ఫీజు పదిహేడు వేల రూపాయలండి, కోర్సు నలభై ఐదు రోజులు, వంద శాతం జాబ్ ప్లేస్‌మెంట్ కూడా ఉంటుంది."),
            (r"డిప్లొమా|డిప్లమా|diploma|పాలిటెక్నిక్|polytechnic",
             "డిప్లొమా ఈఈఈ వాళ్ళకి ఫీజు పంతొమ్మిది వేల రూపాయలండి, కోర్సు నలభై ఐదు రోజులు, వంద శాతం జాబ్ ప్లేస్‌మెంట్ కూడా ఉంటుంది."),
            (r"బీటెక్|బి\.? ?టెక్|b\.? ?tech|ఇంజనీరింగ్|engineering|బీఈ|B\.?E",
             "బీటెక్ ఈఈఈ వాళ్ళకి ఫీజు ఇరవై మూడు వేల రూపాయలండి, కోర్సు నలభై ఐదు రోజులు, వంద శాతం జాబ్ ప్లేస్‌మెంట్ కూడా ఉంటుంది."),
            (r"బీకాం|బి\.? ?కాం|b\.? ?com|బీఎస్సీ|బి\.? ?ఎస్సీ|b\.? ?sc|బీఏ|డిగ్రీ|degree|ఇంటర్(?!వ్యూ|నెట్|ెస్ట్)|\binter\b|టెన్త్|10th|పదో|"
             r"పదవ|ఎస్ఎస్సీ|ssc|ఎంబీఏ|mba|ఎంసీఏ|mca",
             "మీరు కూడా జాయిన్ అవ్వొచ్చండి. ప్రాక్టికల్స్ కోర్సు ఫీజు పదిహేను వేల రూపాయలు, నలభై ఐదు రోజులు, సర్టిఫికేట్ ఇస్తామండి."),
        ],
        # Spoken Telugu joins అండి to the word before it ("చదివారండి").
        "join_andi": True,
        # The script's goodbye line: saying it ends the call.
        "goodbye_phrase": "ఈ నంబర్‌కి కాల్ చేయండి. బై",
        "speech_fixes": {"ఫీజు ఇరవై వేల రూపాయలు": "ఫీజు ఇరవై మూడు వేల రూపాయలు",  # no ₹20,000 fee exists
                         "ఐ ఈ ఎం కోర్సు అద్భుతం.": "సరేనండి.", "అద్భుతం": "సరేనండి",
                         "మీకు ఏం చదివార": "మీరు ఏం చదివార", "మీకు బీటెక్, డిప్లొమా": "మీరు బీటెక్, డిప్లొమా",
                         "ఐ.ఈ.ఎం": "ఐ ఈ ఎం", "ఐ. ఈ. ఎం": "ఐ ఈ ఎం", "I.E.M": "ఐ ఈ ఎం", "I. E. M": "ఐ ఈ ఎం",
                         "IEM": "ఐ ఈ ఎం", "ఇటీవలి ITI": "ఐటీఐ", "ఇటీవలి ఐటీఐ": "ఐటీఐ", "ITI": "ఐటీఐ", "ఇటీఐ": "ఐటీఐ",
                         "ఇంటర్నేషనల్ టెక్నికల్ ఇన్స్టిట్యూట్": "ఐటీఐ"},
        # Questions the client's brief has no answer for -> always the
        # transfer sentence (the model invented answers in testing).
        "transfer_topics": (
            r"హాస్టల్|hostel|రూమ్|ఫుడ్|భోజనం|ఈఎంఐ|EMI|ఇన్‌స్టాల్‌మెంట్|ఇన్స్టాల్మెంట్|డిస్కౌంట్|discount|తగ్గిస్తారా|"
            r"అడ్రస్|address|ఎక్కడ ఉంది|లొకేషన్|location|టైమింగ్|timing|ఎన్ని గంటలకు|బ్యాచ్|batch|"
            r"ఆన్‌లైన్|ఆన్లైన్|online|గవర్నమెంట్|government|అప్రూవ్|approved|గుర్తింపు|డాక్యుమెంట్|document|"
            r"లోన్|loan|ఏజ్|age limit|వయస్సు|వయసు|రెండూ కలిపి|both together"
        ),
        # Tell the model which questions it has already asked twice.
        "repeat_guard": True,
        # TEMPORARY (2026-10-12): the owner's own phone, to test the transfer.
        # The client's admission team is +919154681593 -- switch back after.
        "transfer_to": "+919704123933",
        # The script's fixed transfer sentence -- also triggers the transfer
        # when the model leaves the marker off.
        "transfer_phrase": "టీమ్‌కి కనెక్ట్ చేస్తున్నాను",
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
