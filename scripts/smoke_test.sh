#!/usr/bin/env bash
# scripts/smoke_test.sh — Post-deploy smoke tests for dunkin-voice-assistant
# Usage: ./scripts/smoke_test.sh [BASE_URL]
# Exit code 0 = all passed, non-zero = failures
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
FAILURES=0

pass() { echo "  ✅ PASS: $1"; }
fail() { echo "  ❌ FAIL: $1"; FAILURES=$((FAILURES + 1)); }

echo "============================================"
echo "Smoke Tests — $BASE_URL"
echo "============================================"

# --- 1. HTTP Health Check ---
echo ""
echo "1. HTTP Health Checks"
STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/" --max-time 10 2>/dev/null || echo "000")
[ "$STATUS" = "200" ] && pass "GET / → $STATUS" || fail "GET / → $STATUS (expected 200)"

STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/crew" --max-time 10 2>/dev/null || echo "000")
[ "$STATUS" = "200" ] && pass "GET /crew → $STATUS" || fail "GET /crew → $STATUS (expected 200)"

# --- 2. Response Content Validation ---
echo ""
echo "2. Response Content"
BODY=$(curl -sk "$BASE_URL/" --max-time 10 2>/dev/null)
echo "$BODY" | grep -q "Coffee Chat" && pass "Guest UI contains 'Coffee Chat'" || fail "Guest UI missing 'Coffee Chat'"

BODY=$(curl -sk "$BASE_URL/crew" --max-time 10 2>/dev/null)
echo "$BODY" | grep -q "Dunkin Drive-Thru Dashboard" && pass "Crew UI contains dashboard title" || fail "Crew UI missing dashboard title"

# --- 3. WebSocket Endpoint ---
echo ""
echo "3. WebSocket Endpoint"
STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/realtime" --max-time 5 2>/dev/null || echo "000")
[ "$STATUS" = "400" ] && pass "GET /realtime → $STATUS (expected 400 for non-WS)" || fail "GET /realtime → $STATUS (expected 400)"

# --- 4. API Endpoints ---
echo ""
echo "4. API Endpoints"
STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/crm/customers" --max-time 10 2>/dev/null || echo "000")
[ "$STATUS" = "200" ] && pass "GET /crm/customers → $STATUS" || fail "GET /crm/customers → $STATUS (expected 200)"

STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/simulator/demo" --max-time 10 2>/dev/null || echo "000")
[ "$STATUS" = "200" ] && pass "GET /simulator/demo → $STATUS" || fail "GET /simulator/demo → $STATUS (expected 200)"

# --- 5. Security Headers ---
echo ""
echo "5. Security Headers"
HEADERS=$(curl -sI "$BASE_URL/" --max-time 10 2>/dev/null)
echo "$HEADERS" | grep -qi "X-Frame-Options" && pass "X-Frame-Options present" || fail "X-Frame-Options missing"
echo "$HEADERS" | grep -qi "X-Content-Type-Options" && pass "X-Content-Type-Options present" || fail "X-Content-Type-Options missing"

# --- 6. Static Assets ---
echo ""
echo "6. Static Assets"
STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/favicon.ico" --max-time 10 2>/dev/null || echo "000")
[ "$STATUS" = "200" ] && pass "GET /favicon.ico → $STATUS" || fail "GET /favicon.ico → $STATUS (expected 200)"

# --- 7. HTTPS (if available) ---
echo ""
echo "7. HTTPS"
HTTPS_URL="${BASE_URL/http:/https:}"
STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$HTTPS_URL/" --max-time 10 2>/dev/null || echo "000")
[ "$STATUS" = "200" ] && pass "HTTPS GET / → $STATUS" || fail "HTTPS GET / → $STATUS (expected 200)"

# --- Summary ---
echo ""
echo "============================================"
if [ "$FAILURES" -eq 0 ]; then
  echo "ALL TESTS PASSED ✅"
else
  echo "$FAILURES TEST(S) FAILED ❌"
fi
echo "============================================"
exit "$FAILURES"
