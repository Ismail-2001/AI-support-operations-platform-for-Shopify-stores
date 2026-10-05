# Real-Store Verification Runbook

Manual verification of the money-touching actions against a **real Shopify store**.
The automated suite (`pytest tests/`, 418 tests) proves the logic with fakes; this
runbook proves the *integrations* with production APIs. Run it before trusting the
agent with a paying client, and after any change to `integrations/` or the action
endpoints.

**Why this is manual:** it needs real credentials and spends real (small) amounts
of money. It cannot run in CI or from an environment without store credentials.

---

## 0. Prerequisites

| Thing | Notes |
|---|---|
| Shopify **development store** | Free via Shopify Partners → Stores → Test your app. No real customers. |
| Recharge and/or Skio test account | For subscription ops. Recharge: dev stores get a free dev shop. |
| ShipEngine account | Free trial credit covers a few labels. **Label purchase spends money.** |
| A running instance | `python -m uvicorn api.main:app` locally, or the Render URL. |
| `API_KEY` | Matches your env; sent as `X-API-Key` on every call below. |

Set the integration credentials either in `.env` (single store) **or per store**
(recommended, exercises the multi-store path):

```bash
# Per-store settings (registry, survives env changes, redacted in responses)
curl -X POST "$BASE/support/stores" -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{
  "name": "Acme Dev",
  "shop_domain": "acme-dev.myshopify.com",
  "shopify_access_token": "<admin API token>",
  "recharge_api_token": "<recharge token>",
  "shipengine_api_key": "<shipengine key>",
  "subscription_provider": "auto",
  "return_address": {"name": "Acme Returns", "address1": "1 Return Way", "city": "Portland", "state": "OR", "zip": "97201", "country": "US"},
  "return_window_days": 30
}'
# Save the returned id as $STORE, then add to every call: -H "X-Store-Id: $STORE"
```

Expected: `201`, and the response shows `has_shipengine_token: true` /
`has_recharge_token: true` but **never** the raw tokens.

---

## 1. Order actions (cancel / edit address / refund / resend)

For each: create a ticket whose message contains a real order number from the dev
store, confirm the order link, then fire the action.

```bash
TID=$(curl -s -X POST "$BASE/support/tickets" -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d '{
    "customer_email": "qa@example.com",
    "subject": "QA cancel check",
    "body": "Please cancel order #1001, I ordered by mistake."
  }' | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

curl -s "$BASE/support/tickets/$TID/order" -H "X-API-Key: $API_KEY"   # must return the order
```

| Check | Call | Pass criteria |
|---|---|---|
| Cancel works | `POST .../actions/cancel` with `Idempotency-Key: qa-cancel-1`, body `{"reason":"qa"}` | `200`; Shopify admin shows the order **cancelled**, inventory restocked |
| Cancel replay | Same exact call again | `200` with `"replayed": true`; Shopify still shows one cancellation |
| Cancel already-cancelled | Same key again after cancel, or a fresh key on the cancelled order | `409` `ORDER_ALREADY_CANCELLED` |
| Cross-family key reuse | Take the refund key from step below, call `/actions/cancel` with it | `409` `IDEMPOTENCY_KEY_CONFLICT` |
| Edit address works | `POST .../actions/edit-address` key `qa-addr-1`, `{"address":{"address1":"9 QA St","city":"Springfield","state":"IL","zip":"62704","country":"US"}}` | `200` with old + new address; Shopify shows the new address |
| Edit blocked after fulfillment | Same call on an order that already shipped | `409` `ORDER_ALREADY_FULFILLED` |
| Partial refund works | `POST .../actions/refund` key `qa-refund-1`, `{"amount": 1.00, "reason":"qa"}` | `200`; Shopify order shows a $1.00 refund |
| Refund beyond refundable | `{"amount": 99999}` | `400`/`422` refund-exceeded error; **no** money moved |
| Refund failure audit | (hard to force deliberately - skip unless you have a declining gateway) | On any real failure: `502` `REFUND_FAILED`, and the audit row for the key has `status=failed` |
| Resend order | `POST .../actions/resend-order` key `qa-resend-1` | `200`; a new draft order exists in Shopify |

**Verify the audit trail** from the dashboard ticket trace (`GET
/support/tickets/{id}/trace`) - each accepted action appears exactly once with its
idempotency key; rejected attempts write nothing.

## 2. Subscription ops (Recharge / Skio)

```bash
curl -s "$BASE/support/tickets/$TID/subscriptions" -H "X-API-Key: $API_KEY"
# -> subscriptions[] with real id/next_charge_date, configured: true
```

Run all five with fresh keys on `POST .../actions/subscription`
(`{"subscription_id": "...", "operation": "...", "reason": "qa"}`, plus `address`
for `update_address` and `{"frequency":{"unit":"week","count":2}}` for
`change_frequency`):

| Operation | Pass criteria (check in Recharge/Skio dashboard) |
|---|---|
| `pause` | Next charge pushed out (~30 days with default `SUBSCRIPTION_PAUSE_DAYS`) |
| `skip` | Next charge moved one billing cycle; billing otherwise unchanged |
| `cancel` | Subscription cancelled in the provider; second attempt → `409` |
| `update_address` | Future orders ship to the new address |
| `change_frequency` | Billing cadence now every 2 weeks |

Wrong-state cases must 409 with a clear code (skip with no next charge, cancel
twice, pause a cancelled sub) and write a **failed** audit row - confirm via trace.

Payment-method change (if asked): response must be portal-link advice with
`expected_action_type: none`-style honesty - no card data anywhere.

## 3. Return label (ShipEngine) - spends money

```bash
curl -s "$BASE/support/tickets/$TID/return-eligibility" -H "X-API-Key: $API_KEY"  # eligible: true
curl -s "$BASE/support/tickets/$TID/return-rates"       -H "X-API-Key: $API_KEY"  # rates[] with real $ amounts
```

1. **Rates before purchase:** confirm `rates[]` carries live amounts and
   `cheapest_rate_id` points at the lowest. In the dashboard approval panel the
   same rates render with the cost in the confirm copy.
2. **Buy the label:** `POST .../actions/return-label`, key `qa-return-1`,
   `{"reason":"qa", "rate_id":"<cheapest>"}`. Pass criteria: `200` with
   `tracking_number` + `label_url` + `cost_usd` equal to the quoted rate; the
   label exists in the ShipEngine dashboard; the ticket moves to
   `awaiting_customer` and the order gets the return tag in Shopify.
3. **Replay:** same key → `200` `"replayed": true`, **no second label** in
   ShipEngine.
4. **Not-configured honesty:** clear `shipengine_api_key` via
   `PATCH /support/stores/{id}` (`{"shipengine_api_key": ""}`) → eligibility now
   reports `configured: false` with `SHIPENGINE_API_KEY` in `missing_settings`,
   and the purchase attempt fails with `SHIPENGINE_NOT_CONFIGURED` instead of a
   silent error. Restore the key afterwards.
5. **No rates:** temporarily use an unsupported origin/destination (e.g. an
   international address your account can't rate) → `return-rates` returns `422`
   `NO_RATES_AVAILABLE` and the dashboard disables approval.

## 4. Multi-store isolation

1. Register a second store, give each store a **different** ShipEngine/Recharge
   token (or leave store B blank).
2. Call `GET .../return-rates` with `X-Store-Id: <A>` then `<B>` - each must use
   its own credentials; store B with a blanked token must return
   `SHIPENGINE_NOT_CONFIGURED` **even though env vars still hold a key**.
3. `X-Store-Id: bogus` → `404 STORE_NOT_FOUND`.
4. `GET /support/stores/{id}` never returns a raw token (write-only credentials).

## 5. Safety nets

- Every action endpoint without `Idempotency-Key` → `422`.
- Creating a ticket about refunds/cancellations must **not** auto-send: with
  `AUTO_SEND_ENABLED=true` and default `AUTO_SEND_BLOCKED_CATEGORIES`, the reply
  stays a draft for `refund`, `complaint`, `subscription` categories.
- Circuit health is visible: `GET /health` includes `checks.circuits` with
  `shopify`/`gorgias`/... in state `closed` on a healthy deployment.
- Dead-letter redrive works: if a webhook is ever processed with a forced
  failure, `GET /support/dead-letters` shows the payload and
  `POST /support/dead-letters/{id}/retry` replays it to a normal 200.

---

## Sign-off checklist

| # | Check | Result |
|---|---|---|
| 1 | Cancel/edit-address/refund/resend behave per §1 (incl. replay + 409s) | |
| 2 | All five subscription ops verified in the provider dashboard (§2) | |
| 3 | Return label bought once, quoted cost = charged cost (§3) | |
| 4 | Not-configured and no-rates paths are honest (§3.4-3.5) | |
| 5 | Per-store credentials isolated, redacted (§4) | |
| 6 | Approval + idempotency + blocked-category gates hold (§5) | |

Only after all six pass should `AUTO_SEND_ENABLED=true` be considered for that
store. Re-run the affected sections after any change under `integrations/`.
