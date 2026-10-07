"""Live smoke test: a 2-minute end-to-end proof that a running cs-agent stack
works against the real store (Shopify reads, local agent pipeline).

Safe by default: NO money moves. Side effects are two local tickets and
refund attempts that are rejected (400) before Shopify is called. The
identity MISMATCH path is intentionally not asserted here — this store's
Shopify plan blocks customer PII for the app (order never carries an email);
that path is covered by tests/test_support_agent.py.

Usage:
    python scripts/smoke_live.py                          # local stack, auto-discovers an order
    python scripts/smoke_live.py --order 1004             # force an order number
    python scripts/smoke_live.py --base https://cs-agent-xxxx.onrender.com \\
        --api-key KEY --skip-dashboard                    # against a deployed instance
    python scripts/smoke_live.py --money 1003             # + one real $1 refund (opt-in)

Exit code 0 = all green (SKIPs are fine), 1 = at least one FAIL.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.getcwd())

RESULTS = []


def ok(name, cond, detail="", status=None):
    RESULTS.append((name, status or ("PASS" if cond else "FAIL")))
    print(f"{status or ('PASS' if cond else 'FAIL')}  {name}  ({detail})")


def skip(name, detail):
    RESULTS.append((name, "SKIP"))
    print(f"SKIP  {name}  ({detail})")


def http(method, url, body=None, headers=None, timeout=120):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, {"raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300]}


def refund_post(base, tid, body, hdr, timeout=60):
    """POST a refund action, tolerating the refund rate limiter.

    The limiter (REFUND_RATE_LIMIT_PER_MINUTE per IP) runs BEFORE idempotency,
    so under burst a legitimate replay can come back 429 — same as any client
    would see. Wait out the window once and retry, then surface whatever comes."""
    url = f"{base}/support/tickets/{tid}/actions/refund"
    st, r = http("POST", url, body, headers=hdr, timeout=timeout)
    if st == 429:
        print("  rate limited — waiting 62s for the window to refill, retrying once")
        time.sleep(62)
        st, r = http("POST", url, body, headers=hdr, timeout=timeout)
    return st, r


def discover_order_number():
    try:
        from integrations.shopify import ShopifyClient

        cli = ShopifyClient()
        if not cli.enabled:
            return None
        st, r = http("GET", f"{cli.base_url}/orders.json?limit=5&status=any", headers=cli.headers)
        if st != 200:
            return None
        orders = r.get("orders", [])
        if not orders:
            return None
        return str(orders[0].get("order_number") or orders[0].get("name", "").lstrip("#"))
    except Exception as e:
        print(f"order discovery failed: {e}")
        return None


def refunds_count(order_number):
    try:
        import asyncio

        from integrations.shopify import ShopifyClient

        cli = ShopifyClient()
        order = asyncio.run(cli.get_order_by_number(str(order_number)))
        return len((order or {}).get("refunds") or [])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base", default="http://localhost:8001")
    ap.add_argument("--dashboard", default="http://localhost:5173")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--order", default=None, help="order number for the resolution checks")
    ap.add_argument(
        "--money",
        default=None,
        metavar="ORDER",
        help="opt-in: also run one real $1 refund + replay on this order",
    )
    ap.add_argument("--skip-dashboard", action="store_true")
    args = ap.parse_args()

    api_key = args.api_key
    if not api_key:
        from agent.config import settings

        api_key = settings.API_KEY.get_secret_value()
    base = args.base.rstrip("/")
    auth = {"X-API-Key": api_key}

    # 1. Health
    st, b = http("GET", f"{base}/health", headers=auth, timeout=30)
    ok(
        "API health",
        st == 200 and b.get("status") == "healthy",
        f"status={st} storage={b.get('checks', {}).get('storage')}",
    )

    # 2. Integration posture
    st, b = http("GET", f"{base}/support/health", headers=auth, timeout=30)
    ok(
        "support health",
        st == 200,
        f"shopify={b.get('shopify_connected')} gorgias={b.get('gorgias_connected')} "
        f"auto_send={b.get('auto_send_enabled')}",
    )

    # 3. Dashboard
    if args.skip_dashboard:
        skip("dashboard reachable", "--skip-dashboard")
    elif (
        "localhost" not in args.dashboard
        and args.dashboard == "http://localhost:5173"
        and "localhost" not in base
    ):
        skip("dashboard reachable", "no --dashboard given for a remote run")
    else:
        try:
            with urllib.request.urlopen(args.dashboard + "/", timeout=15) as resp:
                html = resp.read().decode()
            ok(
                "dashboard reachable",
                resp.status == 200 and "<div" in html,
                f"{args.dashboard} len={len(html)}",
            )
        except Exception as e:
            ok("dashboard reachable", False, str(e))

    # 4. Widget bundle carries the needs_human gating
    st, b = http("GET", f"{base}/chat/widget.js", timeout=30)
    js = b.get("raw", "") if isinstance(b, dict) else str(b)
    ok("widget bundle served", st == 200 and len(js) > 3000, f"len={len(js)}")
    ok("widget needs_human holding line present", "follow up with you shortly" in js)

    # 5. Ticket -> order resolution (identity ALLOW path; see module docstring)
    order_ref = args.order or discover_order_number()
    if not order_ref:
        skip("ticket resolves order", "no order number (pass --order N)")
    else:
        st, t = http(
            "POST",
            f"{base}/support/tickets",
            {
                "customer_email": "smoke-test@example.com",
                "subject": "Smoke check",
                "body": f"Hi, checking in on order #{order_ref} — thanks!",
            },
            headers=auth,
            timeout=180,
        )
        tid = t.get("ticket_id") if st == 200 else None
        ok("ticket created through agent pipeline", bool(tid), f"status={st} id={tid}")
        if tid:
            st2, _ = http("GET", f"{base}/support/tickets/{tid}/order", headers=auth, timeout=30)
            ok("order context resolved and linked", st2 == 200, f"status={st2}")

            # 6. Idempotency WITHOUT money: oversized refund is rejected before
            #    Shopify; the same key must replay the identical rejection.
            body = {"amount": 999999, "reason": "smoke: must be rejected", "notify_customer": False}
            hdr = {**auth, "Idempotency-Key": f"smoke-oversize-{int(time.time())}"}
            s1, r1 = refund_post(base, tid, body, hdr)
            time.sleep(7)  # pace the burst under REFUND_RATE_LIMIT_PER_MINUTE
            s2, r2 = refund_post(base, tid, body, hdr)
            ok(
                "oversized refund rejected (no money can move)",
                s1 >= 400,
                f"{s1} {r1.get('error', '')}",
            )
            ok(
                "same idempotency key -> identical rejection",
                s1 == s2 and r1.get("error") == r2.get("error"),
                f"{s1}/{s2} {r1.get('error')}/{r2.get('error')}",
            )

    # 7. Opt-in money path: one real $1 refund, replayed with the same key
    if args.money:
        before = refunds_count(args.money)
        st, t = http(
            "POST",
            f"{base}/support/tickets",
            {
                "customer_email": "smoke-test@example.com",
                "subject": "Smoke refund check",
                "body": f"Please refund $1 of order #{args.money} as a goodwill gesture. "
                "Thanks!",
            },
            headers=auth,
            timeout=180,
        )
        tid = t.get("ticket_id") if st == 200 else None
        if tid:
            http("GET", f"{base}/support/tickets/{tid}/order", headers=auth, timeout=30)
            key = f"smoke-money-{int(time.time())}"
            hdr = {**auth, "Idempotency-Key": key}
            body = {"amount": 1.0, "reason": "smoke live money check", "notify_customer": False}
            s1, r1 = refund_post(base, tid, body, hdr)
            time.sleep(7)  # pace the burst under REFUND_RATE_LIMIT_PER_MINUTE
            s2, r2 = refund_post(base, tid, body, hdr)
            refund_id_1 = ((r1.get("refund") or {}).get("refund") or {}).get("id")
            refund_id_2 = ((r2.get("refund") or {}).get("refund") or {}).get("id")
            ok(
                "real refund succeeded",
                s1 == 200 and refund_id_1,
                f"status={s1} refund_id={refund_id_1}",
            )
            ok(
                "replay returns same refund, marked replayed",
                s2 == 200 and refund_id_2 == refund_id_1 and r2.get("replayed") is True,
                f"status={s2} replayed={r2.get('replayed')}",
            )
            after = refunds_count(args.money)
            if before is None or after is None:
                skip("exactly one refund on Shopify", "Shopify not reachable")
            else:
                ok("exactly one refund on Shopify", after == before + 1, f"{before} -> {after}")
        else:
            ok("money: ticket created", False, f"status={st}")

    passed = sum(1 for _, s in RESULTS if s == "PASS")
    failed = [n for n, s in RESULTS if s == "FAIL"]
    skipped = sum(1 for _, s in RESULTS if s == "SKIP")
    print(f"\n==== SMOKE: {passed} passed, {skipped} skipped, " f"{len(failed)} failed ====")
    if failed:
        print("failed: " + ", ".join(failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
