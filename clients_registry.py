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
        # Vedantha Vanam's line since 2026-10-19 (owner's ask): the number
        # moved to the client's own account (ngcekranthi@gmail.com) and the
        # VoiceZen hooks went with the old client_id -- the website-lead
        # outbound forcing and the lead ring-back path both key on
        # client_id == "voicezen", so neither touches this number any more.
        # VoiceZen itself now has NO number: its website callbacks and
        # campaigns dial out as DIAL_FROM_NUMBER until it gets a new one
        # (accepted by the owner, "no problem for now").
        "client_id": "vedantha_vanam",
        # Customer #25: Vedantha Vanam's own dashboard account
        # (ngcekranthi@gmail.com, Starter plan), created 2026-10-19 -- calls
        # bill its 250 monthly minutes. The env var overrides if the account
        # is ever re-made.
        "customer_id": (
            int(os.environ["VEDANTHA_VANAM_CUSTOMER_ID"])
            if os.getenv("VEDANTHA_VANAM_CUSTOMER_ID", "").strip().isdigit()
            else 25
        ),
        "dial_from": "+918071580257",
        # Dashboard campaigns on this line's own script (campaign_rules.py):
        # a bare "hello" right after the greeting gets this short re-intro.
        "outbound_hello_line": "నమస్కారం సార్, వేదాంత వనం ప్రాజెక్ట్ నుంచి మాట్లాడుతున్నాను. రెండు నిమిషాలు మాట్లాడవచ్చా సార్?",
        # The newer call engine, explicitly: engine selection used to ride on
        # client_id == "voicezen", which this entry no longer carries.
        "engine": "new",
        # Gemini 3.5 Flash-Lite answers this line (owner's ask, 2026-10-20),
        # as the model itself -- not as Groq's fallback.
        "llm_model": "gemini-3.5-flash-lite",
        # INBOUND calls hear the Vedantha Vanam sandalwood-farmland script
        # (owner's request, 2026-10-18; replaced the Hindi real-estate demo,
        # script_realestate_hi, which stays in agent/ for rollback).
        "prompt_module": "script_vedantha_vanam",
        # A faint office behind the whole call. Re-enabled 2026-10-15 after
        # the dead-air cause was fixed (a stalled TTS request had no
        # first-byte deadline or retry; websocket sends now serialized).
        "background_noise": None,  # off 2026-10-19: the ambience clip contains real speech (owner's ask)
        # SWITCH: RNNoise noise cancellation on the caller's audio -- a trial
        # from 2026-10-19 (owner's ask). Set to None to turn it off.
        "noise_cancellation": "rnnoise",
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
        # Busy before a visit is booked: a polite close (audit 2026-10-19 got
        # "మళ్లీ కాల్ చేస్తారా?" and no goodbye).
        "busy_line": "సరేనండి, ఇబ్బంది లేదు. మీకు వీలైనప్పుడు ఈ నంబర్‌కి కాల్ చేయండి. ధన్యవాదాలు, నమస్కారం!",
        # The visit isn't pushed until the benefits were told: an early
        # "ఏ రోజు?" becomes the purpose question (client feedback 2026-10-19).
        "benefit_markers": ["మంచి అవకాశం", "అనుకూలమైన భూమి"],
        # Questions answered with a fixed line by the server (regex, line).
        # Location: the model gave only Narasaraopet; JNTU (7 km) is the
        # owner's lead point (2026-10-19). "సైజ్ ఎక్కడ" is a misheard "సైట్".
        # The whole greeting always plays: nothing the caller says during it
        # replaces it (a real test, 2026-10-19, never heard who was calling).
        "protect_greeting": True,
        "fixed_answers": [
            # "Who is this / why did you call?" -> the greeting again: a real test
            # caller asked "ఎవరండీ?" three times (2026-10-19).
            (r"ఎవర(ు|ండి|ండీ)|మీరెవరు|ఎవరు మాట్లాడ|ఎక్కడి నుంచి (మాట్లాడ|కాల్)|ఎందుకు కాల్ చేశార|ఏ కంపెనీ|who is this|who are you",
             "నమస్కారం సార్! నేను వేదాంత వనం ప్రాజెక్ట్ నుంచి మాట్లాడుతున్నాను. మీకు రియల్ ఎస్టేట్ లేదా వ్యవసాయ భూములలో పెట్టుబడి పెట్టే ఆసక్తి ఉందని మీకు మా ప్రాజెక్ట్ వివరాలు తెలియజేయడానికి కాల్ చేశాను. ఇప్పుడు రెండు నిమిషాలు మాట్లాడవచ్చా సార్?"),
            # Risk / trust: a fixed, honest line -- an audit (2026-10-19) had the
            # model say "ప్లాట్ ధర, రిజిస్ట్రేషన్, పాస్‌బుక్ అన్నీ హామీ".
            (r"నమ్మకం|నమ్మక|రిస్క్|మోసం|ఫ్రాడ్|నిజమేనా|సేఫ్ నా|సేఫా|సురక్షితమేనా|గ్యారంటీ|హామీ",
             "శ్రీగంధం ఆదాయానికి ఖచ్చితమైన హామీ ఇవ్వలేమండి, అది మొక్కల పెరుగుదల, మార్కెట్ ధర మీద ఆధారపడి ఉంటుంది. "
             "భూమి మీ పేరు మీద రిజిస్టర్ చేసి పాస్‌బుక్ ఇస్తాము, డాక్యుమెంట్స్ అన్నీ సైట్ విజిట్‌లో మా టీమ్ చూపిస్తారండి. "
             "ఒకసారి సైట్ చూసి నిర్ణయం తీసుకోవచ్చు."),
            # "What is the site visit for?" -- a real customer asked twice and
            # never got it (2026-10-19). Before the location rule, because
            # "ఎక్కడికి విజిట్" also mentions where.
            (r"^\W*(దేనికి|దీనికి|ఎందుకు)\W*$|(దేనికి|దీనికి|ఎందుకు|ఏంటి|ఏమిటి)\s*.{0,20}(విజిట్|రావాలి|రావాల|రమ్మంటున్నారు)|"
             r"విజిట్\s*.{0,12}(దేనికి|దీనికి|ఎందుకు|అంటే ఏంటి|అంటే ఏమిటి)",
             "సైట్ విజిట్ అంటే మా వేదాంత వనం భూమిని మీరు ప్రత్యక్షంగా వచ్చి చూడటం అండి. భూమి, శ్రీగంధం మొక్కలు, "
             "ఫెసిలిటీస్, డాక్యుమెంట్స్ అన్నీ మా టీమ్ చూపిస్తారు. సైట్ సంతమాగులూరు దగ్గర, జె ఎన్ టి యు కి ఏడు "
             "కిలోమీటర్లు. మీకు ఏ రోజు వీలవుతుందో చెప్తారా అండి?"),
            (r"(సైట్|సైజ్|ల్యాండ్|భూమి|ప్రాజెక్ట్|ప్లేస్)\s*(ఎక్కడ|ఎటు)|ఎక్కడ ఉంది|లొకేషన్|location|"
             r"ఎంత దూరం|ఎన్ని కిలోమీటర్|దగ్గర్లో ఏమ|దగ్గరలో ఏమ|అడ్రస్",
             "మా సైట్ సంతమాగులూరు గ్రామం, బాపట్ల జిల్లా పరిసరాల్లో ఉందండి. జె ఎన్ టి యు కి ఏడు కిలోమీటర్లు, "
             "కోటప్పకొండకి పది కిలోమీటర్లు, నరసరావుపేట టౌన్‌కి పద్నాలుగు కిలోమీటర్లు. లొకేషన్ మీకు ఈ నంబర్‌కి పంపిస్తామండి."),
        ],
        # A lower amount / can't pay / discount ask is answered by the server
        # -- "సిక్స్ లాక్స్ కావాలి, సెవెన్ ఇవ్వలేను" got "సరే అండి" (heard as
        # agreeing to ₹6 lakh) on a real call, 2026-10-19. The price never
        # moves on the phone; the team handles payment.
        "price_objection": {
            "rx": r"(సిక్స్|ఫైవ్|ఆరు|ఐదు|నాలుగు|six|five|(?<![0-9])[1-6])\s*(లాక్స్|లాక్|లక్ష|లక్షలు|లక్షల|lakhs?|lacs?)|"
                  r"ఇవ్వలేను|ఇవ్వలేము|కట్టలేను|కట్టలేము|పెట్టలేను|తగ్గించ|తగ్గిస్త|తగ్గదా|తక్కువ చేయ|"
                  r"ఎక్కువ (ధర|రేటు)|(ధర|రేటు|రేట్) ఎక్కువ|ఖరీదు|డిస్కౌంట్|discount|బేరం|ఆఫర్ ఉందా|"
                  r"అంత (డబ్బు|అమౌంట్) లేదు|బడ్జెట్ (లేదు|తక్కువ)|"
                  # "రేట్ ఏమన్నా (తగ్గుతుందా)" -- got the no-info line (2026-10-19).
                  r"(రేట్|రేటు|ధర|కాస్ట్)\s*(ఏమన్నా|ఏమైనా|ఏమన్న)|తక్కువ (ధర|రేటు|రేట్)",
            "first": "ధర పన్నెండు న్నర సెంట్ల ప్లాట్‌కి ఏడు లక్షల రూపాయలేనండి. పేమెంట్ ఆప్షన్స్, ఏమైనా ఆఫర్స్ ఉంటే "
                     "సైట్ విజిట్‌లో మా టీమ్ వివరంగా చెప్తారండి. ఒకసారి సైట్ చూసి నిర్ణయం తీసుకోవచ్చు.",
            # Never a refusal either ("తగ్గించలేము" is banned -- owner, 2026-10-18).
            "again": "అర్థమైందండి. మీ బడ్జెట్ గురించి మా టీమ్ మీకు కాల్ చేసి పేమెంట్ ఆప్షన్స్ చెప్తారండి.",
        },
        "purpose_question": "మీరు పెట్టుబడి కోసం చూస్తున్నారా, లేక భవిష్యత్తులో వ్యవసాయం కోసం భూమి కావాలా అండి?",
        # Known-false sentences swapped before they are spoken: a demo
        # doubled the plant count ("రెండు వంద శ్రీగంధం మొక్కలు", 2026-10-19).
        "sentence_fixes": [
            (r"రెండు రెట్లు|రెండు వంద|రెండు వందల (శ్రీగంధం )?మొక్క|యాభై శ్రీగంధం మొక్కలు పాతిక",
             "పాతిక సెంట్లకు వంద శ్రీగంధం మొక్కలండి."),
            # Never a guarantee the client did not give (audit 2026-10-19).
            (r"అన్నీ హామీ|హామీ ఇస్తాం|హామీ ఇస్తాము|గ్యారంటీ ఇస్తాం|గ్యారంటీ ఉంది|హామీ ఉంది|రిస్క్ లేదు|రిస్క్ ఉండదు|నష్టం ఉండదు",
             "డాక్యుమెంట్స్ అన్నీ సైట్ విజిట్‌లో మా టీమ్ చూపిస్తారండి."),
            # The passbook timing is never invented: "పాస్‌బుక్ రిజిస్ట్రేషన్
            # తర్వాత ఇస్తాం" was said twice on a real call despite the script.
            (r"పాస్‌?బుక్.{0,30}(తర్వాత|వెంటనే|రోజుల్లో|నెలలో|వారంలో)",
             "పాతిక సెంట్లు రిజిస్టర్ చేసి పాస్‌బుక్ ఇస్తారండి."),
            # No suggested days -- the visit day is the caller's own (owner,
            # 2026-10-19: "శనివారం లేదా ఆదివారం" was being offered).
            (r"శనివారం లేదా ఆదివారం|ఆదివారం లేదా శనివారం|శనివారమా,? ఆదివారమా|వీకెండ్‌లో రాగలరా|వీకెండ్ వీలవుతుందా",
             "మీకు ఏ రోజు వీలవుతుందో చెప్తారా అండి?"),
        ],
        # A visit-day word in the caller's words: the day is settled and the
        # day question is not asked again.
        "visit_day_rx": r"రేపు|రేపై|ఎల్లుండి|ఆదివారం|సోమవారం|మంగళవారం|బుధవారం|గురువారం|శుక్రవారం|శనివారం|"
                        r"వచ్చే వారం|ఈ వారం|ఒక వారం|రెండు వారా|వారం తర్వాత|వారం రోజుల|నెక్స్ట్ వీక్|"
                        r"వచ్చే నెల|నెల తర్వాత|tomorrow|sunday|monday|saturday",
        # Words that make a caller turn about THIS call. A turn with none of
        # them (and not a yes/no, bye, day or name) is treated as noise or
        # background people and gets "please say that again" (2026-10-19).
        "relevance_rx": (
            r"ప్లాట్|సెంట|సెంటు|గజ|ఎకర|ధర|రేటు|రేట్|కాస్ట్|ఫాస్ట్ ఎంత|ఎంత|లక్ష|లాక్|డబ్బు|అమౌంట్|పేమెంట్|ఈఎంఐ|EMI|లోన్|"
            r"డిస్కౌంట్|ఆఫర్|తగ్గ|ఎక్కువ|సైట్|సైజ్|విజిట్|వస్తా|వస్తాను|వస్తాము|రాగల|రాలేను|వీలు|వీలవు|కుదర|కుదురు|"
            r"లొకేషన్|ఎక్కడ|దూరం|కిలోమీటర్|జె ?ఎన్ ?టి ?యు|నరసరావుపేట|కోటప్పకొండ|సంతమాగులూరు|బాపట్ల|"
            r"శ్రీగంధం|గంధం|చందనం|మొక్క|చెట్|ప్లాంట్|కోత|ఆదాయం|లాభం|రిటర్న్|షేర్|శాతం|"
            r"భూమి|ల్యాండ్|పొలం|వ్యవసాయ|పెట్టుబడి|పెట్టు|ఇన్వెస్ట్|పాస్‌?బుక్|రిజిస్ట్రేషన్|రిజిస్టర్|డాక్యుమెంట్|"
            # Misheard "పెట్టుబడి" and plain wanting-words: "ఎటుబడి కోసం
            # కావాలండి" was taken as noise on a real test (2026-10-19).
            r"బడి|ఎటుబడి|కోసం|కావాల|కావాలి|దగ్గర|దగ్గరలో|పైడి|"
            # Upkeep and the twelve years: "పన్నెండేళ్ళ వరకు మీరే
            # చూసుకుంటారా?" -- the core promise of the project -- was taken
            # as noise (sim, 2026-10-20).
            r"సంవత్సర|ఏళ్ళ|ఏళ్ల|పన్నెండ|చూసుకుంటార|చూసుకుంట|పెంపకం|నిర్వహణ|కాపాడ|ఖర్చు|బాధ్యత|"
            # Someone else holding the phone, and thinking-it-over.
            r"లేరు|లేడు|ఇంట్లో|భార్య|భర్త|కొడుకు|కూతురు|ఆలోచించి|చూద్దాం|"
            # Approvals, water, fencing -- things a land buyer always asks.
            r"అప్రూవ|ఎల్ ?ఆర్ ?ఎస్|LRS|నీళ్|నీరు|బోరు|బోర్|ఫెన్సింగ్|కరెంట్|రోడ్|"
            # Question words: "దేనికి?" (what's the visit for?) was taken as
            # noise on a real call (2026-10-19).
            r"దేనికి|దీనికి|ఎందుకు|ఏంటి|ఏమిటి|ఏంటండి|ఎవర|ఏది|ఎలా|ఎప్పుడు|ఎక్కడికి|"
            # Trust and "who am I talking to" -- "ఇది నమ్మకమేనా? రిస్క్ ఉంటుందా?",
            # "గవర్నమెంట్ అప్రూవల్ ఉందా?" and "మీరు మనిషా రోబోనా?" were taken as
            # noise on an audit (2026-10-19).
            r"నమ్మకం|నమ్మక|రిస్క్|సేఫ్|సురక్షిత|మోసం|ఫ్రాడ్|నిజమేనా|గ్యారంటీ|గవర్నమెంట్|ప్రభుత్వ|అప్రూవల్|పర్మిషన్|"
            r"లీగల్|టైటిల్|క్లియర్|మనిషి|మనిషా|రోబో|ఏ ఐ|AI|కంప్యూటర్|రికార్డ్|మేడం|సార్|"
            r"ఫెసిలిటీ|సౌకర్యం|రోడ్|నీళ్|నీరు|బోర్|సోలార్|సెక్యూరిటీ|ఫెన్సింగ్|రిసార్ట్|పూల్|బోటింగ్|"
            r"వేదాంత|ప్రాజెక్ట్|కంపెనీ|టీమ్|కాల్|ఫోన్|నంబర్|నెంబర్|పేరు|వివరాలు|డీటెయిల్|ఇన్సూరెన్స్|"
            r"అవును|అవునండి|లేదు|లేదండి|వద్దు|కాదు|ఇంట్రెస్ట్|బిజీ|తర్వాత|ఆలోచించ|చెప్పండి|చెప్పు|మాట్లాడండి|"
            r"మాట్లాడ|హలో|వినపడ|అర్థం|సరే|ఓకే|ok|thank|థాంక్|థ్యాంక్|బై|నమస్కారం|కష్టం|చెప్పా|చెప్పాను"
        ),
        # The price per cent, for the server's size answers ("50 సెంట్లు
        # అయితే ఎంత?" -> ఇరవై ఎనిమిది లక్షలు).
        "price_per_cent": 56000,
        # Numbers the model writes as digits, spoken as words.
        "speech_fixes": {"పన్నెండున్నర": "పన్నెండు న్నర",
                         "25 సెంట్ల": "పాతిక సెంట్ల", "25 సెంట్లు": "పాతిక సెంట్లు", "12.5 సెంట్ల": "పన్నెండు న్నర సెంట్ల",
                         "12.5": "పన్నెండు న్నర", "12. 5": "పన్నెండు న్నర", "100 శ్రీగంధం": "వంద శ్రీగంధం",
                         "14 లక్షల": "పద్నాలుగు లక్షల",
                         # The 100 plants and the passbook are per పాతిక (25)
                         # cents: a replay said "పన్నెండు న్నర సెంట్లకు వంద".
                         "రేపై అండి": "రేపు అండి",
                         "పన్నెండు న్నర సెంట్లకు వంద": "పాతిక సెంట్లకు వంద",
                         "పన్నెండున్నర సెంట్లకు వంద": "పాతిక సెంట్లకు వంద",
                         "పన్నెండు న్నర సెంట్లు రిజిస్టర్": "పాతిక సెంట్లు రిజిస్టర్",
                         "పన్నెండున్నర సెంట్లు రిజిస్టర్": "పాతిక సెంట్లు రిజిస్టర్", "100 మొక్క": "వంద మొక్క",
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
        # Customer #24: SR Electrical's own dashboard account
        # (info@srelectricaltrainingcenter.com, Starter plan), created
        # 2026-10-19 -- calls bill its 250 monthly minutes, not VoiceZen's.
        # The env var still overrides if the account is ever re-made.
        "customer_id": (
            int(os.environ["SR_ELECTRICAL_CUSTOMER_ID"])
            if os.getenv("SR_ELECTRICAL_CUSTOMER_ID", "").strip().isdigit()
            else 24
        ),
        "dial_from": "+918065353946",
        "prompt_module": "script_sr_electrical",
        # A faint office behind the whole call. Re-enabled 2026-10-15 after
        # the dead-air cause was fixed (a stalled TTS request had no
        # first-byte deadline or retry; websocket sends now serialized).
        "background_noise": None,  # off 2026-10-19: the ambience clip contains real speech (owner's ask)
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
        # "ఫీజు ఎక్కువ" is answered without repeating the fee and without a
        # refusal; discounts stay with the team (audit 2026-10-19).
        "price_objection": {
            "rx": r"(ఫీజు|ఫీజ్|ఫీస్|రేటు|రేట్)\s*(చాలా\s*)?ఎక్కువ|ఎక్కువ\s*(ఫీజు|ఫీజ్|అవుతుంది)|తగ్గించ|తగ్గిస్త|"
                  r"కట్టలేను|కట్టలేము|ఇవ్వలేను|అంత (డబ్బు|అమౌంట్) లేదు",
            "first": "అర్థమైందండి. ఫీజు, పేమెంట్ ఆప్షన్స్ గురించి మా అడ్మిషన్ టీమ్ వివరంగా మాట్లాడతారండి.",
            "again": "అర్థమైందండి. మీ బడ్జెట్ గురించి మా అడ్మిషన్ టీమ్‌తో మాట్లాడొచ్చండి.",
        },
        # Fixed answers said by the server (regex, line). Next batch / start
        # date: the admission team has the dates -- the owner's line, with the
        # connect offer (2026-10-19). After a "no" the offer is left off.
        "fixed_answers": [
            (r"(నెక్స్ట్|తర్వాత|తరువాత|కొత్త|న్యూ|next|new)\s*బ్యాచ్|next batch|"
             r"బ్యాచ్\s*(ఎప్పుడు|ఎప్పటి|ఎప్పటినుంచి|స్టార్ట్|మొదలు|ప్రారంభ|డేట్|తేదీ)|"
             r"(క్లాసెస్|క్లాస్|కోర్సు|ట్రైనింగ్)\s*(ఎప్పుడు|ఎప్పటి నుంచి|ఎప్పటినుంచి)\s*(స్టార్ట్|మొదలు|ప్రారంభ|స్టార్ట)|"
             r"ఎప్పుడు (స్టార్ట్|మొదలు) (అవుతుంది|అవుతాయి)|ఎప్పటి నుంచి (బ్యాచ్|క్లాసెస్|క్లాస్)",
             "తర్వాత బ్యాచ్ వివరాలు మా అడ్మిషన్ టీమ్ చెప్తారండి. కాల్ కనెక్ట్ చేయమంటారా?"),
        ],
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
