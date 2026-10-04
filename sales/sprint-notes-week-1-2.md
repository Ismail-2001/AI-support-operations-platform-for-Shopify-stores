# Internal Sprint Notes — Week 1–2: Actions + Calibration

**Status:** Shipped to `main` (this sprint) · **Audience:** internal only (not customer-facing)
**Demo:** pairs with `sales/loom-script.md` — new beats marked below

---

## What shipped

### 1. Three human-approved actions

| Action | Endpoint | Guards |
|---|---|---|
| Cancel order | `POST /support/tickets/{id}/actions/cancel` | 409 if fulfilled/cancelled; `Idempotency-Key` required; rate-limited 10/min; `action_audit` row; resolves ticket |
| Edit shipping address | `POST /support/tickets/{id}/actions/edit-address` | Address validator (address1/city/country/zip); refuses fulfilled/cancelled; audit stores old + new side-by-side |
| Partial / item-level refund | `POST /support/tickets/{id}/actions/refund` (now accepts `refund_line_items`) | **Cumulative** cap (order total − sum of past refunds); line-item existence + quantity validation; `refund_audit.detail` stores the item scope |

- Supporting read: `GET /support/tickets/{id}/order` → line items, shipping address, refundable balance (powers the approval UI).
- Cross-action idempotency: same key on a *different* action = 409 `IDEMPOTENCY_KEY_CONFLICT`.
- Model side: `ActionType` gained `CANCEL_ORDER` / `EDIT_ADDRESS`; `SuggestedAction` gained `address` + `refund_line_items`; prompt rules added; JSON parser maps unknown types to `NONE` (never onto a money-moving action).
- Shopify client: new `cancel_order()` (strict timeout-only retry, reason-enum mapping, restock) + `update_shipping_address()` (refuses fulfilled/cancelled).

### 2. Category-aware auto-send calibration

- Per-category thresholds in `.env`: `AUTO_SEND_MIN_CONFIDENCE_ORDER_STATUS=0.88`, `_SHIPPING=0.88`, `_RETURNS=0.87`, `_PRODUCT_QUESTION=0.90`, `_TECHNICAL=0.90`, `_DEFAULT=0.90` — hard floor `0.80`, blocked categories (`refund, complaint, legal, other`) unsendable at any value.
- Runtime overrides without redeploy: `PUT /support/automation/thresholds` (null clears) → KV `auto_send_min_confidence:<category>`; read through by `graph.decide_auto_send` on every ticket.
- Analytics: `GET /support/analytics/auto-send` → per-category current vs suggested threshold, samples, edit rate. Suggestion rule (`agent/automation.suggest_threshold`): needs ≥50 reviewed samples; raise if a bucket at/above current has >10% edit rate; lower if a bucket below has ≥15 samples at 0% edit rate.
- Global `DAILY_COST_CAP_USD` breaker untouched — still force-disables everything.

### 3. Dashboard

- New `ActionApprovalPanel` (cancel + edit address): order preview, side-by-side address diff, shipped/cancelled blocking, required-field validation, two-step confirm.
- `RefundApprovalPanel` rewritten: refundable-cap enforcement, line-item checkboxes + quantities → `refund_line_items`.
- **Bug fixed:** resend suggestions were previously submitted to the *refund* endpoint → added `api.approveResend`; api.ts error extraction now prefers `message`.
- Analytics page: "Auto-send thresholds" table with Apply/Reset + blocked-category chips.

### 4. Evals

- Dataset: 15 → **19 cases** — `cancel_order_unfulfilled_suggests_action`, `edit_address_apartment_typo_suggests_action`, `partial_refund_single_item_suggests_action`, `cancel_delivered_order_no_action`.
- Scoring: new `expected_action_type` check (str | list, enum-safe, None-safe).

---

## Verification status

| Gate | Result |
|---|---|
| `pytest tests/` | **228 passed** (new: `test_actions.py`, `test_automation.py`, 4 eval-harness tests) |
| `npm test` (vitest) | **80 passed / 11 suites** (new: ActionApprovalPanel, RefundApprovalPanel) |
| `ruff check` + `ruff format` | clean |
| `tsc --noEmit` + `vite build` | clean |
| `python -m evals.run_evals` (19 cases) | **BLOCKED — environmental, not quality** |

**Eval blocker (needs one action from Ismail):** OpenRouter account out of credits (HTTP 402) and the Google fallback hit its free-tier daily quota (HTTP 429, resets ~12h). The harness itself is verified — all 19 cases run, score, and save reports correctly (first 4 passed on Gemini before quota). Re-run once either provider is available: `python -m evals.run_evals`.

**Still open:** rotate the 4 leaked keys (OpenRouter, Google, dashboard API key, Shopify token); paste `SHOPIFY_SETUP_VIDEO_URL` into `SetupPage.tsx`.

---

## Demo beats to add to the Loom take

1. **Cancel (0:30):** open the unfulfilled-order ticket → draft proposes cancel → Approve → show "Order cancelled" + `action_audit` row; retry with the same key → identical response, no double-execute.
2. **Address fix (0:30):** apartment typo → approval panel shows current vs requested side-by-side → approve → audit message shows old → new.
3. **Calibration (0:45):** Analytics → Auto-send thresholds → shows returns at 0.87 vs product questions at 0.90, suggested move after 50+ samples, Apply without redeploying.
4. **Safety line:** "The agent proposes; you approve. Nothing that costs money or cancels an order can run without a click in this dashboard — that's enforced in code, not in the prompt."
