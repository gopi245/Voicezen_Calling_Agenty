import sys, json
tts_sec = 0.0; stt_sec = 0.0; pin = 0; pout = 0; calls = set()
for line in sys.stdin:
    i = line.find('{"message"')
    if i < 0: continue
    try: d = json.loads(line[i:])
    except Exception: continue
    m = d.get("message", "")
    if d.get("job_id"): calls.add(d["job_id"])
    if "TTS metrics" in m: tts_sec += float(d.get("audio_duration") or 0)
    elif "STT metrics" in m: stt_sec += float(d.get("audio_duration") or 0)
    elif "LLM metrics" in m:
        pin += int(d.get("prompt_tokens") or 0)
        pout += int(d.get("completion_tokens") or 0)
n = max(len(calls), 1)
tts_chars = tts_sec / 60 * 750  # ~750 chars per minute of speech
print("calls in window     : %d" % n)
print("TTS audio           : %.0f s   (~%.0f chars)" % (tts_sec, tts_chars))
print("STT audio           : %.0f s" % stt_sec)
print("LLM tokens          : %d in / %d out" % (pin, pout))
print()
print("PER CALL AVERAGE")
print("  TTS audio         : %.0f s  (~%.0f chars)" % (tts_sec/n, tts_chars/n))
print("  STT audio         : %.0f s" % (stt_sec/n))
print("  LLM tokens        : %d in / %d out" % (pin/n, pout/n))
