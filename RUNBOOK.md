# Runbook

What to actually do, day to day. `README.md` explains how the system works;
this is the operational companion — onboarding, breakages, and routine checks.

Every troubleshooting entry below is a real failure that happened during this
project, with the symptom as it appeared from the outside.

> **Architecture note (2026-09-04):** Gundusoodi's live traffic runs on
> `vobiz-native` (`/opt/rumik-agent/vobiz-native`) — no LiveKit, calls talk
> directly to Vobiz's webhook + WebSocket audio API. The original LiveKit
> setup (`gundusoodi-agent`, `onboard.py`, `clients/`) still exists on the
> VPS but is dormant: no number points at its trunk, so it receives no real
> calls. It's left running as a rollback path (see below), not deleted.
> Everything in this runbook now describes `vobiz-native` unless said
> otherwise.

---

## Onboard a new client

**Time:** ~30 minutes for steps 1–5. Step 6 is the real work.

### 1. Collect from the client

- **Their script** — greeting, prices, what to refuse, what must never be
  invented. The exact wording, not a summary: a paraphrased price is a wrong
  price.
- **What to capture** — orders, bookings, complaints, and which fields.
- **Language and voice.**
- **Business details** for their dashboard record.

> Gundusoodi's prompt is ~23,000 characters and took many rounds of real calls
> to get right. Budget for the script, not for the setup.

### 2. Set up their Vobiz side

Either a number on the shared account, or (recommended for real clients) a
**separate Vobiz account per client** — own billing, own concurrency limit,
isolated from everyone else's call volume. See `MULTI_CLIENT_ONBOARDING.md`
for the full account-setup steps.

Either way, end up with:
- A phone number
- A Vobiz **Application** (not a SIP trunk) whose `answer_url` /
  `hangup_url` point at `https://vobiz-native.185-199-52-73.sslip.io/answer`
  and `/hangup` — the same shared server every client uses
- The number assigned to that Application:
  ```
  POST /Account/{auth_id}/numbers/{number}/application
       {"application_id": "..."}
  ```

### 3. Create their customer record

Either they sign up at `app.voicezentech.com` (the `handle_new_user` trigger
creates their `customers` row automatically, `status='pending'`), or create it
yourself. **Note the `customers.id`.**

### 4. Write their script

A new file next to `script_gundusoodi.py`, same shape: `GREETING` and
`INSTRUCTIONS` as module-level strings. Use `script_gundusoodi.py` as the
worked example.

### 5. Register them

Add one entry to `clients_registry.py`, keyed by their dialed number:

```python
"+91XXXXXXXXXX": {
    "client_id": "acme",
    "customer_id": 5,
    "prompt_module": "script_acme",
    "vobiz_auth_id": "...",     # their account's credentials if separate,
    "vobiz_auth_token": "...",  # or the shared ones if not
},
```

No new deployment, no restart of anything else — the registry is read at
call time.

### 6. Test, then deploy

```bash
docker compose build      # on the VPS, in /opt/rumik-agent/vobiz-native
docker compose up -d --force-recreate
```

Then call their number and listen. Check the logs for
`serving client 'acme' (dialed +91XXXXXXXXXX)` to confirm resolution picked
the right one, not the Gundusoodi fallback.

---

## Remove a client

Detach their number from the Application (or release it, if you're done with
it entirely):

```
DELETE /Account/{auth_id}/numbers/{number}/application
```

Then delete their entry from `clients_registry.py` and their script file.
Nothing else needs touching — the shared server keeps running for everyone
else.

---

## Troubleshooting

### Caller hears an engaged tone or "number switched off"

**First, check whether the call ever reached Vobiz at all:**

```bash
curl -s -H "X-Auth-ID: $VOBIZ_AUTH_ID" -H "X-Auth-Token: $VOBIZ_AUTH_TOKEN" \
  "https://api.vobiz.ai/api/v1/Account/$VOBIZ_AUTH_ID/cdr/recent?limit=3"
```

**If there's no CDR entry for the attempt at all** — the call never reached
Vobiz's system. This is upstream of everything on the VPS: the caller's own
carrier, not our infrastructure. Retry after a few minutes (carriers
sometimes cache a stale "unreachable" result); try a different phone/SIM to
isolate it.

**If the CDR shows the call but no answer**, or a `trunk_id` populated
instead of routing through the Application — the number's routing may have
reverted or not fully propagated:

```bash
curl -s -H "X-Auth-ID: $VOBIZ_AUTH_ID" -H "X-Auth-Token: $VOBIZ_AUTH_TOKEN" \
  https://api.vobiz.ai/api/v1/Account/$VOBIZ_AUTH_ID/numbers | \
  python3 -c "import sys,json; [print(n['e164'], n.get('trunk_group_id'), n.get('application_id')) for n in json.load(sys.stdin)['items']]"
```

Exactly one of `trunk_group_id` / `application_id` should be set, and it
should be `application_id` for anything on `vobiz-native`. Re-run the
detach/attach from step 2 above if it's wrong.

**Also check the server itself is up:**

```bash
docker ps --filter name=vobiz-native
docker logs --tail 30 vobiz-native
```

### Call connects, then silence, or the bot never replies to a question

**Check `llm_completion_tokens` in the call's usage summary** — if it's `0`
after the greeting, the LLM was never invoked for the caller's turn even
though STT heard them. This has happened; no confirmed root cause yet, watch
for a repeat.

**Also possible: TTS credits ran out** (Raya or Sarvam). The bot answers,
listens correctly, and just can't speak.

```bash
docker logs --since 30m vobiz-native | grep -iE "error|402|insufficient"
```

**Also possible: the container crashed on startup** — a bad `voice_id` for
the configured `model` (Raya's Telugu voices are all `standard`, not `m1`;
passing the wrong one 404s) or a missing env var raises inside a call, not at
boot.

```bash
docker logs --since 1h vobiz-native | grep -iE "traceback|error|exception"
```

### Bot hears nonsense, or repeats a question

**Usually:** speech recognition, not the prompt.

```bash
docker logs vobiz-native | grep "caller:"
```

If the caller's words are garbled in the log, the model is answering garbage
correctly and the prompt is not at fault. `STT_PROMPT` in `server.py` biases
recognition toward domain vocabulary — extend it per-client if a second
client's brand name gets consistently misheard the way "గుండుసూది" did.

### Replies feel slow, or the call takes a while to "lift" after ringing

```bash
docker logs vobiz-native | grep -E "TTS metrics|answer:|hangup:"
```

Two separate things bundle into this complaint:

- **Ring-to-answer delay** (Vobiz→our server handoff) — measured ~5-7s on a
  healthy call. This is carrier/platform signaling before our code even
  runs; not fixable from here.
- **Reply length** — the actual dominant cost once the call is live. A long
  scripted answer is several seconds of audio the caller must sit through.
  Shortening answers helps more than any latency tuning.

### Calls work but do not appear in the dashboard

```bash
docker logs vobiz-native | grep -E "call logged|call report failed|queued for retry"
```

Reporting happens after hang-up (`voicezen.py`'s `report_call_async`, reused
unchanged from the LiveKit agent — no LiveKit dependency in that module) and
never affects the caller. Failures queue locally in `unsent_calls.jsonl` and
retry on the next successful send.

### Payment fails with "not registered"

Razorpay live mode only accepts payments from **registered domains**. A Vercel
preview URL will never be registered. Pay from `app.voicezentech.com`, and
register the domain under Razorpay → Settings → Websites & API Keys.

Live activation also requires Terms, Privacy, Refund/Cancellation, Contact and
Pricing pages to be publicly reachable on that domain.

### Dashboard shows "Invalid login credentials" for a valid account

The deployed bundle is pointing at the wrong Supabase project. Vite compiles
`VITE_SUPABASE_URL` in **at build time**, so changing `.env.local` does
nothing to a deployment — the variables must be set in Vercel and the project
redeployed with the build cache disabled.

```bash
curl -s https://app.voicezentech.com/assets/index-*.js | grep -o 'https://[a-z]*\.supabase\.co'
```

---

## Routine checks

### Provider credits

There is no alerting on these — they fail mid-call.

- **Raya (TTS)** — check balance via their dashboard
- **Sarvam** — ₹30 per 10,000 characters (TTS), ₹30/hour (STT)
- **Gemini** — pay-as-you-go, no prepaid balance to run out of
- **Vobiz** — check the balance; calls stop when it empties, per-account if
  clients are on separate Vobiz accounts

### Before any restart

```bash
docker logs vobiz-native --tail 50   # nothing already broken before you touch it
```

There's no `smoke_test.py` equivalent for `vobiz-native` yet — worth building
once there's more than one client, so a bad script doesn't reach a caller
before anyone notices.

---

## Useful commands

```bash
# on the VPS, in /opt/rumik-agent/vobiz-native
docker compose logs -f                      # live
docker logs vobiz-native | grep "caller:\|bot:"   # what was said
docker compose ps                           # health

# Vobiz CDR for a specific number
curl -s -H "X-Auth-ID: $VOBIZ_AUTH_ID" -H "X-Auth-Token: $VOBIZ_AUTH_TOKEN" \
  "https://api.vobiz.ai/api/v1/Account/$VOBIZ_AUTH_ID/cdr/recent?limit=5"
```

---

## Things that are deliberate, not bugs

- **The agent re-asks a field at most once**, then accepts whatever it heard.
  Looping is worse than an imperfect value — the read-back catches mistakes,
  nothing catches a caller who hung up.
- **`call_duration` is stored in seconds**, and billed `ceil(seconds / 60)`.
- **No TTS fallback.** Chaining two engines forced sample-rate resampling that
  degraded every call in the LiveKit version; the same reasoning applies here.
- **Client resolution falls back to Gundusoodi** if a dialed number isn't in
  `clients_registry.py`, rather than failing the call — an unregistered
  number answering with the wrong-but-known script is safer than dead air.

---

## The LiveKit path (dormant, not deleted)

`gundusoodi-agent`, `onboard.py`, `clients/`, and the Vobiz SIP trunk still
exist and still work — they just have no number pointed at them right now.
To roll back to LiveKit for a given number:

```
DELETE /Account/{auth_id}/numbers/{number}/application
POST   /Account/{auth_id}/numbers/{number}/assign
       {"trunk_group_id": "8a94fdf6-a2d1-46cf-9181-39757020b94f"}
```

Everything in the original version of this runbook (LiveKit quota checks,
`smoke_test.py`, `onboard.py --status/--rollback`) still applies to that
path — see git history for the pre-2026-09-04 version of this file if it's
ever brought back into active use.
