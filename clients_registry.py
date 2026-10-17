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
        # INBOUND calls hear the Hindi real-estate demo (owner's request,
        # 2026-10-15). Outbound website-lead callbacks and dashboard demo
        # calls still run VoiceZen's own Telugu pitch -- server.py forces
        # Telugu settings on those paths regardless of this entry.
        "prompt_module": "script_realestate_hi",
        # A faint office behind the whole call. Re-enabled 2026-10-15 after
        # the dead-air cause was fixed (a stalled TTS request had no
        # first-byte deadline or retry; websocket sends now serialized).
        "background_noise": "low",
        # Hindi only for the inbound real-estate demo; mixed English words
        # are understood (hi-IN).
        "stt_language": "hi-IN",
        "tts_language": "hi",
        # Slower than normal -- 1.0 sounded fast on a real call, 0.9 still
        # a touch quick, 0.8 too slow; 0.85 is the owner's pick (2026-10-15).
        "tts_speed": 0.85,
        "reply_language": "Hindi",
        "repeat_guard": True,
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
        # A faint office behind the whole call. Re-enabled 2026-10-15 after
        # the dead-air cause was fixed (a stalled TTS request had no
        # first-byte deadline or retry; websocket sends now serialized).
        "background_noise": "low",
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
            "ఎలక్ట్రీషియన్, ప్రాక్టికల్స్, సర్టిఫికేట్, ట్రైనింగ్, బ్యాచ్, ఎల్ అండ్ టీ మెట్రో, ఫార్మా, "
            "ఆన్‌లైన్ క్లాసెస్, టైమింగ్స్, అడ్రస్, ఎస్ ఆర్ నగర్, రిజిస్ట్రేషన్, డాక్యుమెంట్స్, టెన్త్, ఇంటర్, డిగ్రీ"
        ),
        # Live transfer target when the agent says [[TRANSFER]].
        # Spoken-text fixes: the model keeps writing ITI as "ఇటీవలి" (recent).
        # The I.E.M fee is chosen in code from what the student said they
        # studied -- the model once quoted the B.Tech fee to an ITI student.
        # First match wins; order matters (other branches before plain EEE).
        "qualification_lines": [
            (r"(బీటెక్|బి\.? ?టెక్|b\.? ?tech|డిప్లొమా|డిప్లమా|diploma|ఐటీఐ|ITI).{0,25}"
             r"(మెకానికల్|mechanical|సివిల్|civil|సీఎస్ఈ|CSE|కంప్యూటర్|computer|ఈసీఈ|ECE|ఐటీ |ఫిట్టర్|fitter|వెల్డర్|welder|"
             r"ఆటోమొబైల్|automobile|కెమికల్|chemical)",
             "మీరు కూడా జాయిన్ అవ్వొచ్చండి. ప్రాక్టికల్స్ కోర్సు ఫీజు పదిహేను వేల రూపాయలు, ఫార్టీ ఫైవ్ డేస్, సర్టిఫికేట్ ఇస్తామండి."),
            (r"ఐటీఐ|ITI|ఐ టి ఐ",
             "ఐటీఐ వాళ్ళకి ఫీజు పదిహేడు వేల రూపాయలండి, కోర్సు ఫార్టీ ఫైవ్ డేస్, హండ్రెడ్ పర్సెంట్ జాబ్ ప్లేస్‌మెంట్ కూడా ఉంటుంది."),
            (r"డిప్లొమా|డిప్లమా|diploma|పాలిటెక్నిక్|polytechnic",
             "డిప్లొమా ఈఈఈ వాళ్ళకి ఫీజు పంతొమ్మిది వేల రూపాయలండి, కోర్సు ఫార్టీ ఫైవ్ డేస్, హండ్రెడ్ పర్సెంట్ జాబ్ ప్లేస్‌మెంట్ కూడా ఉంటుంది."),
            (r"బీటెక్|బి\.? ?టెక్|b\.? ?tech|ఇంజనీరింగ్|engineering|బీఈ|B\.?E",
             "బీటెక్ ఈఈఈ వాళ్ళకి ఫీజు ఇరవై మూడు వేల రూపాయలండి, కోర్సు ఫార్టీ ఫైవ్ డేస్, హండ్రెడ్ పర్సెంట్ జాబ్ ప్లేస్‌మెంట్ కూడా ఉంటుంది."),
            # 10th/SSC joined the practicals course on 2026-10-17 (owner's
            # updated brief: non-technical students welcome at ₹15,000).
            (r"టెన్త్|టెంత్|10th|పదో తరగతి|పదవ తరగతి|ఎస్ఎస్సీ|ssc|"
             r"బీకాం|బి\.? ?కాం|b\.? ?com|బీఎస్సీ|బి\.? ?ఎస్సీ|b\.? ?sc|బీఏ|డిగ్రీ|degree|ఇంటర్(?!వ్యూ|నెట్|ెస్ట్)|\binter\b|"
             r"ఎంబీఏ|mba|ఎంసీఏ|mca",
             "మీరు కూడా జాయిన్ అవ్వొచ్చండి. ప్రాక్టికల్స్ కోర్సు ఫీజు పదిహేను వేల రూపాయలు, ఫార్టీ ఫైవ్ డేస్, సర్టిఫికేట్ ఇస్తామండి."),
        ],
        # Spoken Telugu joins అండి to the word before it ("చదివారండి").
        "join_andi": True,
        # The script's goodbye line: saying it ends the call.
        "goodbye_phrase": "ఈ నంబర్‌కి కాల్ చేయండి. బై",
        # Mixed te-en numbers (owner's picks, 2026-10-17): fees, salaries and
        # seat counts stay in Telugu words; durations, percentages, practicals
        # counts and clock times go English-style. The fixes below translate
        # what the model still writes the other way.
        "speech_fixes": {"ఫీజు ఇరవై వేల రూపాయలు": "ఫీజు ఇరవై మూడు వేల రూపాయలు",  # no ₹20,000 fee exists
                         "నలభై ఐదు రోజులు": "ఫార్టీ ఫైవ్ డేస్", "నలభై ఐదు": "ఫార్టీ ఫైవ్",
                         "వంద శాతం": "హండ్రెడ్ పర్సెంట్", "ముప్పై ఆరు": "థర్టీ సిక్స్",
                         # A Hinglish caller once pulled digits out of the
                         # model ("సాలరీ 20 వేల నుంచి 25 వేల") -- spoken digits
                         # are never OK, so the known figures get word forms.
                         "15 వేల": "పదిహేను వేల", "17 వేల": "పదిహేడు వేల", "18 వేల": "పద్దెనిమిది వేల",
                         "19 వేల": "పంతొమ్మిది వేల", "20 వేల": "ఇరవై వేల", "22 వేల": "ఇరవై రెండు వేల",
                         "23 వేల": "ఇరవై మూడు వేల", "25 వేల": "ఇరవై ఐదు వేల",
                         "45 డేస్": "ఫార్టీ ఫైవ్ డేస్", "45 రోజులు": "ఫార్టీ ఫైవ్ డేస్",
                         "100 శాతం": "హండ్రెడ్ పర్సెంట్", "100%": "హండ్రెడ్ పర్సెంట్",
                         # Latin short forms the Telugu voice garbles.
                         "SR ఎలక్ట్రికల్": "ఎస్ ఆర్ ఎలక్ట్రికల్", "AI అసిస్టెంట్": "ఏ ఐ అసిస్టెంట్",
                         "ఐ ఈ ఎం కోర్సు అద్భుతం.": "సరేనండి.", "అద్భుతం": "సరేనండి",
                         "మీకు ఏం చదివార": "మీరు ఏం చదివార", "మీకు బీటెక్, డిప్లొమా": "మీరు బీటెక్, డిప్లొమా",
                         "ఐ.ఈ.ఎం": "ఐ ఈ ఎం", "ఐ. ఈ. ఎం": "ఐ ఈ ఎం", "I.E.M": "ఐ ఈ ఎం", "I. E. M": "ఐ ఈ ఎం",
                         "IEM": "ఐ ఈ ఎం", "ఇటీవలి ITI": "ఐటీఐ", "ఇటీవలి ఐటీఐ": "ఐటీఐ", "ITI": "ఐటీఐ", "ఇటీఐ": "ఐటీఐ",
                         "ఇంటర్నేషనల్ టెక్నికల్ ఇన్స్టిట్యూట్": "ఐటీఐ",
                         # Batch times English-style, the owner's ask (2026-10-17):
                         # "టెన్ థర్టీ" the way students say them, never "పది ముప్పై".
                         "పది ముప్పై": "టెన్ థర్టీ", "ఒంటి గంట ముప్పై": "వన్ థర్టీ", "ఒకటిన్నర": "వన్ థర్టీ",
                         "ఏడు ముప్పై": "సెవెన్ థర్టీ", "ఎనిమిది ముప్పై": "ఎయిట్ థర్టీ",
                         "ఏడున్నర": "సెవెన్ థర్టీ", "ఎనిమిదిన్నర": "ఎయిట్ థర్టీ",
                         "మూడు నుంచి ఆరు వరకు": "త్రీ నుంచి సిక్స్ వరకు"},
        # Questions the client's brief has no answer for -> the no-info line.
        # Address, timings, batches, online classes and the documents overview
        # came OUT of this list on 2026-10-17: the owner's updated brief gave
        # the bot those answers.
        "transfer_topics": (
            r"హాస్టల్|hostel|రూమ్|ఫుడ్|భోజనం|ఈఎంఐ|EMI|ఇన్‌స్టాల్‌మెంట్|ఇన్స్టాల్మెంట్|డిస్కౌంట్|discount|తగ్గించ|తగ్గిస్తా|"
            r"గవర్నమెంట్|government|అప్రూవ్|approved|గుర్తింపు|"
            r"లోన్|loan|ఏజ్|age limit|వయస్సు|వయసు|రెండూ కలిపి|both together"
        ),
        # Tell the model which questions it has already asked twice.
        "repeat_guard": True,
        # Live-transfer target: the owner's own phone for now (their ask,
        # 2026-10-17). The admission team's number is +919154681593 --
        # restore it when the owner says so.
        "transfer_to": "+919704123933",
        # When a caller says yes to the team connect, their details go to
        # this WhatsApp number via the lead-alert edge function (owner's
        # ask, 2026-10-17) -- WaSender internal channel, not a customer
        # WhatsApp number.
        "lead_alert_whatsapp": "+919493918625",
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
