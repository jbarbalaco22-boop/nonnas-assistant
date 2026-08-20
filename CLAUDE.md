# nonnas-assistant ("Harvest")

Hosted financial dashboard + Q&A chat assistant for Nonna's Italian Goods, used by the CFO and
the two founders. Frontend is a single static file (`frontend/index.html`) deployed as a Render
Static Site at `nonnas-assistant-frontend.onrender.com`; backend is a FastAPI app
(`server.py`/`handlers.py`) deployed separately at `nonnas-assistant.onrender.com`. Shares QBO/
Shopify connectors with `nonnas-finance-audit` and `nonnas-daily-operator` via the `nonnas-shared`
package — read that repo's CLAUDE.md first for QBO/Shopify API gotchas that apply here too.

## Shopify live-data limits (discovered the hard way — trust these over intuition)

- **Bulk date-range order search caps out around 55-61 days back.** `shopify_client.fetch_orders`
  and `fetch_orders_with_customers` both search via `created_at:>=X AND created_at:<Y` — Shopify's
  live order API silently returns fewer/zero results the further back that range reaches, with NO
  error to signal it. Confirmed empirically live: a single-day pull 61 days back still returned
  real orders, 62 days back did not. `SHOPIFY_LIVE_LOOKBACK_DAYS = 55` (handlers.py) is that
  boundary with a small safety margin. **Any new feature built on a date-range order search must
  either clamp+warn (like `get_repeat_purchase_rate` does) or blend in a hand-reconciled historical
  reference (like `get_sku_units_for_period` does via `channel_units_by_month.csv`) — never just
  trust the range the caller asked for.** Missing this caused a real bug (2026-08-19): a
  multi-month repeat-purchase-rate/CAC request silently returned partial data labeled as if it
  covered the full range.
- **The per-customer order-history lookup is a different, NOT-limited surface.** Nesting
  `customer { orders(first: 1, sortKey: CREATED_AT) { nodes { id } } }` inside an order query
  returns that customer's true earliest order regardless of how old it is (confirmed live back to
  April 2025) — this is what `is_first_order` relies on. The limit above is specifically on the
  *bulk* date-range order search that finds the orders in the first place; a single customer's own
  history isn't bound by it. Don't conflate the two when reasoning about what's fixable.
- **A Shopify custom-app scope change (e.g. adding `read_customers`) needs two separate steps**,
  not one: (1) add the scope and deploy a new app version in the dev/Partner dashboard, AND
  (2) the merchant approving the updated permissions on the *store's own* installed-app page
  (Settings → Apps and sales channels → the app, not the dev dashboard). Deploying the scope alone
  does not propagate to an already-installed app. Once approved, no backend redeploy is needed —
  this app authenticates via client-credentials (a fresh token every run), so the next request
  automatically reflects the new scope.
- **QBO's live "Bank" balance (the Banking Center's bank-feed number, distinct from the "Posted"
  book balance) is not reachable at all via the API this app uses.** It requires Intuit's separate,
  partner-gated Bank Feeds API — confirmed by testing, not something a custom app like this one can
  get access to. `get_cash_snapshot`'s `cash_balance` is QBO's book balance (verified to match QBO's
  own Balance Sheet report exactly) and can lag what the bank itself shows by up to a day if a
  transaction hasn't synced into QBO yet. That's a real, permanent gap, not a bug — the frontend
  says so directly under the Combined Cash Balance stat rather than letting it look like an error.

## Frontend architecture (`frontend/index.html` — single file, no build step)

- **`dashboardRequestId` guard — required for any new "load X for the selected period" function.**
  `loadDashboard()`'s three follow-up fetches (SKU Revenue, SKU Units, Repeat Purchase Rate/CAC)
  are fire-and-forget and don't cancel on a later date-range change. Real bug (2026-08-19):
  switching ranges quickly let a slower old response land after a newer one and render
  mismatched-period data (CAC dividing one period's ad spend by a different period's new-customer
  count). Fix: every `loadDashboard()` call mints `++dashboardRequestId` and threads it to each
  follow-up, which discards its response instead of rendering if a newer request has since
  superseded it. Any new per-range-load function must follow the same pattern or risk repeating
  this bug class.
- **`API_BASE` must point at production (`https://nonnas-assistant.onrender.com`) before every
  commit.** It gets flipped to `http://localhost:8000` for local testing against a local uvicorn
  instance — grep for `const API_BASE =` and confirm before `git add`.
- **Deploy verification pattern**: commit → push → poll production with curl for a distinctive
  string from the change (`grep -q 'someNewIdentifier'`) → then live-verify in the browser. Expect
  1-2 poll cycles (~15-45s) before Render's build finishes.
- **Known Render cold-start pattern**: the heaviest endpoints (`/dashboard`, `/trends`,
  `/cash-snapshot`) can 502 on the very first request after the backend has been idle or just
  redeployed, and reliably succeed on retry seconds later. `fetchWithRetry` (2 retries, 2s delay)
  plus sequencing dashboard → trends → cash-snapshot in `showApp()` mitigates but doesn't fully
  eliminate this — a 502 right after a deploy is usually this, not a regression, but confirm by
  reloading before assuming otherwise.

## `nonnas-shared` pin discipline

`requirements.txt` pins `nonnas-shared` to an exact commit hash, not a branch — Render's build
cache has served a stale install from an unpinned URL before. **Bump the hash in
`requirements.txt` every time a `nonnas-shared` change is needed here**, in the same commit or the
one right after.

## Testing

`pytest` from the repo root runs the full suite (`test_handlers.py`, `test_server.py`,
`test_assistant.py`, `test_auth.py`, `test_tools.py`). No `.env` exists in local dev by default —
tests mock `shared_qbo`/`shared_shopify` calls, so they don't need real credentials. Frontend has
no automated tests; verify via the browser (mock-inject data by calling `render*`/`load*`
functions directly in the console — every one is a plain global, not module-scoped) or a real
logged-in session.
