# DukaanFlow AI

**Turn messy customer messages into organized small-business actions.**

DukaanFlow AI is a local-first MVP for small tiffin services and home-food businesses. It receives a customer message, classifies whether it appears to request a one-day delivery skip, and asks the seller to confirm before changing a delivery record. A one-day skip must never cancel the recurring subscription.

## What's implemented

- Responsive seller dashboard with request queue, customer directory, confirmed skips and activity log.
- Demo message form so the workflow can be tested without a Meta account or any real WhatsApp message.
- SQLite persistence and seeded demo customers.
- Hinglish/Hindi/English intent classification: broad local rules plus optional local Ollama structured-JSON interpretation (`qwen2.5:1.5b`); if Ollama is enabled but unavailable, rules provide a fallback.
- WhatsApp Cloud API webhook verification and inbound text-message parsing.
- Optional validation of Meta's `X-Hub-Signature-256` with `META_APP_SECRET`.
- Duplicate webhook message protection using WhatsApp message ID (`wamid`), plus semantic same-customer/same-date skip deduplication for demo and WhatsApp flows.
- Startup reconciliation for legacy pending/review requests when that customer/date already has a confirmed skip.
- Seller confirmation endpoint that records the date-specific skip but deliberately leaves `subscription_status='active'` unchanged.
- Ambiguous messages, unknown numbers, recurring cancellation requests, and future-date requests are sent for review.
- Optional outbound confirmation through the official Cloud API; disabled by default to avoid unintended messages.
- Pytest coverage for main safety invariants.

## Important: demo vs real WhatsApp

The project is created and testable locally. **Real WhatsApp traffic is not active until you configure a Meta Developer app, a WhatsApp Business Account/phone number, webhook URL, and valid credentials.** The dashboard is a local-development UI, not yet a multi-tenant production SaaS. If you host it publicly, configure `DASHBOARD_PASSWORD`, `FLASK_SECRET_KEY`, `META_APP_SECRET`, HTTPS and deployment-specific security before connecting business traffic. In the hosting environment also set `SEED_DEMO_DATA=false`, `ENABLE_DEMO_TOOLS=false` and `SESSION_COOKIE_SECURE=true`. Never commit credentials. SQLite files are not encrypted by this app; restrict filesystem and backup access.

The standard Cloud API sender identifier does not guarantee that a phone's locally saved contact name is available. This MVP matches senders by normalized phone number; contacts not matching an active customer are held for review. Do not use customer data without appropriate permission and a privacy notice.

## Requirements

- Python 3.11+ (tested target: Python 3.13)
- Optional: [Ollama](https://ollama.com/) for local AI classification
- Optional: Meta WhatsApp Cloud API account for actual WhatsApp integration

## Run on Windows (PowerShell)

1. Extract the ZIP and open PowerShell in the `dukaanflow_ai` folder.
2. Create and activate a virtual environment:

   ```powershell
   py -3.13 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`:

   ```powershell
   Copy-Item .env.example .env
   ```

4. Start the dashboard:

   ```powershell
   python app.py
   ```

5. Open <http://127.0.0.1:5000>. Try **Analyze message** using the prefilled message: `Bhaiya, aaj mere tiffin mat lana.` Then choose **Confirm skip**. The subscription remains active.

The sample phone numbers are synthetic demo records. They are not connected to real WhatsApp contacts.

## Optional local AI setup

For broader interpretation of conversational Hinglish/Roman Hindi (spelling variants such as `aj`, `nhi`, `mt`, `krdo`), enable the local Ollama model. The starter `.env.example` has Ollama enabled; if you already have an older `.env`, edit that file and set:

```dotenv
OLLAMA_ENABLED=true
OLLAMA_MODEL=qwen2.5:1.5b
OLLAMA_TIMEOUT_SECONDS=12
```

Then in PowerShell run (if the model is not already available):

```powershell
ollama pull qwen2.5:1.5b
ollama list
```

Keep Ollama running and restart DukaanFlow AI. The classifier prompt includes Hinglish and Devanagari examples, and falls back to expanded rules if Ollama is unavailable. No classifier can guarantee understanding every slang phrase; uncertain dates, unrecognized messages, unmatched customers, and subscription-cancellation requests must remain for seller review. The app must never infer a date for one-click confirmation, and seller confirmation remains required.

## Configure WhatsApp Cloud API

Use Meta's official Cloud API docs and your Meta Developer app dashboard. The standard integration steps are:

1. Create/configure an app with the WhatsApp product and obtain the WhatsApp Business Account and phone number IDs.
2. Put the verification token you choose in `.env` as `WHATSAPP_VERIFY_TOKEN`. It must match the value entered in Meta's webhook setup.
3. Configure the app secret in `META_APP_SECRET` so POST webhook signatures are checked.
4. Publish this Flask service behind a publicly reachable **HTTPS** URL. Meta cannot call your `127.0.0.1` address; for local experiments use a trusted HTTPS tunnel only for test data, or deploy to a secure host.
5. Set the callback URL to `https://YOUR-HOST/webhook`, verify the webhook, and subscribe to the WhatsApp `messages` webhook field for your WABA.
6. Configure `WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` in the hosting environment (not in source code). Select a currently supported Graph API version using `META_GRAPH_VERSION`; the current example default is `v26.0` as of October 2026.
7. First test with Meta's test number and allowed test recipients. Don't connect a live business number until you have checked onboarding eligibility, number migration/coexistence requirements, customer consent and data retention.

Official references:
- [WhatsApp Cloud API overview and requests (Meta Postman collection)](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api)
- [WhatsApp webhook payload reference (Meta Postman collection)](https://www.postman.com/meta/whatsapp-business-platform/folder/tduohwq/webhook-payload-reference)
- [Graph API version table](https://developers.facebook.com/docs/graph-api/changelog/versions/)
- [Ollama chat API](https://github.com/ollama/ollama/blob/main/docs/api.md)

The webhook GET endpoint uses `WHATSAPP_VERIFY_TOKEN`; the POST endpoint requires a valid `X-Hub-Signature-256` based on `META_APP_SECRET`. If the app secret is missing, POST requests are rejected. Demo ingestion uses a separate local endpoint.

## Optional customer confirmation

The app can send a confirmation after a seller confirms a recent inbound WhatsApp request, within the 24-hour customer-service window. This is disabled by default. Only set `WHATSAPP_SEND_CONFIRMATIONS=true` after test-number verification and after you understand the applicable Meta rules/costs. The app attempts this only for `source=whatsapp` requests; demo messages never send a WhatsApp message. Older requests are not sent as free-form messages; use a Meta-approved template flow for messages outside the service window. API send failure does not undo a delivery decision.

## Run tests

```powershell
python -m pytest -q
```

The tests check multiple Hindi/Hinglish variants, ingredient instructions versus delivery skips, subscription cancellation, date ambiguity, a confirmed skip leaving the subscription active, duplicate same-day requests being ignored before creating actionable rows, startup reconciliation, unknown customers, and webhook verification.

## Main routes

- `GET /` — dashboard
- `GET /health` — health check
- `POST /api/demo/message` — local demo ingestion (disable with `ENABLE_DEMO_TOOLS=false`)
- `POST /api/customers` — add a customer or map a known unmatched sender number
- `POST /api/requests/<id>/confirm` — confirm eligible one-day skip
- `POST /api/requests/<id>/review` — move request to manual review
- `POST /api/requests/<id>/recheck` — recheck an unmatched request after mapping its customer
- `POST /api/requests/<id>/dismiss` — dismiss a request without changing delivery
- `GET /webhook` — Meta webhook verification
- `POST /webhook` — inbound WhatsApp webhook

## Limitations / next steps

- One business workspace; no organization or tenant isolation yet.
- Date handling supports a conservative same-day skip path. Tomorrow/ambiguous requests require review in this MVP.
- No full WhatsApp contacts sync; sender phone matching is used.
- No billing, roles, backups, retention scheduler, or deployment infrastructure yet.
- No automatic business-actioning from a model: seller confirmation is intentional.

## License

MIT. See `LICENSE`.


### Windows timezone startup error

If you previously saw `ZoneInfoNotFoundError: 'No time zone found with key Asia/Kolkata'`, this version includes a fallback to India Standard Time (UTC+05:30) and installs `tzdata` on Windows. Extract this updated ZIP into a fresh folder, then run `run_windows.bat` again.
