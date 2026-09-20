---
name: testing-fleetreclaim
description: Browser E2E setup, fixture expectations, and auth boundaries for FleetReclaim.
---

# Local setup

- Use the Python environment with requirements.txt dependencies, Playwright,
  Tesseract and Poppler installed. There is no database setup.
- Functional mode must remove admin credentials even when DEV_AUTH_BYPASS=1:
  `env -u FIREBASE_CLIENT_EMAIL -u FIREBASE_PRIVATE_KEY -u GOOGLE_APPLICATION_CREDENTIALS DEV_AUTH_BYPASS=1 python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080`.
- Signed-out auth regression mode can run separately:
  `env -u DEV_AUTH_BYPASS python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8081`.
  Never print environment values or attempt real Google login without authorization.
- If reusing Chrome CDP, pin the test page by a unique `window.name` instead of
  a page index: attachment order can differ between connections. Reload after
  frontend changes; an existing tab retains its previously loaded HTML.
- Maximize the browser before recording. At narrow widths, wait for the
  sidebar CSS transition to finish before screenshots.

# Primary browser flow

Upload `samples/toll_bill.pdf` and `samples/trips.csv` with exclusions empty.
Process documents automatically selects Recovery (`data-view="charge"`).
Expected: 8 transactions, 7 matched, 1 unmatched, $52.85 statement and
$39.60 recoverable. R-1001 has two items: $16.35 minus $10 already charged
leaves $6.35. Trips view has 5 records; settled R-1005 is hidden by default.

- Use tbody selectors for table rows.
- Recovery filter IDs: fFrom, fTo, fVehicle, fPlate, fTrip, fSettled.
- Detail filters dPlate/dStatus should not filter the main trip table.
- Select option labels when values are implicit HTML text.
- Evidence and per-trip CSV controls are in the trip modal. Overall CSV is
  generated in the browser, not necessarily via /api/export.
- The cache note is #tripCacheNote. It persists in sessionStorage across reload;
  `upload a new file` invokes a native file chooser. Exclusions use localStorage.
- `Golden Gate Bridge` exclusion gives 7 transactions and $29.85 recoverable.
- The sidebar switches at 900px. Test 390px widths, horizontal table scrolling,
  desktop-collapse-to-mobile transitions, keyboard focus, and accessible names.

# Auth regression

Without a user, `/` must redirect to `/login` without visible #app frames.
Browser-origin requests to POST parse/match/crop/export and GET me must 401;
public GET /api/config returns only browser-safe Firebase configuration.
Do not equate signed-out boundary checks with successful Firebase verification.

The optional `python3 -m samples.simulated_crop_check` uses a separate loopback
server and overrides only the identity dependency. It verifies owner crop=200,
other UID=404 identical to unknown parse_id, owner remains 200. Label this
explicitly as simulated identities, never as real Firebase testing.

# Stripe TEST billing

- Load the gitignored `.env` with `set -a; . .env; set +a`, then run the
  credential-stripping functional server command above. Confirm the Stripe key
  is a TEST key without printing it. Restart uvicorn after backend/env changes.
- Open `/?view=billing`. A clean customer shows Monthly and
  Yearly — 30 days free. Preserve a second tab before checkout to exercise the
  stale-page duplicate guard: after subscribing, its Yearly click must show
  an already-subscribed error and return HTTP 409 without navigation.
- Use real hosted Checkout with card 4242 4242 4242 4242, a future expiry,
  CVC/ZIP and test name. The yearly flow should show $0 today and 30 days free.
  `/billing/success` must reach Subscription active; Billing must show Yearly,
  Free trial and an end timestamp approximately 30 days ahead.
- Cancel and resume should toggle access-until messaging and the action button
  without changing the period end. Customer Portal should show the same test
  identity, yearly trial, end date and Visa ending 4242.
- Stripe persists test customers/subscriptions between local runs. A fresh
  checkout needs a non-subscribed test identity; do not delete existing Stripe
  resources without authorization. Customer email must be RFC-valid, and
  reusing an idempotency key with changed profile parameters may be rejected.
- Browser `window.name` can reset across Stripe cross-origin redirects; target
  the hosted page by origin and reassign its name after returning to localhost.
- A configured webhook secret permits a bogus-signature rejection check (400).
  This is not proof of real event delivery. Only use Stripe CLI forwarding if
  available/authorized, with the listener's signing secret configured locally.

## Devin Secrets Needed

- No secrets for functional bypass mode.
- Auth-enabled configuration uses FIREBASE_API_KEY, FIREBASE_AUTH_DOMAIN,
  FIREBASE_PROJECT_ID, FIREBASE_APP_ID, FIREBASE_CLIENT_EMAIL and
  FIREBASE_PRIVATE_KEY; never include their values in evidence.
- Successful Google authentication requires a configured project and an
  authorized test account; it is not covered by the bypass or synthetic harness.
- Real Stripe TEST billing needs STRIPE_SECRET_KEY, STRIPE_PRICE_MONTHLY and
  STRIPE_PRICE_YEARLY. Signature verification additionally needs
  STRIPE_WEBHOOK_SECRET. Never include secret values or hosted-session URLs in
  shared artifacts.
