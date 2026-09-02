"""Cross-check the newest call_logs row against the carrier's own CDR.

The agent measures duration itself; Vobiz measures it independently. If those
disagree, billing is wrong -- so this compares them rather than trusting the
agent's number.
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()
import httpx  # noqa: E402

# Credentials come from .env (gitignored) so this file can be committed.
REF = os.getenv("VOICEZEN_PROJECT_REF", "sewpzeymsccrehnjvmib")
ANON = os.environ["VOICEZEN_ANON_KEY"]

tok = httpx.post(
    f"https://{REF}.supabase.co/auth/v1/token?grant_type=password",
    headers={"apikey": ANON, "Content-Type": "application/json"},
    json={
        "email": os.environ["VOICEZEN_LOGIN_EMAIL"],
        "password": os.environ["VOICEZEN_LOGIN_PASSWORD"],
    },
    timeout=60,
).json()["access_token"]
H = {"apikey": ANON, "Authorization": f"Bearer {tok}"}

c = httpx.get(
    f"https://{REF}.supabase.co/rest/v1/call_logs?select=*&order=id.desc&limit=1",
    headers=H, timeout=60,
).json()[0]

print("=== NEWEST call_logs ROW (through your login, via RLS) ===")
for k in ("id", "customer_id", "integration_id", "engine", "call_duration",
          "caller_number", "call_status", "language", "created_at"):
    print(f"  {k:16} = {c.get(k)}")
print(f"  captured_data    = {json.dumps(c.get('captured_data'), ensure_ascii=False)[:250]}")
print(f"  transcript chars = {len(c.get('transcript') or '')}")
print(f"  call_summary     = {(c.get('call_summary') or '(none)')[:400]}")

a = os.getenv("VOBIZ_AUTH_ID")
h = {"X-Auth-ID": a, "X-Auth-Token": os.getenv("VOBIZ_AUTH_TOKEN")}
r = httpx.get(f"https://api.vobiz.ai/api/v1/Account/{a}/Call",
              headers=h, params={"limit": 1}, timeout=60).json()
call = r["objects"][0]
print("\n=== VOBIZ CDR (carrier's independent record) ===")
for k in ("from_number", "to_number", "answer_time", "end_time", "bill_duration"):
    print(f"  {k:16} = {call.get(k)}")

agent_sec = c.get("call_duration") or 0
carrier_sec = call.get("bill_duration") or 0
print(f"\nagent={agent_sec}s  carrier={carrier_sec}s  diff={abs(agent_sec - carrier_sec)}s")
