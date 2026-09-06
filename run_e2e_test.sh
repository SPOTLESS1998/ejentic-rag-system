#!/usr/bin/env bash
#
# End-to-end test against a REAL server (real Pinecone, real NVIDIA).
#
# THIS SCRIPT CAN FAIL. The previous version printed results and always exited 0 —
# so it was a demo wearing a test's name, and a broken clearance boundary would
# have scrolled past as green text. Every check below feeds a counter and the
# script exits 1 if anything failed.
#
# It costs real tokens (a handful of small queries) and needs both API keys.
#
# Usage:
#   ./run_e2e_test.sh                 # start a server on :8002 and test it
#   E2E_URL=http://host:8002 ./run_e2e_test.sh    # test an already-running server
#
# The clearance-isolation checks need role keys in backend/.env (RAG_KEY_GUEST,
# RAG_KEY_EMPLOYEE, RAG_KEY_EXECUTIVE). Without them the script says so and skips
# those checks rather than pretending the boundary was verified.

set -uo pipefail
cd "$(dirname "$0")/backend"

PASS=0
FAIL=0
SKIP=0
FAILED_NAMES=()

ok()   { PASS=$((PASS+1)); printf '  \033[32m✅\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILED_NAMES+=("$1"); printf '  \033[31m❌\033[0m %s%s\n' "$1" "${2:+  ($2)}"; }
skip() { SKIP=$((SKIP+1)); printf '  \033[33m⏭\033[0m  %s%s\n' "$1" "${2:+  ($2)}"; }

# check <name> <condition-exit-code> [detail]
check() { if [ "$2" -eq 0 ]; then ok "$1"; else bad "$1" "${3:-}"; fi; }

section() { printf '\n\033[1m--- %s ---\033[0m\n' "$1"; }

# Load the role keys from .env WITHOUT printing them. Only presence is reported.
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
GUEST_KEY="${RAG_KEY_GUEST:-}"
EMP_KEY="${RAG_KEY_EMPLOYEE:-}"
EXEC_KEY="${RAG_KEY_EXECUTIVE:-}"

echo "========================================"
echo " E2E TEST — endpoints, auth, SSE, tokens"
echo "========================================"

# --- Server ----------------------------------------------------------------
URL="${E2E_URL:-http://127.0.0.1:8002}"
SRV=""
if [ -z "${E2E_URL:-}" ]; then
  if curl -sf "$URL/" >/dev/null 2>&1; then
    echo "[info] a server is already listening on :8002 — testing that one."
    echo "       (it may predate your latest changes; restart it if results look stale)"
  else
    venv/bin/uvicorn main:app --host 127.0.0.1 --port 8002 > /tmp/rag_server.log 2>&1 &
    SRV=$!
    for i in $(seq 1 60); do
      curl -sf "$URL/" >/dev/null 2>&1 && break
      sleep 1
    done
    if ! curl -sf "$URL/" >/dev/null 2>&1; then
      echo "FATAL: server did not come up in 60s. Last log lines:"
      tail -20 /tmp/rag_server.log
      exit 1
    fi
    echo "[ok] server started (${i}s)"
  fi
fi

cleanup() { [ -n "$SRV" ] && kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null; return 0; }
trap cleanup EXIT

# status <method> <path> [json-body] [key]
status() {
  local method="$1" path="$2" body="${3:-}" key="${4:-}"
  local args=(-s -o /dev/null -w '%{http_code}' -X "$method" "$URL$path")
  [ -n "$body" ] && args+=(-H 'Content-Type: application/json' -d "$body")
  [ -n "$key" ] && args+=(-H "X-API-Key: $key")
  curl "${args[@]}"
}

# ask <query> <clearance> <key> -> prints the answer text
ask() {
  local q="$1" lvl="$2" key="${3:-}"
  local args=(-s -X POST "$URL/api/rag" -H 'Content-Type: application/json'
              -d "{\"query\":\"$q\",\"clearance_level\":\"$lvl\",\"platform\":\"e2e\"}")
  [ -n "$key" ] && args+=(-H "X-API-Key: $key")
  curl "${args[@]}" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("response",""))
except Exception: print("")'
}

# ---------------------------------------------------------------------------
section "1) GET / — liveness is public"
# ---------------------------------------------------------------------------
HEALTH=$(curl -s "$URL/")
check "GET / responds" "$([ -n "$HEALTH" ] && echo 0 || echo 1)"
echo "$HEALTH" | python3 -c 'import sys,json
h=json.load(sys.stdin)
print(f"     client={h.get(\"client\")} index={h.get(\"index\")} reranker={h.get(\"reranker\")}")
a=h.get("auth") or {}
print(f"     auth.required={a.get(\"required\")} roles_with_keys={a.get(\"roles_with_keys_set\")}")' || true

AUTH_ON=$(echo "$HEALTH" | python3 -c 'import sys,json
print("1" if (json.load(sys.stdin).get("auth") or {}).get("required") else "0")' 2>/dev/null || echo 0)

check "GET / reports the auth posture" \
  "$(echo "$HEALTH" | grep -q '"auth"' && echo 0 || echo 1)"
check "GET / exposes no key value" \
  "$(echo "$HEALTH" | grep -qE 'RAG_KEY|[a-f0-9]{64}' && echo 1 || echo 0)"

if [ "$AUTH_ON" != "1" ]; then
  printf '\n\033[33m⚠️  auth.required is FALSE on this instance.\033[0m\n'
  echo "   The 401/403 checks below cannot prove anything while auth is off."
  echo "   Set \"auth\": {\"required\": true} in backend/clients/ejentic.json and put"
  echo "   the RAG_KEY_* values in backend/.env, then re-run."
fi

# ---------------------------------------------------------------------------
section "2) the boundary — no key must be refused"
# ---------------------------------------------------------------------------
if [ "$AUTH_ON" = "1" ]; then
  for spec in "POST /api/rag {\"query\":\"hi\"}" \
              "POST /chat {\"query\":\"hi\"}" \
              "POST /ingest {}" \
              "GET /metrics" \
              "GET /clients" \
              "GET /whoami"; do
    # shellcheck disable=SC2086
    set -- $spec
    m="$1"; p="$2"; b="${3:-}"
    code=$(status "$m" "$p" "$b")
    check "$m $p rejects a keyless caller" \
      "$([ "$code" = "401" ] && echo 0 || echo 1)" "got $code"
  done

  code=$(status POST /api/rag '{"query":"hi"}' "definitely-not-a-real-key")
  check "a bogus key is refused" "$([ "$code" = "401" ] && echo 0 || echo 1)" "got $code"

  # THE ORIGINAL EXPLOIT: this exact request used to return board-only material.
  code=$(status POST /api/rag '{"query":"What was Q2 revenue?","clearance_level":"executive"}')
  check "the original exploit (body-only executive claim, no key) is refused" \
    "$([ "$code" = "401" ] && echo 0 || echo 1)" "got $code"
else
  skip "keyless-rejection checks" "auth.required is false"
fi

# ---------------------------------------------------------------------------
section "3) clearance isolation — the product claim"
# ---------------------------------------------------------------------------
if [ -z "$GUEST_KEY" ] || [ -z "$EXEC_KEY" ]; then
  skip "clearance isolation" "RAG_KEY_GUEST / RAG_KEY_EXECUTIVE not set in backend/.env"
else
  echo "     (keys present: guest=${#GUEST_KEY} chars, executive=${#EXEC_KEY} chars)"

  # Narrowing: down is fine, up is 403.
  code=$(status POST /api/rag '{"query":"hi","clearance_level":"executive"}' "$GUEST_KEY")
  check "a guest key asking for executive is 403" \
    "$([ "$code" = "403" ] && echo 0 || echo 1)" "got $code"

  code=$(status POST /api/rag '{"query":"hi","clearance_level":"guest"}' "$EXEC_KEY")
  check "an executive key may narrow to guest" \
    "$([ "$code" = "200" ] && echo 0 || echo 1)" "got $code"

  # Both halves of isolation. Only checking the refusal would also "pass" if
  # retrieval were broken for everyone, so the executive must genuinely get it.
  SECRET_Q="What was Q2 revenue?"
  GUEST_ANS=$(ask "$SECRET_Q" "" "$GUEST_KEY")
  EXEC_ANS=$(ask "$SECRET_Q" "" "$EXEC_KEY")

  if echo "$GUEST_ANS" | grep -qiE '2\.4 ?M|\$2,?400,?000'; then
    bad "a guest key CANNOT reach executive revenue" "LEAKED: ${GUEST_ANS:0:90}"
  else
    ok "a guest key CANNOT reach executive revenue"
  fi
  if echo "$EXEC_ANS" | grep -qiE '2\.4 ?M|\$2,?400,?000'; then
    ok "an executive key CAN reach it (retrieval still works)"
  else
    bad "an executive key CAN reach it (retrieval still works)" "got: ${EXEC_ANS:0:90}"
  fi

  if [ -n "$EMP_KEY" ]; then
    EMP_ANS=$(ask "$SECRET_Q" "" "$EMP_KEY")
    if echo "$EMP_ANS" | grep -qiE '2\.4 ?M|\$2,?400,?000'; then
      bad "an employee key cannot reach executive revenue" "LEAKED: ${EMP_ANS:0:90}"
    else
      ok "an employee key cannot reach executive revenue"
    fi
  else
    skip "employee-tier isolation" "RAG_KEY_EMPLOYEE not set"
  fi

  # /whoami must describe the caller, and only the caller.
  WHO=$(curl -s -H "X-API-Key: $GUEST_KEY" "$URL/whoami")
  check "/whoami reports the key's role" \
    "$(echo "$WHO" | grep -q '"role": *"guest"' && echo 0 || echo 1)" "$WHO"
  check "/whoami leaks no key value" \
    "$(echo "$WHO" | grep -qF "$GUEST_KEY" && echo 1 || echo 0)"

  # /ingest can delete every vector in the namespace — admin only.
  code=$(status POST /ingest '{}' "$GUEST_KEY")
  check "/ingest refuses a non-admin key" \
    "$([ "$code" = "403" ] && echo 0 || echo 1)" "got $code"
fi

# ---------------------------------------------------------------------------
section "4) a grounded answer with citations + token headers"
# ---------------------------------------------------------------------------
KEY_FOR_QUERY="${GUEST_KEY:-}"
CURL_KEY=()
[ -n "$KEY_FOR_QUERY" ] && CURL_KEY=(-H "X-API-Key: $KEY_FOR_QUERY")

curl -s -D /tmp/h.txt -o /tmp/b.json -X POST "$URL/api/rag" \
  -H 'Content-Type: application/json' "${CURL_KEY[@]}" \
  -d '{"query":"What services does Ejentic AI offer?","platform":"e2e"}' >/dev/null

HTTP_CODE=$(head -1 /tmp/h.txt | tr -d '\r' | awk '{print $2}')
check "POST /api/rag answers" "$([ "$HTTP_CODE" = "200" ] && echo 0 || echo 1)" "HTTP $HTTP_CODE"

check "the frozen JSON shape is intact" \
  "$(python3 -c 'import json;d=json.load(open("/tmp/b.json"));exit(0 if d.get("status")=="success" and isinstance(d.get("response"),str) else 1)' && echo 0 || echo 1)"

ANSWER=$(python3 -c 'import json;print(json.load(open("/tmp/b.json")).get("response",""))' 2>/dev/null || echo "")
check "the answer is non-empty" "$([ -n "${ANSWER// /}" ] && echo 0 || echo 1)"
echo "     -> ${ANSWER:0:140}"

# Either a citation (it answered from sources) or the escalation line (the gate
# refused). Both are correct; an answer with NEITHER is ungrounded prose.
if echo "$ANSWER" | grep -qE '\[Source [0-9]+\]'; then
  ok "the answer carries [Source N] citations"
elif echo "$ANSWER" | grep -qiE 'escalate|more context|don.t have that'; then
  ok "the confidence gate refused instead of guessing (also correct)"
else
  bad "the answer is grounded (citation or refusal)" "${ANSWER:0:90}"
fi

for h in X-Total-Tokens X-Token-Source X-Gated X-Clearance; do
  check "response header $h is present" \
    "$(grep -qi "^$h:" /tmp/h.txt && echo 0 || echo 1)"
done
grep -iE '^X-(Prompt|Completion|Total|Saved)-Tokens|^X-Token-Source|^X-Gated|^X-Clearance' /tmp/h.txt \
  | sed 's/^/     /' | tr -d '\r'

# ---------------------------------------------------------------------------
section "5) cross-tenant guard (expect 409)"
# ---------------------------------------------------------------------------
code=$(status POST /api/rag '{"query":"hi","client":"other-tenant"}' "$KEY_FOR_QUERY")
check "a request for another client is refused with 409" \
  "$([ "$code" = "409" ] && echo 0 || echo 1)" "got $code"

# ---------------------------------------------------------------------------
section "6) POST /chat — the SSE contract"
# ---------------------------------------------------------------------------
rm -f /tmp/sse_raw.txt
curl -s -N -X POST "$URL/chat" -H 'Content-Type: application/json' "${CURL_KEY[@]}" \
  -d '{"query":"List Ejentic AI core offerings","platform":"e2e"}' \
  --max-time 90 > /tmp/sse_raw.txt

check "the stream produced output" \
  "$([ -s /tmp/sse_raw.txt ] && echo 0 || echo 1)"
check "events use the frozen 'data: ' prefix" \
  "$(grep -q '^data: ' /tmp/sse_raw.txt && echo 0 || echo 1)"
check "the stream terminates with [DONE]" \
  "$(grep -q '^data: \[DONE\]' /tmp/sse_raw.txt && echo 0 || echo 1)"

# Reassemble the stream and look for the specific ways it has broken before:
# an error event, no text at all, an exactly-doubled answer (the replay bug), or a
# leaked 'assistant:' marker. Prints the answer, then a |-joined problem list.
SSE_OUT=$(python3 - <<'PY'
import json
text, err = "", None
for line in open("/tmp/sse_raw.txt"):
    line = line.strip()
    if not line.startswith("data: "):
        continue
    payload = line[6:]
    if payload == "[DONE]":
        break
    try:
        obj = json.loads(payload)
    except Exception:
        continue
    if "error" in obj:
        err = obj["error"]
        break
    if "chunk" in obj:
        text += obj["chunk"]

t = text.strip()
print(f"     RECONSTRUCTED: {t[:160]}")

problems = []
if err:
    problems.append("error: " + err[:60])
if not t:
    problems.append("no text delivered")
if len(t) >= 40 and len(t) % 2 == 0 and t[:len(t) // 2] == t[len(t) // 2:]:
    problems.append("text is exactly doubled (the replay bug is back)")
if t.lower().startswith("assistant:"):
    problems.append("a leading 'assistant:' marker leaked")
print("PROBLEMS:" + "|".join(problems))
PY
)
echo "$SSE_OUT" | grep -v '^PROBLEMS:'
SSE_PROBLEMS=${SSE_OUT##*PROBLEMS:}
check "the reconstructed answer is clean (no error, no duplication, no marker leak)" \
  "$([ -z "$SSE_PROBLEMS" ] && echo 0 || echo 1)" "$SSE_PROBLEMS"

# ---------------------------------------------------------------------------
section "7) GET /metrics — token accounting, scoped to this tenant"
# ---------------------------------------------------------------------------
METRICS=$(curl -s "${CURL_KEY[@]}" "$URL/metrics")
check "GET /metrics responds to an authenticated caller" \
  "$(echo "$METRICS" | grep -q '"totals"' && echo 0 || echo 1)"
check "metrics name the tenant they belong to" \
  "$(echo "$METRICS" | grep -q '"client"' && echo 0 || echo 1)"
check "the queries we just ran were recorded" \
  "$(echo "$METRICS" | python3 -c 'import sys,json
try: exit(0 if json.load(sys.stdin)["totals"]["queries"] > 0 else 1)
except Exception: exit(1)' && echo 0 || echo 1)"
echo "$METRICS" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); t=d["totals"]
    print(f"     client={d.get(\"client\")} queries={t[\"queries\"]} answered={t[\"answered\"]} "
          f"gated={t[\"gated\"]} total_tokens={t[\"total_tokens\"]} saved={t[\"estimated_saved_tokens\"]}")
except Exception as e: print("     (could not parse metrics)", e)' || true

# ---------------------------------------------------------------------------
section "8) upload rejects a traversal filename"
# ---------------------------------------------------------------------------
if [ "$AUTH_ON" = "1" ] && [ -z "$KEY_FOR_QUERY" ]; then
  skip "upload traversal check" "auth is on and no key available"
else
  rm -f /tmp/pwned_e2e.pdf
  printf 'harmless test content\n' > /tmp/e2e_upload.pdf
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/upload" "${CURL_KEY[@]}" \
    -F 'file=@/tmp/e2e_upload.pdf;filename=../../../../tmp/pwned_e2e.pdf')
  check "a traversal filename did not write outside the upload dir" \
    "$([ ! -f /tmp/pwned_e2e.pdf ] && echo 0 || echo 1)" "HTTP $code"
  rm -f /tmp/e2e_upload.pdf /tmp/pwned_e2e.pdf
fi

# ---------------------------------------------------------------------------
printf '\n========================================\n'
printf ' E2E RESULT: %d passed, %d failed' "$PASS" "$FAIL"
[ "$SKIP" -gt 0 ] && printf ', %d skipped' "$SKIP"
printf '\n========================================\n'
if [ "$FAIL" -gt 0 ]; then
  printf '\nFailed checks:\n'
  for n in "${FAILED_NAMES[@]}"; do printf '  - %s\n' "$n"; done
  echo
  echo "Server log: /tmp/rag_server.log"
  exit 1
fi
if [ "$SKIP" -gt 0 ]; then
  echo
  echo "NOTE: $SKIP check(s) were skipped — the security boundary is NOT fully"
  echo "      verified until auth.required is true and the RAG_KEY_* values are set."
fi
exit 0
