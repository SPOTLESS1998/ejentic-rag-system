# Ejentic Enterprise RAG System

The reusable, enterprise-grade Retrieval-Augmented Generation (RAG) brain
behind every Ejentic process that needs grounded answers: customer support,
lead-gen, telephony agents, HR, and the Knowledge Core we sell to clients.

It implements the **Advanced-RAG production architecture** (Microsoft-style
retrieval-augmented pattern) as a linear, observable pipeline — not a black-box
agent — with security and token economics baked in at every stage:

```
INGEST                                RETRIEVE                        SYNTHESIZE
(clearance-tagged)                    (query rewrite)                 (grounded prompt
  PDF / txt / JSON   semantic chunk   -> hybrid search                + mandatory citations)
  -> embed (NVIDIA)  -> rerank (cross-encoder / BM25)                 -> confidence gate
  -> Pinecone upsert -> length cap                                    -> escalation line
```

| Stage | What runs | Why it matters |
| ----- | --------- | -------------- |
| 1. Ingestion | `ingest_knowledge.py` (validates + tags), `pdf_to_knowledge.py` | The `clearance` tag is attached here — the security boundary of the whole system |
| 2. Retrieval | Query rewrite → hybrid (dense+sparse) or dense search → rerank (NVIDIA cross-encoder, else local CE, else dependency-free BM25) | Recall + precision; the reranker floor is always real |
| 3. Guardrails | Confidence gate, mandatory `[Source N]` citations, grounding-only persona | Hallucination refusal at ~0 answer tokens |
| Observability | Token metering (provider or labelled estimate), SQLite audit DB, `GET /metrics`, LangSmith wiring | The system is *measurable*, so spend is provable |

## Why this is the "shared architecture"

Every Ejentic system we build needs retrieval augmentation. Instead of each
service reinventing a RAG stack, **all of them call one RAG core** through two
stable contracts:

- `POST /api/rag` — strict JSON in/out for pipelines & n8n (Telegram/CRM/Lead-gen)
- `POST /chat` — Server-Sent Events streaming for interactive UIs

The Python core is the single engine; per-tenant differences (index, namespace,
models, persona, clearances) are **data in `backend/clients/*.json`**, never
copies of the code. Services consume it through the first-class TypeScript SDK
in `sdk/`.

## Repository layout

```
ejentic-rag-system/
├─ backend/            # FastAPI RAG brain (Python, LlamaIndex + NVIDIA NIM + Pinecone)
│  ├─ main.py          # Server: retrieval pipeline, guardrails, frozen API contracts
│  ├─ ingest_knowledge.py  # Canonical clearance-aware ingestion (the ONLY writer to Pinecone)
│  ├─ client_registry.py   # Multi-tenant config (data-driven, validated JSON)
│  ├─ clients/ejentic.json # The default client config — copy this to onboard a tenant
│  ├─ eval_rag.py      # Data-driven eval harness (correctness + leak + gate probing)
│  ├─ verify_clearance.py # Three-role security matrix demo
│  ├─ database.py      # SQLite audit log + token accounting
│  ├─ token_meter.py   # token metering (provider or labelled estimate)
│  └─ RUNBOOK.md       # Repeatable ingest/verify/eval process
├─ frontend/           # Next.js chat UI (SSE client)
├─ sdk/                # @ejentic/rag-sdk — TS client for every Ejentic consumer
└─ n8n_workflow_template.json  # Telegram -> RAG -> reply workflow
```

## Quickstart

```bash
# 1. Backend
cd backend
cp .env.example .env            # fill PINECONE_API_KEY + NVIDIA_API_KEY
pip install -r requirements.txt
python ingest_knowledge.py      # build the index from backend/ejentic_knowledge.json
python verify_clearance.py      # prove the 3-role security matrix
uvicorn main:app --port 8002    # serve on :8002

# 2. UI (optional, separate terminal)
cd ../frontend && npm install && npm run dev   # :3002

# 3. TypeScript SDK (for services, agents, pipelines)
cd ../sdk && npm install && npm test
```

Health check: `GET http://localhost:8002/` → `{"status":"ok",
"...":true, "token_metering": true, ...}`.

## Multi-tenancy: one engine, every client

A RAG instance boots for **one active tenant** (`RAG_CLIENT` env → a file in
`backend/clients/`). Each tenant gets its own Pinecone index/namespace, models,
retrieval tunables, persona and clearance map. A different tenant = a separate
deployment (same code). The API rejects cross-tenant requests with HTTP 409.

**To onboard a new tenant:** copy `backend/clients/ejentic.json` to
`clients/<id>.json`, edit the fields, ingest their documents
(`python ingest_knowledge.py --file <tenant>_knowledge.json --append`), and
start an instance with `RAG_CLIENT=<id>`. No Python changes.

## Frozen contracts

`POST /chat` streams `data: {"chunk": "..."}` + trailing `data: [DONE]`;
`POST /api/rag` returns `{"status":"success","response":"..."}`. Both also
accept an optional `client` field. Token accounting rides the `X-*` headers so
bodies stay byte-for-byte compatible. **These shapes are load-bearing** — the
web UI, n8n, and the SDK depend on them.

## Token economics

The confidence gate refuses ungroundable questions at ~0 answer tokens; query
rewrite only fires on vague queries; reranking trims to a small top-N with
per-source length caps. Every query is metered to the audit DB, and the
`X-*` headers + `GET /metrics` make spend and estimated savings observable.

## License / provenance

Adapted by Ejentic from a reference retrieval-augmented-generation design first
introduced at Microsoft (the 3-step production RAG pattern) and tailored to our
stack: NVIDIA NIM, LlamaIndex, Pinecone, FastAPI, Next.js, and the TypeScript
SDK for our services. Docs, runbooks and eval harness are Ejentic's own.

## Verification — how to test this build

Everything below is grouped by how much it needs. Run the cheap checks first;
they prove the multi-tenant wiring and the SDK work with **zero API keys**.

### 1. Configuration & SDK sanity (no keys needed)

```bash
# 1a. Python: registry loads, merges, validates, and fails closed
cd backend
python3 -c "
import client_registry as r
c = r.get_client()
assert c['index_name'] == 'ejentic-global'
assert c['clearance_levels']['guest'] == ['public']
assert c['persona_name']
print('client:', r.active_client_id(), '| clients:', [x['id'] for x in r.list_clients()])
"
RAG_CLIENT=bogus python3 -c "import client_registry as r; r.active_client_id()"
# expect: ERROR: RAG_CLIENT='bogus' is not registered. ...  (fail closed, exit 1)

# 1b. TypeScript SDK: typecheck, unit tests (mock server), build
cd ../sdk
npm install
npm run typecheck   # 0 errors
npm test            # 9 passing
npm run build       # emits dist/
```

### 2. Live pipeline (needs `backend/.env` with PINECONE_API_KEY + NVIDIA_API_KEY)

```bash
cd backend
cp .env.example .env       # fill real keys
pip install -r requirements.txt        # (or: uv pip install --python venv/bin/python -r requirements.txt)

# Build the index from the demo data
python ingest_knowledge.py

# Two-line proof of the security boundary — the confidence-gate + clearance matrix
python verify_clearance.py
#   question tier   |   guest    | employee  | executive
#   public /internal/exec  ->  ANSWER/escalate cells per the grid in the script

# Data-driven grading: correctness, leak probes, gate robustness (exit code = CI-ready)
python eval_rag.py                       # full run (judge LLM)
python eval_rag.py --no-judge            # fast behaviour+leak check, ~0 judge tokens

# Serve it
uvicorn main:app --port 8002
```

### 3. Verify the live API surface (server running)

```bash
# health — proves the active client + config wiring
curl -s http://localhost:8002/ | python3 -m json.tool
#  { "client": "ejentic", "client_name": "...", "index": "ejentic-global", "token_metering": true, ... }

# multi-tenant registry (new endpoint)
curl -s http://localhost:8002/clients | python3 -m json.tool
#  { "active_client": "ejentic", "client_count": 1, "clients": [ { "id": "ejentic", ... } ] }

# grounded answer + X-* token headers
curl -s -D - -X POST http://localhost:8002/api/rag \
  -H 'Content-Type: application/json' \
  -d '{"query":"What services does Ejentic offer?","clearance_level":"guest","platform":"curl"}'
#  X-Prompt-Tokens / X-Completion-Tokens / X-Total-Tokens / X-Gated / X-Saved-Tokens present

# cross-tenant guard — MUST reject with HTTP 409 (proves isolation is enforced)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8002/api/rag \
  -H 'Content-Type: application/json' \
  -d '{"query":"hi","clearance_level":"guest","client":"not-this-instance"}'

# token dashboard
curl -s http://localhost:8002/metrics | python3 -m json.tool
```

### 4. End-to-end through the SDK (server running)

```bash
cd sdk
node --experimental-strip-types examples/usage.ts   # or: npm link + run in a TS service
# prints: active client, a grounded answer, token usage, lifetime metrics
```

### 5. UI (optional)

```bash
cd frontend && npm install && npm run dev    # open http://localhost:3002
# the top-right clearance selector changes what the backend is allowed to answer
```

Any failing step above should correspond to a real bug: clearances that leak
(fail-closed check), an unknown `RAG_CLIENT` accepted instead of refused
(step 1a), a drift from the frozen API contracts (SDK tests), or a silent
cross-tenant request (step 3, 409).

---

See backend/RUNBOOK.md for the ingestion & verification onboarding process,
and sdk/README.md for consuming the RAG from TypeScript services.