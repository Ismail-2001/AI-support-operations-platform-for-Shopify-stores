# DEPLOYMENT CHECKLIST — cs-agent on Render

## Prerequisites
- GitHub account with `AI-support-operations-platform-for-Shopify-stores` repo pushed
- Render account (sign up at https://render.com)
- Shopify store, Gorgias account, OpenRouter API key ready

---

## 1. Create Blueprint on Render

1. Go to https://dashboard.render.com
2. Click **New +** → **Blueprint**
3. Connect your GitHub account
4. Select **Ismail-2001/AI-support-operations-platform-for-Shopify-stores**
5. Render auto-detects `render.yaml` → click **Apply**

---

## 2. Set Environment Variables (Manual Entry Required)

The Blueprint will pre-fill all vars from `render.yaml`. **Every var marked `sync: false` MUST be typed in manually** in Render's dashboard. Do NOT commit secrets.

### Required `sync: false` Variables (copy from your `.env`):

| Variable | Where to Get It |
|----------|-----------------|
| `TENANT_NAME` | Your label for this deployment (e.g., `acme-corp-support`) |
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys (create key, copy) |
| `GOOGLE_API_KEY` | https://aistudio.google.com/apikey — **REQUIRED for Knowledge Base / RAG search**, even if you use Groq/OpenRouter for chat |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/ (optional, fallback) |
| `SHOPIFY_SHOP_DOMAIN` | Your store admin URL → `your-store.myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | Shopify Admin → Settings → Apps and sales channels → Develop apps → Create app → `read_orders` scope → Install → Admin API access token |
| `GORGIAS_DOMAIN` | Your Gorgias subdomain (e.g., `your-store` from `your-store.gorgias.com`) |
| `GORGIAS_EMAIL` | The email you log into Gorgias with |
| `GORGIAS_API_KEY` | Gorgias → Settings → REST API → Generate API Key |
| `GORGIAS_WEBHOOK_SECRET` | Any random string; **must match** the `x-webhook-secret` header you set in Gorgias webhook config |
| `API_KEY` | Generate locally: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `INBOUND_WEBHOOK_SECRET` | Same generation method as `API_KEY` |

### Optional integration variables (leave blank to disable)

| Variable | Where to Get It |
|----------|-----------------|
| `RECHARGE_API_TOKEN` | Recharge merchant portal → Apps → API tokens (pause/skip/cancel/frequency) |
| `SKIO_API_TOKEN` | Skio dashboard → Integrations → API (alternative subscription provider) |
| `SHIPENGINE_API_KEY` | https://app.shipengine.com → Settings → API Keys (return labels) |
| `RETURN_ADDRESS_*` | Your return origin address — required to actually buy a return label |

### Pre-filled `sync: true` / `value:` Variables (auto-set by Blueprint):
- `OPENROUTER_MODEL` = `openai/gpt-4o-mini`
- `FALLBACK_MODEL` = `claude-haiku-4-5-20251001`
- `AUTO_SEND_ENABLED` = `false`
- `AUTO_SEND_MIN_CONFIDENCE` = `0.85`
- `AUTO_SEND_BLOCKED_CATEGORIES` = `refund,complaint,legal,other,subscription`
- `DB_PATH` = `cs_agent.db` (ephemeral — wiped on deploy)
- `ENV` = `production`
- `REQUIRE_API_KEY` = `true`
- `ALLOWED_ORIGINS` = `""`
- `RATE_LIMIT_PER_MINUTE` = `60`
- `REFUND_RATE_LIMIT_PER_MINUTE` = `10`
- `PYTHON_VERSION` = `3.12.0`

---

## 3. Deploy & Verify

1. Click **Deploy** — wait 2–4 min for build
2. Note the service URL: `https://cs-agent-xxxx.onrender.com`

### Quick Health Check
```bash
# Public health
curl https://cs-agent-xxxx.onrender.com/health

# Authenticated health (requires API_KEY)
curl https://cs-agent-xxxx.onrender.com/support/health \
  -H "X-API-Key: YOUR_API_KEY"
```

Expected: both return `{"status":"healthy",...}`

---

## 4. Connect Gorgias Webhooks

In Gorgias: **Settings → REST API → Webhooks**

### Webhook 1: Ticket Created
- **URL**: `https://cs-agent-xxxx.onrender.com/support/webhooks/gorgias/ticket-created`
- **Events**: `ticket_created`
- **Headers**: `x-webhook-secret: YOUR_GORGIAS_WEBHOOK_SECRET`

### Webhook 2: Message Created
- **URL**: `https://cs-agent-xxxx.onrender.com/support/webhooks/gorgias/message-created`
- **Events**: `message_created`
- **Headers**: `x-webhook-secret: YOUR_GORGIAS_WEBHOOK_SECRET`

---

## 5. Run Post-Deploy Smoke Test

```bash
./scripts/smoke_test.sh https://cs-agent-xxxx.onrender.com YOUR_API_KEY
```

Expected: all 6 checks PASS

---

## 5b. Live Integration Verification (do this BEFORE trusting auto-send)

The unit tests use fake Shopify/Gorgias. Before launch you must prove the real
integrations work against production credentials:

1. **Shopify lookup** — send a real ticket containing a real order number;
   confirm the reply includes correct order status/items (check Render logs).
2. **Gorgias round-trip** — create a test ticket in Gorgias, confirm the agent
   receives the webhook and posts a reply/internal note.
3. **Idempotency** — re-send the same Gorgias webhook payload; confirm it is
   processed **once** (no duplicate reply). Check the audit row count.
4. **Cost cap** — temporarily set `DAILY_COST_CAP_USD=0.01`, send traffic,
   confirm `AUTO_SEND` is force-disabled and replies stop at the cap.
5. **Multi-store routing** — `GET /support/stores` returns 200; calling any
   endpoint with `X-Store-Id: bogus` returns `404 STORE_NOT_FOUND` (the smoke
   test now covers both).

Only after all five pass should you consider flipping `AUTO_SEND_ENABLED=true`.

---

## 6. (Optional) Deploy Operator Dashboard

Separate Render **Static Site**:
- Build: `cd dashboard && npm install && npm run build`
- Publish dir: `dashboard/dist`
- Env var: `VITE_API_BASE_URL=https://cs-agent-xxxx.onrender.com`

---

## ⚠️ Free Tier Notes

- The web service runs on Render's **free plan** (spins down after 15 min idle;
  first request after idle takes ~30–60 s to wake).
- **No persistent disk on free plan** — Render does not allow disk attachments
  for free-tier web services. This means the SQLite DB is **ephemeral**: every
  deploy or restart wipes all tickets, audit history, and the cost ledger.
  Acceptable for MVP/evaluation. Upgrade to Render **Starter** ($7/month) and
  re-add a `disk:` block when you need data to survive deploys.
- Keep `AUTO_SEND_ENABLED=false` for the first 1–2 weeks; review drafts in
  Gorgias, then flip to `true` once you trust the agent on that client.

---

## 🗄️ Production Storage (paying clients)

**Free/demo → SQLite on the working directory is fine.** Data loss on deploy
is acceptable while you are evaluating.

**Paying client → persistent storage is a go-live blocker.** Do this before
handing over credentials:

### Option A — Render attached disk (SQLite, smallest change)

1. In `render.yaml`: set `plan: starter` (disks require Starter+ / $7 mo).
2. In `render.yaml`: uncomment the `disk:` block (mounts `/var/data`, 1 GB).
3. In `render.yaml`: set `DB_PATH=/var/data/cs_agent.db`
   (absolute paths are used verbatim; parent dir is created on boot).
4. Redeploy, then verify:

```bash
curl https://cs-agent-xxxx.onrender.com/health
# -> "storage": "persistent"   ✅  (NOT "ephemeral")
```

The dashboard Settings page also shows a warning banner while storage is
ephemeral — it must be gone before go-live.

### Option B — Postgres (Supabase/Neon) for larger deployments

Postgres is **not implemented yet** (storage is isolated behind
`agent/storage.py` to make the swap straightforward). Until it lands, use
Option A and avoid multi-worker setups — SQLite needs a single writer.

| Check | Free/demo | Paying client |
|---|---|---|
| `checks.storage` in `/health` | `"ephemeral"` OK | must be `"persistent"` |
| Backup story | none needed | export `DB_PATH` file regularly (cron / Render scheduled job) |
| `AUTO_SEND_ENABLED` | `false` | `false` until 1–2 weeks of reviewed drafts |

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Build fails | `render.yaml` `buildCommand` matches `pip install -r requirements.txt` |
| 500 on health | Check logs → usually missing `TENANT_NAME` or `OPENROUTER_API_KEY` |
| Gorgias 401 | Webhook secret mismatch — must match exactly |
| Shopify 404 | Wrong `SHOPIFY_SHOP_DOMAIN` or missing `read_orders` scope |
| Dashboard blank | `VITE_API_BASE_URL` must point to the deployed API URL |

---

## Quick Commands

```bash
# Local dry-run (mirrors Render build)
python3 -m venv /tmp/render-test && source /tmp/render-test/bin/activate
pip install -r requirements.txt
uvicorn api.main:app --host 0.0.0.0 --port 8000

# Local smoke test
./scripts/smoke_test.sh http://localhost:8000 test-key

# Check env sync locally
python scripts/check_env_sync.py
```