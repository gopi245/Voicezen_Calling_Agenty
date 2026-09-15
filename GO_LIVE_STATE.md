# Gundusoodhi go-live state — 2026-09-16

Where the Gundusoodhi (PiNN Media) line stands the night before it is handed
to the real client, what changed to get here, and what is still open. Written
because most of this is spread across two repositories, a VPS, a Supabase
project and a Vobiz account, and none of it is obvious from any one of them.

`RUNBOOK.md` is still the operational guide. This file is a point-in-time
record; when it disagrees with the live system, the live system is right.

---

## The routing chain, end to end

A call to **+918065353946** resolves like this. Every step was verified
against the live system, not inferred:

```
Vobiz number +918065353946
  → application_id 22118967368729053  ("vobiz-native-rumik")
  → answer_url https://vobiz-native.185-199-52-73.sslip.io/answer
  → container vobiz-native on 185.199.52.73
  → clients_registry.py: client_id "gundusoodi", customer_id 3
  → check-minutes: customer 3, Enterprise, 5000 min, renews 2027-09-15
  → log-call: vobiz_number + engine='livekit' → integration 7 → customer_id 3
  → call_logs row under customer 3
```

The number has **no `trunk_group_id`**, which is the concrete reason LiveKit
is not in the call path.

WhatsApp routes separately, on
`customer_integrations.vobiz_whatsapp_phone_number_id`, currently
`1270842702783701` on WABA `1108179771753860`.

## The client account

Customer **3** is Gundusoodhi. It used to be the shared "voicezen" account
and was converted rather than recreated, because `clients_registry.py`
hard-codes `customer_id: 3` and creating a new customer would have meant a
code change plus an agent redeploy.

| | |
|---|---|
| Login | `admin@gundusoodhi.com` |
| Auth user | `f4438ead-e8a8-40ee-87b2-42f6e08abc8b` |
| Plan | Enterprise, 5000 min, renews 2027-09-15 |
| Integration | id 7, `engine: 'livekit'`, `+918065353946` |

The previous subscription had expired on 2026-09-14, which meant
`check-minutes` was returning `no_active_plan` and every caller was hearing
"ఈ సర్వీస్ అందుబాటులో లేదు" before the line hung up. Worth checking the
renewal date first if calls ever start failing that way again.

All test data was **moved, not deleted** — 372 rows across 13 tables
reassigned from customer 3 to customer 16, so the client opens a clean
dashboard and nothing was lost. Two `api_keys` rows went with it, which
matters: `lead-intake` resolves the customer *from the key*, so leaving them
behind would have filed every voicezentech.com lead under the client and
dialled those people on the client's minutes.

## Do not rename `engine = 'livekit'`

It is a label, not a description — no LiveKit is involved in a call any more.
But `log-call` both filters on it and writes it:

```ts
.eq('engine', 'livekit')   // line 114, resolves the customer
engine: 'livekit',         // line 156, written into call_logs
```

Change the value without changing both lines in the same deploy and
`log-call` returns `404 No livekit integration`. The call still connects and
the caller notices nothing, but no row is written — so no call history and no
minutes billed, silently. Billing is nothing more than summing
`call_logs.call_duration`, so a missing row is a missing invoice.

`dograh_workflow_id` is the same kind of trap: the column is NOT NULL and the
new onboarding writes `"livekit:<client>"` into it, so despite the name it
carries the current system and cannot be dropped.

## What was removed

**Dograh** — fully gone. No containers, images, volumes or files on the VPS,
and its Vobiz application (`dograh-1a0b2a3b1bda`, which was `default_app`)
has been deleted. That default mattered: any newly bought number would have
inherited it and rung into `api.dograh.com`, which no longer serves this
account. With no default at all, a new number arrives unrouted, which is
visible instead of silent. Only database labels remain.

**LiveKit** — removed from the VPS: containers `gundusoodi-agent` and
`rumik-agent`, their images, their networks, and `/opt/gundusoodi`. Archived
to `/root/backups/livekit-removal-20260915.tar.gz` first. The LiveKit Cloud
account still holds two SIP trunks and two agent registrations; those are on
LiveKit's side and cost nothing to leave, but check the billing page.

`voiceengine_app-network` was deliberately **not** removed. It is a leftover
from the Dograh stack, but `vobiz-native` and `edesy-native` are both
attached to it, so deleting it would take down two client lines.

## Fixed tonight

**The retry queue could never empty itself.** `unsent_calls.jsonl` is a
single-file bind mount, and `unlink()` on a mount target raises `EBUSY` —
which `except OSError: pass` swallowed. So the success path of
`_flush_queue()` never completed, and every later successful call re-posted
the whole backlog. Four stuck calls had become nine `call_logs` rows in one
evening, each replay also re-uploading its recording. `log-call` has no call
identifier column, so it cannot tell a replay from a new call.

Fixed by truncating instead of deleting (`write_text("")`), which works fine
on a bind mount. The mount itself is correct and intentional — the compose
file explains that without it the queue lived only inside the container and
was wiped on every redeploy, losing any call that had failed to report.

**The post-call WhatsApp gate dropped the most committed callers.** It tested
`subscription_plan` alone, so a caller who read out their house number,
street, city, district and pincode but whose plan never got recorded was
marked `skipped` — silently and permanently, since the row is stamped as
handled. The gate now accepts an address on its own. A caller who asked a
price and hung up is still correctly ignored, which is the point of having a
gate at all.

That fix needed a second change in the same function: with a null plan the
template's body parameter would have gone out empty, and Meta rejects that,
so the send would have failed at the API for exactly the caller the new gate
lets through. Both places now use one non-empty `planLabel`.

## Nothing sends

Verified through `cron.job` rather than assumed. The only scheduled jobs are:

```
campaign-dispatch-tick     * * * * *     active
recordings-cleanup-daily   20 3 * * *    active
```

Neither sends WhatsApp. There is no `post-call-whatsapp` or
`call-summary-alert` schedule. The dispatcher also has nothing to dial: all
60 campaigns are completed, failed or cancelled, with none running or queued.

This is deliberate and must stay that way until the user explicitly says
sending may begin. A summary sender that was built, scheduled and run
immediately once pushed nine unasked-for messages to three of their team
numbers, and that is the reason for the rule.

## Still open

1. **`customer_integrations.forwarding_number` is null** for integration 7.
   A caller who asks to speak to a person reaches nothing. Needs a real
   mobile at Gundusoodhi.

2. **The script promises what cannot be delivered.**
   `agent/script_gundusoodi.py` tells callers "we're sending it to your
   WhatsApp" on lines 99, 187 and 248. A caller has not messaged on WhatsApp,
   so no 24-hour window is open and the first message must be an approved
   template. Rewording those to ask the caller to message the number instead
   removes the template from the critical path entirely — the window then
   opens from their side and every reply after it is free text, unlimited and
   free. Line 250 already does this for address corrections.

3. **An idempotency key for `call_logs`.** Tonight's fix closes the known
   cause of duplicate rows; this closes the rest. Vobiz supplies a
   `CallUUID` and the agent already tracks it as `self._call_uuid`; there is
   just nowhere to put it:

   ```sql
   alter table call_logs add column if not exists provider_call_id text;
   create unique index if not exists call_logs_provider_call_id_key
     on call_logs (provider_call_id) where provider_call_id is not null;
   ```

4. **Five duplicate rows on customer 16** from tonight, left in place
   deliberately — about four surplus minutes on the user's own account.

5. **If the real WhatsApp number changes**, integration 7 needs the new
   `vobiz_whatsapp_phone_number_id`, `vobiz_whatsapp_channel_id` and
   `vobiz_whatsapp_waba_id`. Until then inbound messages are dropped as
   unmapped — the bot looks dead rather than degraded. Templates belong to
   the WABA, so joining `1108179771753860` keeps the approved ones; a new
   WABA sends all of them back into review. The inbound automation itself
   needs no template: the welcome falls back to plain text and the 1 / 2
   digits still work, because the digit matching checks the logged body.

6. **Rotate the credentials** that were pasted into the working session: the
   `sb_secret_` project key and the `sbp_` access token.

## Inbound WhatsApp needs no template approval

Worth stating plainly because it came up repeatedly. Everything reactive is
free text inside the window the customer opens by messaging: the welcome and
address form, order parsing, missing-field prompts, plan matching, FAQs,
payment screenshots, escalation to a human, complaint capture, and the
subscription confirmation a human triggers. The welcome is sent as a template
only to get tappable buttons, and `sendAndLog` falls back to plain text if
that send fails.

The single exception is messaging someone who has not written first.
