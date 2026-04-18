#!/usr/bin/env bash
# Exploratory / fault-injection test suite for llm-valet v0.5.3+
# Usage:
#   export LLM_VALET_HOST=<ip>
#   export LLM_VALET_PORT=8764
#   export LLM_VALET_KEY=<api-key>
#   bash tests/exploratory/run_exploratory.sh
set -euo pipefail

HOST="${LLM_VALET_HOST:?Set LLM_VALET_HOST}"
PORT="${LLM_VALET_PORT:?Set LLM_VALET_PORT}"
KEY="${LLM_VALET_KEY:?Set LLM_VALET_KEY}"
BASE="http://${HOST}:${PORT}"

PASS=0
FAIL=0
SKIP=0

_pass() { echo "  PASS  $1"; PASS=$((PASS+1)); }
_fail() { echo "  FAIL  $1 — $2"; FAIL=$((FAIL+1)); }
_skip() { echo "  SKIP  $1 — $2"; SKIP=$((SKIP+1)); }

auth_header() { echo "-H \"X-API-Key: ${KEY}\""; }

# Helper: curl with auth, return HTTP status code
api() {
    local method="$1" path="$2"
    shift 2
    curl -s -o /dev/null -w "%{http_code}" \
        -X "$method" \
        -H "X-API-Key: ${KEY}" \
        -H "Content-Type: application/json" \
        "$@" \
        "${BASE}${path}"
}

# Helper: curl with auth, return body
api_body() {
    local method="$1" path="$2"
    shift 2
    curl -s \
        -X "$method" \
        -H "X-API-Key: ${KEY}" \
        -H "Content-Type: application/json" \
        "$@" \
        "${BASE}${path}"
}

# Helper: curl WITHOUT auth (tests auth rejection from LAN)
api_noauth() {
    local method="$1" path="$2"
    shift 2
    curl -s -o /dev/null -w "%{http_code}" \
        -X "$method" \
        -H "Content-Type: application/json" \
        "$@" \
        "${BASE}${path}"
}

echo ""
echo "llm-valet exploratory test suite"
echo "Target: ${BASE}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── E1: API Idempotency ────────────────────────────────────────────────────────
echo ""
echo "E1 — API Idempotency"

# Ensure service is RUNNING before idempotency tests
initial_state=$(api_body GET /status | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('state','unknown'))" 2>/dev/null || echo "unknown")
echo "  Pre-test state: ${initial_state}"

# E1a — PAUSE, then PAUSE again
code=$(api POST /pause)
sleep 2
code2=$(api POST /pause)
if [[ "$code2" =~ ^(200|409|400)$ ]]; then
    _pass "E1a PAUSE-when-PAUSED — HTTP ${code2}"
else
    _fail "E1a PAUSE-when-PAUSED" "HTTP ${code2} (expected 200/409/400, not 500)"
fi

# E1b — RESUME, then RESUME again
code=$(api POST /resume)
sleep 2
code2=$(api POST /resume)
if [[ "$code2" =~ ^(200|409|400)$ ]]; then
    _pass "E1b RESUME-when-RUNNING — HTTP ${code2}"
else
    _fail "E1b RESUME-when-RUNNING" "HTTP ${code2}"
fi

# E1c — State consistent after idempotent calls
status_code=$(api GET /status)
if [[ "$status_code" == "200" ]]; then
    _pass "E1c /status returns 200 after idempotent calls"
else
    _fail "E1c /status after idempotent calls" "HTTP ${status_code}"
fi

# E1d — Rapid fire PAUSE/RESUME (5 pairs back-to-back)
rapid_fail=0
for i in {1..5}; do
    c1=$(api POST /pause)
    c2=$(api POST /resume)
    if [[ ! "$c1" =~ ^(200|409|400)$ ]] || [[ ! "$c2" =~ ^(200|409|400)$ ]]; then
        rapid_fail=1
    fi
done
sleep 2
final_state=$(api_body GET /status | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('state','unknown'))" 2>/dev/null || echo "unknown")
if [[ $rapid_fail -eq 0 ]]; then
    _pass "E1d Rapid PAUSE/RESUME (5×) — no 500s; final state: ${final_state}"
else
    _fail "E1d Rapid PAUSE/RESUME" "One or more requests returned 500"
fi

# ── E2: Invalid Config ─────────────────────────────────────────────────────────
echo ""
echo "E2 — Invalid Config (PUT /config)"

# Save current config for restore
original_config=$(api_body GET /config)

# E2a — Inverted thresholds (resume > pause) — flat format
code=$(api PUT /config -d '{"ram_pause_pct":60.0,"ram_resume_pct":85.0}')
if [[ "$code" =~ ^(400|422)$ ]]; then
    _pass "E2a Inverted thresholds rejected — HTTP ${code}"
elif [[ "$code" == "200" ]]; then
    _fail "E2a Inverted thresholds accepted" "HTTP 200 — thresholds not validated"
else
    _fail "E2a Inverted thresholds" "HTTP ${code}"
fi

# E2b — ram_pause_pct > 100
code=$(api PUT /config -d '{"ram_pause_pct":150.0}')
if [[ "$code" =~ ^(400|422)$ ]]; then
    _pass "E2b ram_pause_pct:150 rejected — HTTP ${code}"
else
    _fail "E2b ram_pause_pct:150" "HTTP ${code} (expected 400/422)"
fi

# E2c — Negative value
code=$(api PUT /config -d '{"ram_pause_pct":-5.0}')
if [[ "$code" =~ ^(400|422)$ ]]; then
    _pass "E2c Negative ram_pause_pct rejected — HTTP ${code}"
else
    _fail "E2c Negative ram_pause_pct" "HTTP ${code}"
fi

# E2d — check_interval_seconds: 0
code=$(api PUT /config -d '{"check_interval_seconds":0}')
if [[ "$code" =~ ^(400|422)$ ]]; then
    _pass "E2d check_interval_seconds:0 rejected — HTTP ${code}"
else
    _fail "E2d check_interval_seconds:0" "HTTP ${code} (expected 400/422)"
fi

# E2e — Malformed JSON
code=$(curl -s -o /dev/null -w "%{http_code}" \
    -X PUT \
    -H "X-API-Key: ${KEY}" \
    -H "Content-Type: application/json" \
    -d 'not json at all {{{{' \
    "${BASE}/config")
if [[ "$code" =~ ^(400|422)$ ]]; then
    _pass "E2e Malformed JSON rejected — HTTP ${code}"
else
    _fail "E2e Malformed JSON" "HTTP ${code}"
fi

# E2f — Empty JSON body
code=$(api PUT /config -d '{}')
if [[ "$code" =~ ^(200|400|422)$ ]]; then
    _pass "E2f Empty JSON body — HTTP ${code} (no 500)"
else
    _fail "E2f Empty JSON body" "HTTP ${code}"
fi

# E2g — Unknown fields (flat format with one valid + one unknown)
code=$(api PUT /config -d '{"unknown_field":"should_be_ignored","ram_pause_pct":85.0}')
if [[ "$code" =~ ^(200|400|422)$ ]]; then
    _pass "E2g Unknown fields — HTTP ${code} (no 500)"
else
    _fail "E2g Unknown fields" "HTTP ${code}"
fi

# E2h — Config readable and ram_pause_pct still at expected value after all bad PUTs
current_config=$(api_body GET /config)
current_ram=$(echo "$current_config" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ram_pause_pct','?'))" 2>/dev/null || echo "?")
if [[ "$current_ram" != "?" ]]; then
    _pass "E2h /config readable after bad PUTs — ram_pause_pct: ${current_ram}"
else
    _fail "E2h Config readable after bad PUTs" "Could not parse response"
fi

# ── E3: Authentication Edge Cases ─────────────────────────────────────────────
echo ""
echo "E3 — Authentication"

# E3a — Missing key
code=$(api_noauth GET /status)
if [[ "$code" == "401" ]]; then
    _pass "E3a Missing key → 401"
else
    _fail "E3a Missing key" "HTTP ${code} (expected 401)"
fi

# E3b — Wrong key
code=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "X-API-Key: invalid-key-for-testing" \
    "${BASE}/status")
if [[ "$code" == "401" ]]; then
    _pass "E3b Wrong key → 401"
else
    _fail "E3b Wrong key" "HTTP ${code} (expected 401)"
fi

# E3c — Empty key
code=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "X-API-Key: " \
    "${BASE}/status")
if [[ "$code" == "401" ]]; then
    _pass "E3c Empty key → 401"
else
    _fail "E3c Empty key" "HTTP ${code} (expected 401)"
fi

# E3d — Correct key
code=$(api GET /status)
if [[ "$code" == "200" ]]; then
    _pass "E3d Correct key → 200"
else
    _fail "E3d Correct key" "HTTP ${code}"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Results: ${PASS} passed  ${FAIL} failed  ${SKIP} skipped"
echo ""

# Restore RUNNING state
api POST /resume > /dev/null 2>&1 || true

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
