"""VoiceZen's Hindi-first line (+918065353946, Gundusoodi's former voice
number, moved to VoiceZen on 2026-10-08).

The same VoiceZen script as the Telugu line -- one set of rules to keep
right -- with a Hindi welcome and Hindi as the starting language. The
per-turn language note in server.py still answers each caller in the
language they speak.
"""

from script_voicezen import INSTRUCTIONS as _BASE

GREETING = (
    "नमस्ते, VoiceZen में आपका स्वागत है। "
    "क्या आप AI calling agent के बारे में जानना चाहेंगे, या demo चाहिए जी?"
)

INSTRUCTIONS = _BASE + """

THIS IS VOICEZEN'S HINDI LINE. The welcome was said in Hindi. Reply in Hindi unless the caller's latest message is clearly in another language, and always use the Hindi version of every example line above (the Telugu lines only show the meaning). In Hindi use जी, never అండి. Keep English business words (business, AI, agent, demo, WhatsApp, calls) as they are."""
