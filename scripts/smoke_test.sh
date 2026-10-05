#!/usr/bin/env bash
# Post-deploy smoke test for cs-agent
# Usage: ./scripts/smoke_test.sh https://cs-agent-xxxx.onrender.com <api-key>
set -euo pipefail

BASE_URL="${1:?Usage: $0 <base-url> <api-key>}"
API_KEY="${2:?Usage: $0 <base-url> <api-key>}"

PASS=0
FAIL=0

pass() { echo "  PASS  $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }

echo "==== cs-agent smoke test ===="
echo "Target: $BASE_URL"
echo ""

# 1. Public health
echo "--- [1/6] GET /health ---"
status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")
body=$(curl -s "$BASE_URL/health")
if [ "$status" = "200" ]; then
  pass "/health returned 200"
else
  fail "/health returned $status (expected 200)"
fi
echo "  body: $body"

# 2. Authenticated health
echo "--- [2/6] GET /support/health ---"
status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/support/health" -H "X-API-Key: $API_KEY")
body=$(curl -s "$BASE_URL/support/health" -H "X-API-Key: $API_KEY")
if [ "$status" = "200" ]; then
  pass "/support/health returned 200"
else
  fail "/support/health returned $status (expected 200)"
fi
echo "  body: $body"

# 3. Create a ticket (exercises LLM)
echo "--- [3/6] POST /support/tickets ---"
TS=$(date +%s)
payload=$(cat <<EOF
{
  "customer_email": "smoke-test-$TS@example.com",
  "customer_name": "Smoke Tester",
  "subject": "Smoke test ticket $TS",
  "body": "I ordered item #12345 last week but it still hasn't arrived. Can you check the status?"
}
EOF
)
status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/support/tickets" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d "$payload")
body=$(curl -s -X POST "$BASE_URL/support/tickets" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d "$payload")

if [ "$status" = "200" ] || [ "$status" = "201" ]; then
  pass "POST /support/tickets returned $status"
  # Extract ticket_id from response
  TICKET_ID=$(echo "$body" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
  [ -z "$TICKET_ID" ] && TICKET_ID=$(echo "$body" | grep -o '"ticket_id":"[^"]*"' | head -1 | cut -d'"' -f4)
  [ -z "$TICKET_ID" ] && TICKET_ID=$(echo "$body" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
  echo "  ticket_id: ${TICKET_ID:-unknown}"
else
  fail "POST /support/tickets returned $status (expected 200/201)"
fi
echo "  body: $body"

# 4. List tickets (verify persistence)
echo "--- [4/6] GET /support/tickets ---"
status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/support/tickets" -H "X-API-Key: $API_KEY")
body=$(curl -s "$BASE_URL/support/tickets" -H "X-API-Key: $API_KEY")
if [ "$status" = "200" ]; then
  pass "GET /support/tickets returned 200"
else
  fail "GET /support/tickets returned $status (expected 200)"
fi
COUNT=$(echo "$body" | grep -o '"id"' | wc -l)
echo "  total tickets returned: $COUNT"

# 5. Store registry reachable (agency mode)
echo "--- [5/6] GET /support/stores ---"
status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/support/stores" -H "X-API-Key: $API_KEY")
body=$(curl -s "$BASE_URL/support/stores" -H "X-API-Key: $API_KEY")
if [ "$status" = "200" ]; then
  pass "/support/stores returned 200"
else
  fail "/support/stores returned $status (expected 200)"
fi
echo "  body: $body"

# 6. X-Store-Id middleware rejects unknown stores
echo "--- [6/6] GET /support/tickets with unknown X-Store-Id ---"
status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/support/tickets" \
  -H "X-API-Key: $API_KEY" -H "X-Store-Id: smoke-test-nonexistent")
body=$(curl -s "$BASE_URL/support/tickets" \
  -H "X-API-Key: $API_KEY" -H "X-Store-Id: smoke-test-nonexistent")
if [ "$status" = "404" ] && echo "$body" | grep -q "STORE_NOT_FOUND"; then
  pass "unknown X-Store-Id returned 404 STORE_NOT_FOUND"
else
  fail "unknown X-Store-Id returned $status (expected 404 STORE_NOT_FOUND)"
fi
echo "  body: $body"

# Summary
echo ""
echo "==== smoke test results ===="
echo "  PASSED: $PASS"
echo "  FAILED: $FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo "  RESULT: FAILED"
  exit 1
else
  echo "  RESULT: PASSED"
  exit 0
fi