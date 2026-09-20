# Tolls/tickets ↔ trips matcher
```
                   YOUR APP
                      │
        ┌─────────────┴─────────────┐
        │                           │
        ▼                           ▼
 Firebase Authentication          Stripe
        │                           │
 Google Login                 Subscription
        │                           │
 Gmail / Workspace            Payment status
        │                           │
        └────────── UID ────────────┘
                      │
                      ▼
              Authenticated App
                      │
         ┌────────────┴─────────────┐
         ▼                          ▼
    Toll Resolution          Violation Resolution
         │                          │
         └────────────┬─────────────┘
                      ▼
               Evidence Package
```
Upload a tolls/tickets bill (PDF or photo) plus a marketplace trip export (CSV/XLSX) and get,
per trip, how much to charge the guest.

## How it works

1. **Read the bill** — text PDFs are parsed with `pdfplumber`; scanned PDFs and photos go
   through Tesseract OCR. OCR word boxes are regrouped into visual rows so a line item's
   date, plate, and amount stay on one line even in multi-column statements.
2. **Read the trips** — column names are auto-detected (trip start/end, license plate,
   guest, vehicle, reservation id) from CSV or Excel. When there is no plate column, the
   plate is read out of the vehicle label, e.g. `Taofeek's Nissan (TX #VCG6008)`.
3. **Match** — a toll belongs to a trip when the plates agree and the toll timestamp falls
   inside the trip window (± a configurable buffer, default 2h). If nothing matches on
   time, the toll falls back to calendar-date overlap with the trip. Tolls that fit several
   trips equally well are charged in full to the best-ranked one;
   tolls outside every trip's dates, or whose plate contradicts every candidate trip's
   plate, stay `unmatched` and are charged to nobody. Statement lines that are not tolls
   (tag-store rebills, card auto-charges, support-services system fees) are dropped while parsing.
4. **Charge** — optional markup % and per-item admin fee are applied; anything the
   marketplace already collected on the trip (its "Tolls & tickets" column) is subtracted, so only the
   shortfall is owed. Totals roll up per trip and export to CSV.

Everything parsed is editable in the browser, so OCR mistakes (plate `0` vs `O`, a wrong
amount) can be fixed and matching re-runs instantly.

## Run

```bash
pip install -r requirements.txt          # plus: apt install tesseract-ocr poppler-utils
python -m uvicorn app.main:app --port 8080
# open http://localhost:8080
```

## Authentication

Sign-in is Firebase Authentication with Google only (personal and Workspace
accounts alike) — no passwords, no user database. The Firebase uid is the
permanent identity; anything persisted later would hang off `users/{uid}/…`.

* Browser: `/login` → "Continue with Google" (popup, redirect when popups are
  blocked). `static/auth.js` is the single Firebase client and the single auth
  listener; the app is hidden behind a loading gate until the state is known.
* Server: every `/api/*` route (except `/api/config`) depends on
  `require_firebase_user`, which verifies the `Authorization: Bearer <ID token>`
  with the Firebase Admin SDK. A uid sent by the browser is never trusted.

Copy `.env.example` to `.env` and fill it in. The `FIREBASE_API_KEY`…`FIREBASE_APP_ID`
values are web config and are served to the browser on purpose;
`FIREBASE_CLIENT_EMAIL` and `FIREBASE_PRIVATE_KEY` are Admin credentials and stay
on the server. In the Firebase console: enable Authentication → Google, set a
support email, and add `localhost` plus the production host under Authorized
domains.

With no Firebase project configured, `DEV_AUTH_BYPASS=1` runs the app open for
local development; it is ignored as soon as Admin credentials exist.

## Billing

Subscriptions run on Stripe, still with no application database. Firebase says
who the user is, Stripe says whether they have paid, and the bridge between them
is the Firebase uid stored on the Stripe customer as `metadata.firebaseUid`.
Every read goes to Stripe, so a subscription survives sign-out, a new browser
and a new device with nothing to keep in sync.

Endpoints, all behind `require_firebase_user`:

| Route | Does |
| --- | --- |
| `GET /api/billing/status` | normalized state from Stripe |
| `POST /api/billing/checkout` | hosted Checkout for a plan key |
| `POST /api/billing/cancel` | `cancel_at_period_end=true`, access kept to the period end |
| `POST /api/billing/resume` | undoes a pending cancellation |
| `POST /api/billing/portal` | hosted Customer Portal session |
| `POST /api/stripe/webhook` | signature-verified events (public, verified by signature) |

The browser sends a plan key (`monthly`, `yearly`) and never a Price ID, customer
ID or subscription ID: the server resolves those from `app/billing.py`'s plan map
and from the uid on the token. `/billing/success` confirms with Stripe rather
than trusting the redirect, and checkout is refused when a live subscription
already exists.

### Stripe setup

1. Products → add one product with two **recurring** prices, monthly and yearly;
   copy the `price_…` ids into `STRIPE_PRICE_MONTHLY` / `STRIPE_PRICE_YEARLY`.
   An interval with no configured price is not offered in the UI. Yearly carries
   a 30-day free trial, applied by Checkout as `subscription_data.trial_period_days`
   (Stripe's Trial Offer objects, `to_…`, are not supported by hosted Checkout).
2. Developers → API keys → copy the secret key into `STRIPE_SECRET_KEY`
   (test key while developing, live key in production).
3. Settings → Billing → Customer portal → activate it, and allow cancellation
   and payment-method updates.
4. Local webhooks:

   ```bash
   stripe listen --forward-to localhost:8080/api/stripe/webhook
   ```

   Put the `whsec_…` it prints into `STRIPE_WEBHOOK_SECRET`.
5. Production: Developers → Webhooks → add `https://your-host/api/stripe/webhook`
   for `checkout.session.completed`, `customer.subscription.*`, `invoice.paid`
   and `invoice.payment_failed`, then copy that endpoint's signing secret into
   `STRIPE_WEBHOOK_SECRET`.

Nothing is behind the paywall yet. `PAID_FEATURES=parse,evidence` turns on the
`requires_subscription` gate for document processing and evidence images; the
default empty value leaves every feature open to signed-in users.

## Sample data / test

```bash
python samples/make_samples.py   # writes a fake toll bill (PDF+PNG) and trips CSV
DEV_AUTH_BYPASS=1 python -m uvicorn app.main:app --port 8080   # server under test
python samples/smoke_test.py     # end-to-end check against a running server
python samples/test_matching.py  # matching rules
python samples/test_auth.py      # auth boundaries
python samples/test_billing.py   # Stripe billing against a fake Stripe
python samples/ui_check.py       # browser flow against a running server
```
