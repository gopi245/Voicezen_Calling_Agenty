# Setting up instant callback for a new client's website

When someone fills out an interest/demo form on a client's website, this
makes the AI agent call them back immediately — not on any batch schedule.
Follow this checklist for every new client who wants it.

## What happens under the hood

The client's website form submits to `lead-intake`, a Supabase Edge
Function. It:
1. Verifies the client's API key
2. Immediately saves the person to that client's **Leads** page
3. Immediately dials them through `vobiz-native` (no waiting for any cron)
4. If unanswered/rejected, retries automatically after 5 min, then 10 min,
   then gives up (same retry logic as campaign calls)
5. Reports the outcome back — if they sound interested, their Leads entry
   gets the address/details the AI collected

This reuses the exact same calling pipeline as Campaign Calls — it's
modeled internally as a one-contact campaign, not separate code.

## Step 1 — Generate an API key for this client

Dashboard → API Keys page → "Generate new key" → name it something
identifiable, e.g. `"<Client name> website form"`.

Copy the generated key (`vz_live_...`) — this is what goes to their
website.

## Step 2 — Ask the client two questions

1. **What platform is your website hosted on?** (Vercel, Netlify, WordPress,
   Wix/Squarespace/no-code, or a custom server) — decides which code
   snippet applies (see Step 3).
2. **What should the AI say / ask about on this call?** — their answer
   becomes the `pitch_content` / `call_to_action` / `objection_handling`
   fields (see Step 4). If they don't have a strong preference, a generic
   "someone showed interest on your website" script is used by default.

## Step 3 — The endpoint (same for every platform)

```
POST https://sewpzeymsccrehnjvmib.supabase.co/functions/v1/lead-intake
Headers:
  Content-Type: application/json
  X-Api-Key: <the client's key from Step 1>

Body:
  {
    "name": "<from form>",
    "phone": "<from form, E.164 format e.g. +919876543210>",
    "source": "website",
    "pitch_content": "<optional, see Step 4>",
    "call_to_action": "<optional, see Step 4>",
    "objection_handling": "<optional, see Step 4>",
    "language": "<optional, defaults to Telugu>"
  }
```

Success response: `{ "success": true, "campaignId": <n>, "contactId": <n> }`
Error response: `{ "error": "<message>" }`

**Important:** the API key must never be placed directly in browser
JavaScript — it always goes through a small server-side piece (a
serverless function, a backend route, or a Zapier/Make webhook action)
that the client's site controls.

## Step 4 — Customizing what the AI says (optional, per client)

Defaults if not provided:
- `pitch_content`: "This person just filled out an interest form on
  <business>'s website and asked to be called back. Thank them for their
  interest, ask what specifically they're interested in, and answer their
  questions."
- `call_to_action`: "Answer their questions clearly. If they want to
  proceed, collect any details needed (such as delivery address, if
  relevant) and let them know the next step."
- `objection_handling`: "If they say now isn't a good time, ask when would
  be better and offer to call back then."
- `language`: "Telugu"

Override any of these based on what the client tells you in Step 2 — same
idea as writing campaign scripts, just phrased for "this person just
showed interest," not a cold outbound pitch.

## Step 5 — Code snippet by platform

### Vercel (serverless function)
```typescript
// api/request-callback.ts
import type { VercelRequest, VercelResponse } from '@vercel/node';

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method !== 'POST') return res.status(405).json({ error: 'Method not allowed' });

  const { name, phone } = req.body ?? {};
  if (!phone || !/^\+?\d{8,15}$/.test(String(phone).replace(/\s/g, ''))) {
    return res.status(400).json({ error: 'Invalid phone number' });
  }
  const normalizedPhone = phone.startsWith('+') ? phone : `+91${phone.replace(/\D/g, '')}`;

  const vzRes = await fetch('https://sewpzeymsccrehnjvmib.supabase.co/functions/v1/lead-intake', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Api-Key': process.env.VOICEZEN_API_KEY as string },
    body: JSON.stringify({ name, phone: normalizedPhone, source: 'website' })
  });

  if (!vzRes.ok) return res.status(502).json({ error: 'Could not schedule callback' });
  res.status(200).json({ success: true });
}
```
Env var: Vercel → Project → Settings → Environment Variables → `VOICEZEN_API_KEY`

### WordPress (functions.php, hooked to Contact Form 7 submission)
```php
add_action('wpcf7_mail_sent', function($contact_form) {
    $submission = WPCF7_Submission::get_instance();
    $data = $submission->get_posted_data();

    wp_remote_post('https://sewpzeymsccrehnjvmib.supabase.co/functions/v1/lead-intake', [
        'headers' => ['Content-Type' => 'application/json', 'X-Api-Key' => getenv('VOICEZEN_API_KEY')],
        'body' => json_encode(['name' => $data['your-name'], 'phone' => $data['your-phone'], 'source' => 'website'])
    ]);
});
```
(field names depend on which form plugin the client uses)

### Node/Express (custom backend)
```javascript
app.post('/api/request-callback', async (req, res) => {
  const { name, phone } = req.body;
  const normalizedPhone = phone.startsWith('+') ? phone : `+91${phone.replace(/\D/g, '')}`;

  const vzRes = await fetch('https://sewpzeymsccrehnjvmib.supabase.co/functions/v1/lead-intake', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Api-Key': process.env.VOICEZEN_API_KEY },
    body: JSON.stringify({ name, phone: normalizedPhone, source: 'website' })
  });

  if (!vzRes.ok) return res.status(502).json({ error: 'Could not schedule callback' });
  res.json({ success: true });
});
```

### Wix / Squarespace / no-code sites — use Zapier or Make, not code
These platforms usually can't run custom backend code. Instead:
1. Trigger = "New form submission" (their native form, Google Form, Typeform, etc.)
2. Action = "Webhooks by Zapier/Make → POST"
3. URL = the `lead-intake` endpoint above
4. Headers = `X-Api-Key` (stored safely inside Zapier/Make's own encrypted
   field — never exposed anywhere public)
5. Map the form's name/phone fields into the JSON body

This is genuinely the least-effort option for any client whose site isn't
custom-coded — no developer needed, you configure it yourself.

## Things worth remembering

- Every instant-lead call costs real minutes against the client's plan,
  same as any other call — worth being upfront about that when pitching
  this feature.
- Vobiz's account-wide concurrency limit (3 simultaneous calls) is shared
  across real inbound calls, running campaigns, and instant leads. A flood
  of leads at once will queue, not all ring instantly.
- Facebook/Instagram Lead Ads are a separate, heavier integration (Meta
  Developer App + Page linkage + app review) — not covered by this
  checklist. Treat as a distinct add-on, not part of the standard website
  setup.
