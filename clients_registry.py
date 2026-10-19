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
        # INBOUND calls hear the Vedantha Vanam sandalwood-farmland script
        # (owner's request, 2026-10-18; replaced the Hindi real-estate demo,
        # script_realestate_hi, which stays in agent/ for rollback).
        # Outbound website-lead callbacks and dashboard demo calls still run
        # VoiceZen's own Telugu pitch -- server.py forces Telugu settings on
        # those paths regardless of this entry.
        "prompt_module": "script_vedantha_vanam",
        # A faint office behind the whole call. Re-enabled 2026-10-15 after
        # the dead-air cause was fixed (a stalled TTS request had no
        # first-byte deadline or retry; websocket sends now serialized).
        "background_noise": "low",
        # Telugu only, like SR Electrical.
        "stt_language": "te-IN",
        "tts_language": "te",
        "reply_language": "Telugu",
        "tts_provider": "raya",
        # A little faster than normal -- the owner's pick (2026-10-18).
        "tts_speed": 1.1,
        # Spoken-text fixes are further down (with the digit-to-word ones):
        # "పన్నెండున్నర" -> "పన్నెండు న్నర" is the owner's pick from five voice
        # samples (2026-10-18) -- the voice made it sound like "పన్నెండు వందల".
        # Speech-recognition hints: the words callers say about the project.
        "stt_prompt": (
            "వేదాంత వనం, శ్రీగంధం, ఫామ్‌ల్యాండ్, ప్లాట్, సెంట్లు, సెంటు, గజం, ఎకరం, లక్షలు, "
            "సైట్ విజిట్, లొకేషన్, సంతమాగులూరు, బాపట్ల, నరసరావుపేట, కోటప్పకొండ, జె ఎన్ టి యు, "
            "రిజిస్ట్రేషన్, డాక్యుమెంట్స్, ఆదాయం, పెట్టుబడి, ఆలోచించి చెబుతాను, ఆదివారం"
        ),
        "repeat_guard": True,
        # The site-visit day is the whole point of the call: up to four asks,
        # not two (a real call hit the cap early and never booked a visit).
        "repeat_limits": {"ఏ రోజు": 4},
        # Background noise from the caller's side turns into short phrases;
        # while the bot is mid-sentence, under three words never stops it.
        "min_interrupt_words": 3,
        # Once the booked-call closing line is said, thanks / OK / bye ends
        # the call with final_bye -- no more model replies.
        "closing_phrase": "సైట్ విజిట్ వివరాలు, లొకేషన్ మీకు ఈ నంబర్‌కి పంపిస్తామండి",
        "final_bye": "ధన్యవాదాలు అండి, నమస్కారం!",
        # Every finished call's lead goes to the client on WhatsApp. Empty
        # until the client's number arrives (owner, 2026-10-19).
        "lead_alert_whatsapp": "",
        "lead_alert_title": "Vedantha Vanam - site visit lead",
        # Known-false sentences swapped before they are spoken: a demo
        # doubled the plant count ("రెండు వంద శ్రీగంధం మొక్కలు", 2026-10-19).
        "sentence_fixes": [
            (r"రెండు రెట్లు|రెండు వంద|రెండు వందల (శ్రీగంధం )?మొక్క|యాభై శ్రీగంధం మొక్కలు పాతిక",
             "పాతిక సెంట్లకు వంద శ్రీగంధం మొక్కలండి."),
        ],
        # A visit-day word in the caller's words: the day is settled and the
        # day question is not asked again.
        "visit_day_rx": r"రేపు|రేపై|ఎల్లుండి|ఆదివారం|సోమవారం|మంగళవారం|బుధవారం|గురువారం|శుక్రవారం|శనివారం|"
                        r"వచ్చే వారం|ఈ వారం|నెక్స్ట్ వీక్|tomorrow|sunday|monday|saturday",
        # Numbers the model writes as digits, spoken as words.
        "speech_fixes": {"పన్నెండున్నర": "పన్నెండు న్నర",
                         "25 సెంట్ల": "పాతిక సెంట్ల", "25 సెంట్లు": "పాతిక సెంట్లు", "12.5 సెంట్ల": "పన్నెండు న్నర సెంట్ల",
                         "12.5": "పన్నెండు న్నర", "100 శ్రీగంధం": "వంద శ్రీగంధం", "100 మొక్క": "వంద మొక్క",
                         "7 లక్షల": "ఏడు లక్షల", "56,000": "యాభై ఆరు వేల", "1157": "వెయ్యి నూట యాభై ఏడు",
                         "12 సంవత్సరాల": "పన్నెండు సంవత్సరాల", "60:40": "అరవై నలభై", "14 కిలోమీటర్ల": "పద్నాలుగు కిలోమీటర్ల",
                         "7 కిలోమీటర్ల": "ఏడు కిలోమీటర్ల", "10 కిలోమీటర్ల": "పది కిలోమీటర్ల"},
        # Sentences of the client's convince line: spoken only when the caller
        # is about to leave, never tacked onto a price or facilities answer.
        "persuade_markers": ["పరిశీలించి నిర్ణయం", "అనుకూలమైన రోజున సైట్ విజిట్ ఏర్పాటు"],
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
            "ఆన్‌లైన్ క్లాసెస్, టైమింగ్స్, అడ్రస్, ఎస్ ఆర్ నగర్, రిజిస్ట్రేషన్, డాక్యుమెంట్స్, టెన్త్, ఇంటర్, డిగ్రీ, "
            # Yes/no words last: a real "వద్దండి. జాబ్ ప్లేస్‌మెంట్ ఎక్కడ
            # ఇస్తారండి?" was heard as "అబ్బే అండి" + "ఆ ప్లేస్ లో" and the
            # caller got transferred over their no (2026-10-17).
            "వద్దండి, వద్దు, అబ్బే, అవును, కనెక్ట్ చేయండి, జాబ్ ప్లేస్‌మెంట్ ఎక్కడ ఇస్తారు"
        ),
        # Live transfer target when the agent says [[TRANSFER]].
        # A bare B.Tech/Diploma gets the branch question before any fee: a
        # real call (2026-10-18) gave a plain "డిప్లొమా" the EEE fee.
        "branch_ask": {
            # Misheard B.Tech forms too: "డీటెక్ అండి" (2026-10-18) matched no
            # line and the model quoted the practicals fee.
            "needs": r"బీటెక్|బి\.? ?టెక్|b\.? ?tech|డీటెక్|డి టెక్|వీటెక్|బీట్ ఎక్|బీ టెక్|బిటెక్|"
                     r"డిప్లొమా|డిప్లమా|టెప్లమా|diploma|పాలిటెక్నిక్|polytechnic|ఇంజనీరింగ్|engineering",
            # Spoken EEE too: "ట్రిబుల్ ఈ", "త్రిబి", "3BE" were asked again
            # and again on a real call (2026-10-19).
            "given": r"ఈఈఈ|ఈ ఈ ఈ|\bEEE\b|ట్రిబుల్ ?ఈ|ట్రిపుల్ ?ఈ|త్రిబుల్ ?ఈ|త్రిపుల్ ?ఈ|త్రిబి|ట్రిబి|"
                     r"triple ?e|\b3 ?B?E\b|"
                     # "ఈఈ" / "EE", and a lone "ఈ" / "E" as the whole answer to
                     # the branch question (owner, 2026-10-19).
                     r"ఈఈ|ఈ ఈ|\bEE\b|^\W*(ఈ|e)(\s*అండి)?\W*$|ఎలక్ట్రికల్|electrical|మెకానికల్|మెకానిక్|mechanical|సివిల్|civil|సీఎస్ఈ|సీ ఎస్ ఈ|"
                     r"సీఎస్|CSE|కంప్యూటర్|computer|ఈసీఈ|ఈ సీ ఈ|ఈసీ|ఈ సీ|ECE|\bEC\b|ఐటీ|ఐ టీ|\bIT\b|ఆటోమొబైల్|"
                     r"automobile|కెమికల్|chemical|ఏరోనాటికల్|బయోటెక్|బయో టెక్|బ్రాంచ్",
            "line": "ఏ బ్రాంచ్ అండి? ఈఈఈ నా, లేక వేరే బ్రాంచా?",
        },
        # Spoken-text fixes: the model keeps writing ITI as "ఇటీవలి" (recent).
        # The I.E.M fee is chosen in code from what the student said they
        # studied -- the model once quoted the B.Tech fee to an ITI student.
        # First match wins; order matters (other branches before plain EEE).
        "qualification_lines": [
            (r"(బీటెక్|బి\.? ?టెక్|b\.? ?tech|డీటెక్|డి టెక్|వీటెక్|బీ టెక్|బిటెక్|డిప్లొమా|డిప్లమా|టెప్లమా|diploma|ఐటీఐ|ITI).{0,25}"
             # Spoken branch forms too: "బీటెక్ ఈసీ" (ECE) fell through to the
             # EEE fee and a placement promise on a real call (2026-10-18).
             r"(మెకానికల్|మెకానిక్|mechanical|సివిల్|civil|సీఎస్ఈ|సీ ఎస్ ఈ|సీఎస్|CSE|కంప్యూటర్|computer|"
             r"ఈసీఈ|ఈ సీ ఈ|ఈసీ|ఈ సీ|ECE|\bEC\b|ఐటీ |ఐ టీ|\bIT\b|ఫిట్టర్|fitter|వెల్డర్|welder|"
             r"ఆటోమొబైల్|automobile|కెమికల్|chemical|ఏరోనాటికల్|బయోటెక్|బయో టెక్)",
             "మీరు కూడా జాయిన్ అవ్వొచ్చండి. ప్రాక్టికల్స్ కోర్సు ఫీజు పదిహేను వేల రూపాయలు, ఫార్టీ ఫైవ్ డేస్, సర్టిఫికేట్ ఇస్తామండి."),
            (r"ఐటీఐ|ITI|ఐ టి ఐ",
             "ఐటీఐ వాళ్ళకి ఫీజు పదిహేడు వేల రూపాయలండి, కోర్సు ఫార్టీ ఫైవ్ డేస్, హండ్రెడ్ పర్సెంట్ జాబ్ ప్లేస్‌మెంట్ కూడా ఉంటుంది."),
            (r"డిప్లొమా|డిప్లమా|టెప్లమా|diploma|పాలిటెక్నిక్|polytechnic",
             "డిప్లొమా ఈఈఈ వాళ్ళకి ఫీజు పంతొమ్మిది వేల రూపాయలండి, కోర్సు ఫార్టీ ఫైవ్ డేస్, హండ్రెడ్ పర్సెంట్ జాబ్ ప్లేస్‌మెంట్ కూడా ఉంటుంది."),
            # \b around B.E: bare "be" matched inside "October" (2026-10-18).
            (r"బీటెక్|బి\.? ?టెక్|b\.? ?tech|డీటెక్|డి టెక్|వీటెక్|బీ టెక్|బిటెక్|ఇంజనీరింగ్|engineering|బీఈ|\bB\.?E\b",
             "బీటెక్ ఈఈఈ వాళ్ళకి ఫీజు ఇరవై మూడు వేల రూపాయలండి, కోర్సు ఫార్టీ ఫైవ్ డేస్, హండ్రెడ్ పర్సెంట్ జాబ్ ప్లేస్‌మెంట్ కూడా ఉంటుంది."),
            # 10th/SSC joined the practicals course on 2026-10-17 (owner's
            # updated brief: non-technical students welcome at ₹15,000).
            # 7th-9th class joined on 2026-10-18: "సెవెంత్ క్లాస్" matched no
            # line and the model quoted ₹17,000 (the ITI fee) for practicals.
            # Digits only with "class"/"తరగతి" or a word boundary: a date
            # like "27వ తేదీ" or "27th" must never read as 7th class.
            (r"సెవెంత్|ఏడో తరగతి|ఏడవ తరగతి|\b7th|(?<![0-9])7వ తరగతి|ఎయిత్|ఎయిట్త్|\b8th|(?<![0-9])8వ తరగతి|ఎనిమిదో తరగతి|"
             r"నైన్త్|\b9th|(?<![0-9])9వ తరగతి|తొమ్మిదో తరగతి|"
             r"టెన్త్|టెంత్|10th|పదో తరగతి|పదవ తరగతి|ఎస్ఎస్సీ|ssc|"
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
                         # The practicals course is ₹15,000 -- ₹17,000 is ITI's
                         # (spoken for a 7th-class student, 2026-10-18).
                         "ప్రాక్టికల్స్ కోర్సు ఫీజు పదిహేడు వేల": "ప్రాక్టికల్స్ కోర్సు ఫీజు పదిహేను వేల",
                         "చదివారు గారు": "చదివారండి",
                         "పదెనిమిది": "పద్దెనిమిది",  # misspelt 18 (2026-10-18)
                         "7వ తరగతి": "సెవెంత్ క్లాస్", "8వ తరగతి": "ఎయిత్ క్లాస్", "9వ తరగతి": "నైన్త్ క్లాస్",
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
                         # Clock times echoed from the caller as digits
                         # ("మార్నింగ్ 7:30 బ్యాచ్", a real call 2026-10-17).
                         "10:30": "టెన్ థర్టీ", "1:30": "వన్ థర్టీ", "7:30": "సెవెన్ థర్టీ",
                         "8:30": "ఎయిట్ థర్టీ", "3:00": "త్రీ", "6:00": "సిక్స్",
                         "10.30": "టెన్ థర్టీ", "1.30": "వన్ థర్టీ", "7.30": "సెవెన్ థర్టీ", "8.30": "ఎయిట్ థర్టీ",
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
            # Not after ఫారెన్/ఫారిన్: "ఫారెన్ లోన్ ఇస్తారా?" was a misheard
            # foreign-JOB question and got the no-info line (2026-10-18).
            r"(?<!ఫారెన్ )(?<!ఫారిన్ )(?<!ఫారెన్)(?<!ఫారిన్)(?<!ఫారెన్‌)(?<!ఫారిన్‌)లోన్|(?<!foreign )loan|ఏజ్ లిమిట్|age limit|వయస్సు|వయసు|రెండూ కలిపి|both together"
            # Bare "ఏజ్" came out 2026-10-19: "ఏజ్ ఎంత అన్నారు?" was a misheard
            # "ఫీజ్" and got the no-info line.
        ),
        # Tell the model which questions it has already asked twice.
        "repeat_guard": True,
        # Live-transfer target: the admission team, restored 2026-10-19 at
        # the owner's ask (it was the owner's own test phone, +919704123933,
        # from 2026-10-17).
        "transfer_to": "+919154681593",
        # When a caller says yes to the team connect, their details go to
        # this WhatsApp number via the lead-alert edge function (owner's
        # ask, 2026-10-17) -- WaSender internal channel, not a customer
        # WhatsApp number.
        "lead_alert_whatsapp": "+919493918625",
        # The script's fixed transfer sentence -- also triggers the transfer
        # when the model leaves the marker off.
        "transfer_phrase": "టీమ్‌కి కనెక్ట్ చేస్తున్నాను",
        # Background noise ("ఎలా అయిందో", 2026-10-18) cut the batch answer:
        # under three words never stops the bot mid-sentence.
        "min_interrupt_words": 3,
        # An interested student (offered before, never said no) who says
        # thanks/bye hears this once instead of the goodbye; a yes transfers.
        "final_offer": "వెళ్ళే ముందు, జాయిన్ అవ్వడానికి మా అడ్మిషన్ టీమ్‌కి కనెక్ట్ చేయమంటారా అండి?",
        # Sentences that must never be spoken, swapped for the true line
        # (regex, replacement). Online batches exist -- a real call
        # (2026-10-18) said they don't and the student hung up.
        "sentence_fixes": [
            (r"ఆన్\s?‌?లైన్.{0,20}(లేవు|లేవండి|లేదు|లేదండి|ఉండవు|ఉండదు)|ఆఫ్\s?‌?లైన్ క్లాసులే",
             "ఆఫ్‌లైన్, ఆన్‌లైన్ రెండూ ఉన్నాయండి. ఆన్‌లైన్ బ్యాచ్‌లు ఉదయం, సాయంత్రం సెవెన్ థర్టీ నుంచి ఎయిట్ థర్టీ వరకండి."),
        ],
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
