# Onboarding a new client (multi-tenant, LiveKit-free)

Not built yet — this is the plan agreed on, to follow once you say "proceed."
Nothing here has been implemented; `vobiz-native` currently only knows about
Gundusoodi.

## Architecture in one paragraph

One shared `vobiz-native` server keeps handling every client. Each client
gets their own Vobiz account (own number, own billing, own concurrency
limit), but all their Vobiz Applications point at the same server URL. A
client registry (a small table, keyed by dialed number) tells the server
which client's script to load and which client's Vobiz credentials to use
for anything outbound. No new deployment per client.

## What needs building first (one-time, before client #2)

- The client registry (Supabase table: client_id, vobiz_auth_id,
  vobiz_auth_token (encrypted), numbers, prompt_module, customer_id)
- `server.py` changed to load the prompt per-call by looked-up client,
  instead of the current hardcoded `from script_gundusoodi import ...`
- Outbound/API calls (dial, number status) changed to pick credentials from
  the registry instead of the single pair in `.env`

## Steps for each new client, start to end

| # | Step | Who |
|---|------|-----|
| 1 | Sign up for a new Vobiz account for this client — KYC, payment method | **You** |
| 2 | Buy a number on that account | Either — I can do it via API once funded, or you via their dashboard |
| 3 | Get that account's `auth_id` / `auth_token` from their dashboard, give them to me | **You** |
| 4 | Create a Vobiz Application on that account, `answer_url`/`hangup_url` pointing at `vobiz-native.185-199-52-73.sslip.io` | Me |
| 5 | Assign the number to that Application | Me |
| 6 | Write the client's script (prompt, greeting, refusal rules, tools) — tell me the business details | Me |
| 7 | Add the client to the registry | Me |
| 8 | Test-call the number, confirm the right script answers and the call logs correctly | You place the call; I check the logs/dashboard after |
| 9 | Hand the number to the client | **You** |

## What I can't do

- Sign up for accounts, complete KYC, or pay for anything (steps 1, 3, 9) —
  those need your identity, business documents, and payment method.
- Judge live voice quality by ear — I can confirm the technical pipeline
  worked from logs, not how it actually sounded.

## To actually start

Say "proceed" — I'll build the registry + server changes first (one-time),
then walk through steps 1-9 for the first new client.
