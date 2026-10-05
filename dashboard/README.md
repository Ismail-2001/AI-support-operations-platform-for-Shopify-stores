# Support Agent Console

A dashboard for operating the `cs-agent` customer support agent — review AI drafts,
approve refunds, watch confidence calibration and spend, and manage the knowledge base.
Talks directly to your deployed `cs-agent` API; nothing is faked or mocked.

## Setup

```bash
npm install
npm run dev
```

Opens at `http://localhost:5173`. On first load it asks for:
- **API base URL** — your deployed `cs-agent` instance (or `http://localhost:8001` if
  running it locally)
- **API key** — the same `API_KEY` you set in `cs-agent`'s `.env`

Both are stored only in this browser's `localStorage` — nothing is sent anywhere except
straight to your own backend.

## Deploying

```bash
npm run build
```

Outputs static files to `dist/` — deploy that folder to Vercel, Netlify, Render's static
site hosting, or any static host. There's no server component to this app; it's a pure
client that talks to your `cs-agent` API over HTTPS. Make sure `ALLOWED_ORIGINS` in
`cs-agent`'s `.env` includes wherever you deploy this dashboard, or the browser will
block the requests via CORS.

## What's in here

| Screen | What it does |
|---|---|
| **Tickets** | Filterable list, each row shows category/priority and the AI's confidence at a glance |
| **Ticket detail** | Full conversation thread + a decision panel: confidence, reasoning, edit-and-send the draft, approve/deny refunds, cancel/edit-address, subscription operations (pause/skip/cancel/frequency/address), return-label purchase, expand the raw pipeline trace |
| **Analytics** | Volume, confidence calibration chart, edit-rate-by-category, and daily LLM spend |
| **Knowledge base** | Sync from Shopify, add FAQ content manually, test what the agent would retrieve for a given question |
| **Stores** | Register stores for agency mode, attach Shopify tokens, activate a store from the sidebar switcher — every API call then carries that store's `X-Store-Id` |

## Design notes

The one visual idea this whole app is built around: **confidence rendered as an
instrument reading**, not a number buried in a table cell. The same gradient bar
(rose → gold → teal) appears on every ticket row, the decision panel, and the
calibration chart — because confidence calibration is the actual product concern this
tool exists to surface, not a detail.

Palette is a pale instrument-panel blue-gray (`#EEF1F4`) with a deep-ink sidebar
(`#12202E`), deliberately not the cream-and-terracotta or near-black-plus-neon defaults.
Three signal colors carry meaning throughout: **gold** = awaiting review, **teal** =
trusted/verified, **rose** = escalated — never used decoratively.

Typography: Fraunces (serif, page titles only — gives the console personality without
fighting legibility in dense tables) + IBM Plex Sans (UI and body text) + IBM Plex Mono
(ticket IDs, timestamps, costs, percentages — reinforces the "instrument readout" feel).

## Refund safety

The refund panel requires an explicit confirm step and generates a fresh
`crypto.randomUUID()` as the `Idempotency-Key` on every submit — matching the backend's
idempotency contract, so a double-click or network retry can't double-refund.

## Known limitation

Bundle is ~553KB (mostly `recharts`). Fine for an internal ops tool used by a handful of
people; if it ever needs to feel snappier on a slow connection, code-split the Analytics
page with `React.lazy()` so Tickets loads without pulling in the charting library.
