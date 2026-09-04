#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/backend"
rm -f /tmp/sse_raw.txt
echo "========================================"
echo " FINAL E2E TEST — all endpoints + SSE"
echo "========================================"

venv/bin/uvicorn main:app --host 127.0.0.1 --port 8002 > /tmp/rag_server.log 2>&1 &
SRV=$!
for i in $(seq 1 40); do curl -s http://127.0.0.1:8002/ >/dev/null 2>&1 && break; sleep 1; done
echo "[ok] server up (${i}s)"

echo; echo "--- 1) GET / (health) ---"
curl -s http://127.0.0.1:8002/ | python3 -m json.tool | grep -E 'client|index|token_metering|reranker'

echo; echo "--- 2) GET /clients (registry) ---"
curl -s http://127.0.0.1:8002/clients | python3 -m json.tool | grep -E 'active_client|llm_model|embed_model|"id"'

echo; echo "--- 3) POST /api/rag guest/public + token headers ---"
curl -s -D /tmp/h.txt -o /tmp/b.json -X POST http://127.0.0.1:8002/api/rag \
  -H 'Content-Type: application/json' \
  -d '{"query":"What three offerings does Ejentic have?","clearance_level":"guest","platform":"e2e"}' >/dev/null
grep -iE '^X-|HTTP' /tmp/h.txt
python3 -m json.tool /tmp/b.json | grep -E 'status|response' | head -3

echo; echo "--- 4) POST /api/rag employee/internal ---"
curl -s -X POST http://127.0.0.1:8002/api/rag -H 'Content-Type: application/json' \
  -d '{"query":"What is Project Delta?","clearance_level":"employee"}' | python3 -c "import sys,json;print('  ->',json.load(sys.stdin)['response'][:120])"

echo; echo "--- 5) cross-tenant guard (expect 409) ---"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8002/api/rag \
  -H 'Content-Type: application/json' \
  -d '{"query":"hi","clearance_level":"guest","client":"other-tenant"}')
echo "  HTTP $code $([ "$code" = "409" ] && echo '✓ guard OK' || echo '✗ FAIL')"

echo; echo "--- 6) POST /chat SSE stream ---"
curl -s -N -X POST http://127.0.0.1:8002/chat -H 'Content-Type: application/json' \
  -d '{"query":"List the three core offers","clearance_level":"guest"}' --max-time 30 \
  | sed -n 's/^data: //p' > /tmp/sse_raw.txt
python3 -c "
import json
txt=''
for line in open('/tmp/sse_raw.txt'):
    line=line.strip()
    if not line: continue
    try:
        obj=json.loads(line)
    except: continue
    if '[DONE]' in str(obj) or obj.get('type')=='done': break
    if 'chunk' in obj: txt+=obj['chunk']
    if 'error' in obj: print('  STREAM ERROR:', obj['error'][:100]); break
print('  RECONSTRUCTED:', txt[:200])
print('  ✓ SSE stream OK' if txt.strip() and 'error' not in txt else '  ✗ stream empty/error')
"

echo; echo "--- 7) GET /metrics (token dashboard) ---"
curl -s http://127.0.0.1:8002/metrics | python3 -m json.tool | grep -E 'queries|answered|gated|total_tokens|client|llm_model' | head -10

kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
echo; echo "========================================"
echo " FINAL E2E COMPLETE — server stopped"
echo "========================================"