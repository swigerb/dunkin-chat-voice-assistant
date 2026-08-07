#!/usr/bin/env bash
# scripts/resilience_test.sh — Validates deployment resilience on AKS Arc
# Usage: ./scripts/resilience_test.sh [BASE_URL] [NAMESPACE]
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
NS="${2:-dunkin-voice}"
FAILURES=0

pass() { echo "  ✅ PASS: $1"; }
fail() { echo "  ❌ FAIL: $1"; FAILURES=$((FAILURES + 1)); }

echo "============================================"
echo "Resilience Tests — $BASE_URL (ns: $NS)"
echo "============================================"

# --- 1. Deployment Health ---
echo ""
echo "1. Deployment Health"
READY=$(kubectl get deployment dunkin-voice-assistant -n "$NS" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
DESIRED=$(kubectl get deployment dunkin-voice-assistant -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "1")
[ "$READY" = "$DESIRED" ] && pass "Replicas ready: $READY/$DESIRED" || fail "Replicas ready: $READY/$DESIRED"

RESTARTS=$(kubectl get pods -n "$NS" -l app=dunkin-voice-assistant -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo "-1")
[ "$RESTARTS" -lt 5 ] && pass "Restart count: $RESTARTS (< 5)" || fail "Restart count: $RESTARTS (>= 5, possible crash loop)"

# --- 2. Pod Restart Recovery ---
echo ""
echo "2. Pod Restart Recovery"
echo "  Deleting pod to test self-healing..."
POD=$(kubectl get pods -n "$NS" -l app=dunkin-voice-assistant -o jsonpath='{.items[0].metadata.name}')
kubectl delete pod "$POD" -n "$NS" --wait=false > /dev/null 2>&1

echo "  Waiting for new pod to become ready (max 120s)..."
kubectl rollout status deployment/dunkin-voice-assistant -n "$NS" --timeout=120s > /dev/null 2>&1
RS=$?
[ "$RS" -eq 0 ] && pass "Pod recovered after deletion" || fail "Pod did not recover within 120s"

# Verify service is responding after restart
sleep 5
STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/" --max-time 15 2>/dev/null || echo "000")
[ "$STATUS" = "200" ] && pass "Service responding after pod restart → $STATUS" || fail "Service not responding after restart → $STATUS"

# --- 3. Flux GitOps Health ---
echo ""
echo "3. Flux GitOps Reconciliation"
FLUX_READY=$(kubectl get kustomization dunkin-voice-gitops-apps -n flux-system -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "Unknown")
[ "$FLUX_READY" = "True" ] && pass "Flux kustomization is Ready" || fail "Flux kustomization not Ready: $FLUX_READY"

FLUX_MSG=$(kubectl get kustomization dunkin-voice-gitops-apps -n flux-system -o jsonpath='{.status.conditions[?(@.type=="Ready")].message}' 2>/dev/null || echo "")
echo "    Flux status: $FLUX_MSG"

# --- 4. Resource Usage ---
echo ""
echo "4. Resource Usage"
CPU_REQ=$(kubectl get pods -n "$NS" -l app=dunkin-voice-assistant -o jsonpath='{.items[0].spec.containers[0].resources.requests.cpu}' 2>/dev/null || echo "unset")
MEM_LIM=$(kubectl get pods -n "$NS" -l app=dunkin-voice-assistant -o jsonpath='{.items[0].spec.containers[0].resources.limits.memory}' 2>/dev/null || echo "unset")
[ "$CPU_REQ" != "unset" ] && pass "CPU request set: $CPU_REQ" || fail "CPU request not set"
[ "$MEM_LIM" != "unset" ] && pass "Memory limit set: $MEM_LIM" || fail "Memory limit not set"

# --- 5. Network Policy ---
echo ""
echo "5. Network Policy"
NP_COUNT=$(kubectl get networkpolicies -n "$NS" --no-headers 2>/dev/null | wc -l)
[ "$NP_COUNT" -gt 0 ] && pass "NetworkPolicy exists ($NP_COUNT found)" || fail "No NetworkPolicy in namespace"

# --- 6. Secrets Validation ---
echo ""
echo "6. Secrets"
kubectl get secret dunkin-secrets -n "$NS" > /dev/null 2>&1 && pass "dunkin-secrets exists" || fail "dunkin-secrets missing"
kubectl get secret acr-secret -n "$NS" > /dev/null 2>&1 && pass "acr-secret exists" || fail "acr-secret missing"

# --- 7. Ingress Health ---
echo ""
echo "7. Ingress"
ING_ADDR=$(kubectl get ingress dunkin-voice-ingress -n "$NS" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "none")
[ "$ING_ADDR" != "none" ] && pass "Ingress has address: $ING_ADDR" || fail "Ingress has no address"

# --- Summary ---
echo ""
echo "============================================"
if [ "$FAILURES" -eq 0 ]; then
  echo "ALL RESILIENCE TESTS PASSED ✅"
else
  echo "$FAILURES RESILIENCE TEST(S) FAILED ❌"
fi
echo "============================================"
exit "$FAILURES"
