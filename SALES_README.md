# Customer Support AI Employee — for Shopify

> **Deploy an AI agent into your Shopify store in 10 minutes. It answers orders, handles returns, suggests refunds, and escalates — all inside your Gorgias workflow.**

---

## For US ecommerce brands tired of answering the same questions

Every day, your team answers the same 5 questions:

> *"Where's my order?" — "Can I return this?" — "When will it ship?" — "Do you have this in size M?" — "I never received item #1042."*

Your support team spends **60-70% of their time** on these repetitive tickets. Your customers wait hours for answers an AI could give in seconds.

**cs-agent is different.** It's not a generic chatbot with a script. It connects directly to your Shopify store — reads real order status, tracking numbers, product data, and policies — before it responds. No hallucinations. No "I don't have access to that information." Just answers that sound like your best agent.

---

## What it handles automatically

| Customer question | What the AI does |
|---|---|
| *"Where's my order #1042?"* | Pulls real tracking from Shopify, responds with status + carrier link |
| *"I want a return"* | Checks your return policy from your KB, starts the process |
| *"This is the 3rd time I'm asking"* | Detects escalation, flags as urgent, routes to your top agent |
| *"My order arrived damaged"* | Suggests a refund amount with reason — you approve with one click |
| *"Can you resend it?"* | Creates a replacement order — you approve, Shopify ships it |
| *"Is this waterproof?"* | Searches your product catalog, answers from your spec sheet |

---

## How it works

```
Customer sends a message via email/chat/social
        │
        ▼
cs-agent reads your Shopify store (real order data)
        │
        ▼
cs-agent searches your policies + product catalog (RAG)
        │
        ▼
cs-agent drafts a reply with a confidence score
        │
        ├── Confidence ≥ 85% & category allows auto-send
        │   └── Reply sent to customer automatically
        │
        └── Confidence < 85% OR refund/complaint/legal category
            └── Draft posted as internal note for human review
```

You keep your existing stack (Shopify + Gorgias). The AI plugs in between.

---

## Pricing — for US Shopify stores

| Plan | For | Setup | Monthly | Best For |
|------|-----|:-----:|:-------:|----------|
| **Starter** | 1 store, you are support | $1,500 | $0 | Solo operators, bootstrapped brands |
| **Growth** | 1 store + support team | $3,000 | $1,000 | Brands with 500-5000 orders/month |
| **Enterprise** | Multiple stores | Custom | Custom | Agencies, 5+ store portfolios, custom SLA |

**No per-ticket fees. No usage caps. No surprise bills.** Your only variable cost is LLM API usage (~$5-50/month depending on ticket volume).

---

## What it costs you NOT to have it

| Metric | Without AI | With AI |
|--------|:-:|:-:|
| First response time | 4-6 hours | 2-5 minutes |
| Tickets handled per agent per hour | 2-3 | 8-12 |
| Support team size needed (for 500 tickets/mo) | 2-3 agents | 1 agent |
| Monthly team cost | $8,000-12,000 | $4,000-6,000 |

**Typical payback period: 1-2 months.**

---

## Safety architecture — your store, your rules

- **Refunds** are suggested by AI, **approved** by you via one click
- **Replacement orders** are created by AI, **approved** by you
- **Low-confidence drafts** (< 85%) never go to customers
- **Angry customers** (very_negative sentiment) always get a human
- **3rd follow-up** auto-escalates to urgent + forces human review
- **Daily cost cap** — auto-send disabled when LLM spend exceeds budget
- **Every action has an audit trail** — who approved what, when, and which Shopify order

---

## Built for Shopify by someone who knows Shopify

This isn't a wrapper around ChatGPT with a Shopify prompt. It's a purpose-built agent that:
- Reads your orders through the **Shopify Admin API** (read_orders scope — read-only for lookups)
- Posts replies and internal notes through **Gorgias REST API**
- Stores everything in **your database** (self-hosted, your data never touches a third party)
- Runs on **your infrastructure** (Render, AWS, or any VPS)

---

## Contact

**Ismail Sajid** — Principal AI Engineer

- GitHub: [github.com/Ismail-2001](https://github.com/Ismail-2001)
- Project repo: [customer-support-ai-employee](https://github.com/Ismail-2001/customer-support-ai-employee)

Schedule a 15-minute demo. I'll connect cs-agent to your Shopify store and show you handling real tickets live.
