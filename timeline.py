import sys, json
rows = []
for line in sys.stdin:
    i = line.find('{"message"')
    if i < 0:
        continue
    try:
        d = json.loads(line[i:])
    except Exception:
        continue
    m = d.get("message", "")
    t = d.get("timestamp", "")[11:23]
    if "received job request" in m:
        rows.append((t, ">>> JOB RECEIVED"))
    elif "job runner initialized" in m:
        rows.append((t, "job runner init elapsed=%s" % d.get("elapsed_time")))
    elif "TTS metrics" in m:
        rows.append((t, "TTS ttfb=%.2f dur=%s" % (d.get("ttfb", 0), d.get("audio_duration"))))
    elif "noise cancellation" in m:
        rows.append((t, m))
for t, m in rows[-14:]:
    print(t, "|", m)
