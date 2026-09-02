# Runbook

What to actually do, day to day. `README.md` explains how the system works;
this is the operational companion — onboarding, breakages, and routine checks.

Every troubleshooting entry below is a real failure that happened during this
project, with the symptom as it appeared from the outside.

---

## Onboard a new client

**Time:** ~30 minutes for steps 1–4. Step 5 is the real work.

### 1. Collect from the client

- **Their script** — greeting, prices, what to refuse, what must never be
  invented. The exact wording, not a summary: a paraphrased price is a wrong
  price.
- **What to capture** — orders, bookings, complaints, and which fields.
- **Language and voice.**
- **Business details** for their dashboard record.

> Gundusoodi's prompt is ~23,000 characters and took many rounds of real calls
> to get right. Budget for the script, not for the setup.

### 2. Buy a Vobiz number

[console.vobiz.ai](https://console.vobiz.ai) → buy a number in their region.
**One number per client** — the number is how a call is routed to the right
script.

### 3. Create their customer record

Either they sign up at `app.voicezentech.com` (the `handle_new_user` trigger
creates their `customers` row automatically, `status='pending'`), or create it
yourself. **Note the `customers.id`.**

### 4. Wire it up

```bash
python onboard.py acme --number +91XXXXXXXXXX --customer-id 5 \
                       --agent-name "Acme Clinic Reception"
```

Checks everything first, shows the plan, waits for confirmation, then creates:
Vobiz number assignment, LiveKit inbound + outbound trunks, the dispatch rule
carrying the client metadata, and the `customer_integrations` row.

```bash
python onboard.py acme --status      # what exists so far
python onboard.py acme --rollback    # undo a half-finished run
```

### 5. Write their profile

`onboard.py` leaves a stub at `clients/acme/agent_profile.py` full of `TODO`
markers. Replace them with the real script and tools. Use
`clients/gundusoodi/agent_profile.py` as the worked example.

**The smoke test fails while `TODO` markers remain**, so an unfinished client
cannot reach a caller by accident. That is deliberate.

### 6. Test, then deploy

```bash
python smoke_test.py                       # must pass
docker compose up -d --force-recreate      # on the VPS
```

Then call their number and listen.

---

## Remove a client

```bash
python onboard.py acme --rollback     # trunks, dispatch rule, database row
rm -rf clients/acme                   # the script, only if you mean it
```

Rollback only touches resources `onboard.py` recorded creating — it will never
remove something set up by hand.

---

## Troubleshooting

### Caller hears an engaged tone

**Usually:** no worker was registered when the call arrived.

Deploys take 30–60 seconds because the worker prewarms provider clients before
registering. A call during that window is never answered — LiveKit holds the
SIP call unanswered until an agent is ready, so the carrier gives up.

```bash
docker compose logs | grep "registered worker"     # must be recent
docker ps --filter name=gundusoodi-agent
```

**Also possible:** Vobiz concurrency. The account cap is shared across *all*
clients — the 4th simultaneous caller is refused by the carrier before the
call ever reaches us.

```bash
curl -s -H "X-Auth-ID: $VOBIZ_AUTH_ID" -H "X-Auth-Token: $VOBIZ_AUTH_TOKEN" \
  https://api.vobiz.ai/api/v1/Account/$VOBIZ_AUTH_ID/concurrency
```

### Call connects, then silence

**Usually:** TTS credits ran out. This is the most common outage.

The bot answers, listens and thinks correctly — it just cannot speak. Cartesia
returns `402 Insufficient credits`; short utterances may still succeed while
longer ones fail, so it can look intermittent.

```bash
python smoke_test.py | grep greeting     # "greeting synthesises" fails
```

Fix: top up, or switch `TTS_PROVIDER` in `.env` and restart. Sarvam and
Cartesia both speak Telugu.

**Also possible:** the job crashed at startup. A config value out of range
raises inside the job, not at boot, so the worker looks healthy while every
call dies.

```bash
docker compose logs | grep -i "traceback\|unhandled"
```

`smoke_test.py` exists to catch exactly this before a caller does.

### Bot hears nonsense, or repeats a question

**Usually:** speech recognition, not the prompt.

Check the transcript before changing anything:

```bash
docker compose logs | grep TRANSCRIPT
```

If the caller's words are garbled in the log, the model is answering garbage
correctly and the prompt is not at fault. Measured: Telugu recognition is ~92%
through the phone codec but drops to ~67% with line noise. Krisp
`BVCTelephony` is what closes most of that gap — confirm it is active:

```bash
docker compose logs | grep "noise cancellation"
```

### Replies feel slow

Read the per-turn metrics rather than guessing:

```bash
docker compose logs | grep -E "EOU metrics|LLM metrics|TTS metrics"
```

Healthy on this deployment: `end_of_utterance_delay` ~0.07–0.45s, `llm ttft`
~0.3–1.0s, `tts ttfb` ~0.12s.

> **The usual culprit is not latency at all — it is reply length.** A 33-token
> answer becomes ~8 seconds of speech the caller must sit through. Time to
> first word is ~1.3s; everything after that is the bot talking. Shortening
> answers helps more than any model change.

An endpointing delay set too low is the other classic: 0.07s split one spoken
question into three turns, so the agent kept discarding and restarting.

### Calls work but do not appear in the dashboard

```bash
docker compose logs | grep -E "call logged|not logged"
cat unsent_calls.jsonl        # queued for retry
```

Reporting happens after hang-up and never affects the caller. Failures queue
locally and retry on the next successful send.

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

### LiveKit quota

Runs daily at 09:00 on the VPS. Writes `QUOTA_ALERT.txt` at 90%.

```bash
cat /opt/gundusoodi/QUOTA_ALERT.txt    # exists only if triggered
python usage.py                        # check on demand
```

Free tier is 1,000 agent-minutes/month. **Every call is billed rounded up to a
whole minute**, so short calls cost a full minute each.

### Provider credits

There is no alerting on these — they fail mid-call, as Cartesia did.

- **Cartesia** — 1 credit per character, ~1,100 per call
- **Sarvam** — ₹30 per 10,000 characters (TTS), ₹30/hour (STT)
- **Vobiz** — check the balance; calls stop when it empties

### Before any restart

```bash
python smoke_test.py
```

It gates the deploy for a reason: a bad config value surfaces as a caller
hearing silence, not as an error you would notice.

---

## Useful commands

```bash
# on the VPS, in /opt/gundusoodi
docker compose logs -f                      # live
docker compose logs | grep TRANSCRIPT       # what was said
docker compose ps                           # health
python verify_call.py                       # newest call vs the carrier's CDR

# locally
python demo.py                              # every scenario, no phone call
python smoke_test.py                        # the deploy gate
python onboard.py <client> --status
```

---

## Things that are deliberate, not bugs

- **The greeting cannot be interrupted.** Line noise used to cut it in half.
- **The agent re-asks a field at most once**, then accepts whatever it heard.
  Looping is worse than an imperfect value — the read-back catches mistakes,
  nothing catches a caller who hung up.
- **`call_duration` is stored in seconds**, and billed `ceil(seconds / 60)`.
- **No TTS fallback.** Chaining two engines forced sample-rate resampling that
  degraded every call. The LLM fallback stays, because there the alternative
  is silence.
