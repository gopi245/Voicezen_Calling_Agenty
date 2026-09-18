# Admin dashboard — build plan

> **Status (2026-09-18): built and live on app.voicezentech.com, admin login
> only.** All four stages below are done, plus WhatsApp template management,
> "Add client" in one step, and a client's own Vobiz account (keys encrypted
> in Supabase Vault). Gundusoodhi and VoiceZen still run on their code
> scripts, default AI settings and the magazine WhatsApp bot until switched
> per client from the admin pages. The plan below is kept as the record of
> how it was designed.

A brief for a fresh working session. Read this first, then `GO_LIVE_STATE.md`
for how the live system is wired today. Written 2026-09-18.

## What we are building

One admin login (the owner, `admin@voicezentech.com`) that can switch between
clients and, per client, manage:

1. **Call script** — the greeting and instructions the voice agent speaks
   from, with every save kept as a version, one-click rollback, and a test
   call before a change goes live.
2. **AI stack** — speech-to-text provider and language, LLM model, TTS
   provider and voice, whether the agent follows the caller's language — with
   the per-minute cost of each option shown next to it.
3. **WhatsApp bot content** — welcome, menu options, payment numbers, team
   contact, FAQs. Today the bot is written for Gundusoodhi only and every
   WhatsApp number gets the magazine replies; this fixes that.
4. **Usage and history** — calls, minutes, WhatsApp chats, orders and cost per
   client, plus a log of who changed which setting or script and when.

Changes take effect on the next call, with no redeploy.

## Rules that must not be broken

These come from the owner and have been restated more than once. Treat them
as hard constraints, not preferences.

- **The existing system keeps running untouched throughout.** Gundusoodhi and
  VoiceZen stay on today's code-based settings until the owner explicitly
  switches each one. Nothing live is replaced; the new path is built beside it.
- **Database changes are additive only.** New tables and new columns; never
  drop, rename or repurpose an existing one. Unused columns stay as NULL.
- **Draft the exact SQL and get the owner's sign-off before running any
  database change**, including data seeding.
- **Get approval before changing working code.** Diagnose and propose first.
- **Do not change the call scripts.** Gundusoodhi's and VoiceZen's scripts
  move into the database **word for word** as version 1. After that the owner
  edits them through the admin page; nobody rewrites them unasked.
- **Nothing may start sending messages or placing calls on its own** without
  the owner's explicit go-ahead. Build senders unscheduled.
- **Do not rename** `customer_integrations.engine = 'livekit'` or drop
  `dograh_workflow_id`. Both look like leftovers and both are load-bearing:
  `log-call` resolves the customer by `engine = 'livekit'`, and new onboarding
  writes `"livekit:<client>"` into `dograh_workflow_id`.
- **Work on a git branch**, and prove everything on a test client and test
  number before any real client is switched.

## Where things live today

**Voice agent** — repo `C:\Downloads\own_calling agnet`
(github.com/gopi245/gundusoodi-voice-agent, branch `main`)

- `vobiz_native_server.py` — the live agent. Deployed on the VPS as
  `/opt/rumik-agent/vobiz-native/server.py`; the two are byte-identical
  (checked 2026-09-18), only the filename differs.
- `clients_registry.py` — hard-coded map of dialled number → client:
  `client_id`, `customer_id`, `dial_from`, `prompt_module`, `stt_language`,
  `tts_language`, `multilingual`, Vobiz credentials. Two entries today:
  `+918065353946` (Gundusoodhi, customer 3, Telugu pinned) and
  `+918071580257` (VoiceZen, customer from `VOICEZEN_CUSTOMER_ID`,
  multilingual).
- `agent/script_gundusoodi.py`, `agent/script_voicezen.py` — each exports
  `GREETING` and `INSTRUCTIONS`. Loaded by `load_client_script()` with
  `importlib`; unknown numbers fall back to Gundusoodhi's script.
- AI stack is **server-wide**, from environment variables, not per client:
  Sarvam streaming STT (`SARVAM_API_KEY`), Gemini LLM (`GOOGLE_API_KEY`,
  `LLM_MODEL` default `gemini-3.5-flash-lite`, `LLM_MAX_TOKENS` 400), Raya TTS
  (`RAYA_API_KEY`, `RAYA_VOICE_ID`, per-language `RAYA_VOICES`).
- Calls are reported through `voicezen.py` → the `log-call` edge function.
  The agent holds only `VOICEZEN_AGENT_SECRET` and the anon key, deliberately
  not the service-role key.

**Dashboard** — repo `C:\Users\Developer\OneDrive\Downloads\dashboard`
(github.com/gopi245/customer_dashboard, branch `master`)

- React + TypeScript + Vite, deployed by Vercel on push to `master`
  (app.voicezentech.com). Read its `CLAUDE.md` first.
- **It sits inside OneDrive, where `node_modules` deadlocks npm, vite and
  tsc.** Edit in the OneDrive path, but run tooling in a mirror outside it
  (e.g. `C:\Users\Developer\dashboard_mirror`), re-syncing after each change.
- Supabase project `sewpzeymsccrehnjvmib`. Edge functions in
  `supabase/functions/`; shared code in `supabase/functions/_shared/`.
  Deploy with `npx supabase functions deploy <name> --project-ref
  sewpzeymsccrehnjvmib`, keeping each function's existing `verify_jwt`
  setting (check it first; `--no-verify-jwt` where it is currently false).
- **One login sees exactly one customer** (`customers.user_id`); there is no
  admin role yet. Customer 16 = VoiceZen (`admin@voicezentech.com`),
  customer 3 = Gundusoodhi (`admin@gundusoodhi.com`).
- The WhatsApp bot is `supabase/functions/whatsapp-webhook/`
  (`index.ts` + `order-details.ts`); all its replies are constants there.

**VPS** — `root@185.199.52.73`, SSH key `~/.ssh/dograh_vps2`

- 4 vCPU, 15 GB RAM (~1.4 GB used), 193 GB disk (5% used) as of 2026-09-18.
  Ample headroom: the admin pages run on Vercel and the data on Supabase; the
  VPS only needs the agent change.
- Containers: `vobiz-native` (the agent), `edesy-native` (another client's
  agent), `n8n-n8n-1`, `n8n-caddy-1`. Do not touch `n8n-*` or `edesy-native`.
  Both agents sit on the `voiceengine_app-network` Docker network — do not
  remove it.
- Redeploy the agent: copy the changed file into `/opt/rumik-agent/vobiz-native/`
  (back up the old one to `/root/backups/` first), then
  `cd /opt/rumik-agent/vobiz-native && docker compose -p vobiz-native up -d --build`.
  The retry queue `unsent_calls.jsonl` is a single-file bind mount on
  purpose; leave it.

## Target design

**Per-client switch.** Add a config source per client — `code` (today) or
`db` (new). The agent keeps using `clients_registry.py` and the script modules
for every client still on `code`. Only clients switched to `db` read the
new tables. Switching back is changing that one value.

**New tables (additive; draft SQL and get sign-off before creating):**

- `agent_profiles` — one row per client: `customer_id`, `dialed_number`,
  `config_source` (`code` | `db`), STT provider and language, LLM model and max
  tokens, TTS provider and voice id, `multilingual`, `active_script_version_id`.
- `agent_script_versions` — every saved script: `customer_id`, `version`,
  `greeting`, `instructions`, `created_by`, `created_at`, `note`.
- `whatsapp_bot_profiles` — per-client bot content (welcome, menu options,
  payment numbers, support contact, FAQs), with the same versioning idea.
- `admin_users` (or an `is_admin` flag) — who may use the admin pages.
- `admin_audit_log` — who changed what, when, before and after.

Row Level Security: admin tables readable and writable by admins only;
clients never see other clients' rows.

**How the agent reads it.** Not with the service-role key — the agent must
not hold it. Add an edge function (e.g. `agent-config`) authenticated with
`VOICEZEN_AGENT_SECRET`, like `log-call`, that returns the profile and active
script for a dialled number. The agent caches it briefly and **falls back to
`clients_registry.py` and the script module if the call fails**, so a
database or network problem can never stop a call being answered.

## Build stages

Each stage is usable on its own and is finished before the next starts.

1. **Admin login and client switcher.** Admin role, a switcher, and a
   read-only per-client overview (usage, recent calls, WhatsApp status,
   current script and AI settings shown from today's code). No behaviour
   changes anywhere.
2. **Call script editor with versions.** Tables, admin editor, version
   history, rollback, test call. Gundusoodhi and VoiceZen copied in verbatim
   as version 1 but left on `config_source = code`. Agent gains the `db` path
   with fallback. Proven on a test client and test number, then switched
   client by client only when the owner says.
3. **AI stack per client.** Move STT, LLM, TTS and voice selection into
   `agent_profiles`, with cost per minute shown. Server-wide environment
   variables stay as the defaults.
4. **WhatsApp bot per client.** Webhook reads bot content by customer;
   today's Gundusoodhi constants become its version 1. This also gives
   VoiceZen's line (+91 96767 53400) its own replies instead of the magazine
   bot.

## Before starting

- Confirm with the owner which stage to start with (stage 1 is the default).
- Create the git branch in each repo that the stage touches.
- Pick or create a test customer and a test number for proving the `db` path.
- Re-check `GO_LIVE_STATE.md` and the live system; this plan is a snapshot,
  and the live system wins if they disagree.
