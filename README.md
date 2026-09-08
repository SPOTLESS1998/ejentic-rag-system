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
| 0. Authentication | `auth.py` — an API key maps to a role; a request may narrow it, never widen | The clearance filter is only as meaningful as the authentication in front of it |
| 1. Ingestion | `ingest_knowledge.py` (validates + tags), `pdf_to_knowledge.py` | The `clearance` tag is attached here — the security boundary of the whole system |
| 2. Retrieval | Query rewrite → hybrid (dense+sparse) or dense search → rerank (NVIDIA cross-encoder, else local CE, else dependency-free BM25) | Recall + precision; the reranker floor is always real |
| 3. Guardrails | Confidence gate, mandatory `[Source N]` citations, grounding-only persona | Hallucination refusal at ~0 answer tokens |
| Observability | Token metering (provider or labelled estimate), SQLite audit DB, `GET /metrics`, LangSmith wiring | The system is *measurable*, so spend is provable |

## The demo corpus is fabricated

`backend/ejentic_knowledge.json` is a **test fixture, not company records.** Its `internal` and
`executive` entries — an employee handbook, a "Project Delta", Q2 revenue figures, an authorised
acquisition bid — are invented, and exist only so the clearance grid has something to gate.
`backend/eval_cases.json` and `backend/verify_clearance.py` assert on those exact strings to prove
that a `guest` answer never leaks an `executive` fact, which is why they read as oddly specific.

To be explicit, because the numbers look real: **they are not Ejentic AI's financials.** Nothing in
that file describes any real company's revenue, projects, or strategy. Replace it wholesale with
your own clearance-tagged content before running this against anything that matters.

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
│  ├─ auth.py          # THE ONLY place a caller's role is decided (API key -> role)
│  ├─ ingest_knowledge.py  # Canonical clearance-aware ingestion (the ONLY writer to Pinecone)
│  ├─ client_registry.py   # Multi-tenant config (data-driven, validated JSON)
│  ├─ clients/ejentic.json # The default client config — copy this to onboard a tenant
│  ├─ tests/           # Offline suite: no network, no keys (tests/run_all.py)
│  ├─ eval_rag.py      # Data-driven eval harness (correctness + leak + gate probing)
│  ├─ verify_clearance.py # Three-role security matrix demo
│  ├─ database.py      # Per-tenant SQLite audit log + token accounting
│  ├─ token_meter.py   # token metering (provider or labelled estimate)
│  └─ RUNBOOK.md       # Repeatable ingest/verify/eval process
├─ frontend/           # Next.js chat UI + server-side proxy that holds the API key
├─ sdk/                # @ejentic/rag-sdk — TS client for every Ejentic consumer
├─ run_e2e_test.sh     # Live end-to-end test (exits non-zero on failure)
└─ n8n_workflow_template.json  # Telegram -> RAG -> reply workflow
```

## Authentication — the key decides the role

**The clearance filter is only as meaningful as the authentication in front of
it.** This system filters documents by a `clearance` tag, and until `auth.py`
existed the clearance was simply a field in the request body — so a plain `curl`
with `"clearance_level": "executive"` and no credential read board-only material.
Filtering correctly on a role nobody proved is an honour system, not a boundary.

Now the caller presents an API key and **the server derives the role from it**:

```json
"auth": {
  "required": true,
  "keys": {
    "guest":     "RAG_KEY_GUEST",
    "employee":  "RAG_KEY_EMPLOYEE",
    "executive": "RAG_KEY_EXECUTIVE"
  },
  "admin_role": "executive"
}
```

The config names **environment variables**, never key values (see
`MULTITENANCY.md`). Generate one key per role and put the values in
`backend/.env`:

```bash
openssl rand -hex 32     # once per role -> RAG_KEY_GUEST / _EMPLOYEE / _EXECUTIVE
```

Send it as `X-API-Key: <key>` (or `Authorization: Bearer <key>`).

**The rules, in short:**

| Rule | Behaviour |
| ---- | --------- |
| The key decides the role | `clearance_level` in the body can only **narrow** it |
| Narrowing is allowed | An executive key may ask for the `guest` view (useful for testing) |
| Widening is **403** | A guest key asking for `executive` is refused loudly — never silently downgraded |
| Missing/unknown key | **401**, with no hint about which way you were wrong |
| `/ingest` | **Admin role only**, and appends by default — a rebuild needs `{"rebuild": true}` |
| `/metrics`, `/clients`, `/whoami` | Authenticated: they expose query text and tenant topology |
| `GET /` | Public liveness — and it reports whether auth is on, so an unprotected instance is visible rather than assumed safe |
| `required: true` with no keys set | **Refuses to boot.** A misconfigured instance must not degrade to open access |

`required: false` is a local-development default. Any deployment that serves
real people sets it to `true` — and `GET /` will tell you which mode you are in.

> **Never put a key in the browser.** A key in client-side JavaScript is a public
> key: anyone can read it in devtools and hold that clearance. The Next.js UI
> talks to its own `/api/rag/*` route handlers, which run on the server, hold
> `RAG_API_KEY`, and add it on the way out. That is why the UI *displays* your
> clearance instead of offering a dropdown to choose it.

## Quickstart

```bash
# 1. Backend
cd backend
cp .env.example .env            # fill PINECONE_API_KEY + NVIDIA_API_KEY
                                # (for a protected instance also set RAG_KEY_GUEST/_EMPLOYEE/_EXECUTIVE)
pip install -r requirements.txt
python tests/run_all.py         # offline suite — no keys, no network
python ingest_knowledge.py      # build the index from backend/ejentic_knowledge.json
python verify_clearance.py      # prove the 3-role security matrix
uvicorn main:app --port 8002    # serve on :8002

# 2. UI (optional, separate terminal)
cd ../frontend
cp .env.example .env.local      # set RAG_API_KEY — SERVER-SIDE only, never NEXT_PUBLIC_
npm install && npm run dev      # :3002

# 3. TypeScript SDK (for services, agents, pipelines)
cd ../sdk && npm install && npm test
```

Health check: `GET http://localhost:8002/` → `{"status":"ok", "client":"ejentic",
"auth": {"required": false, ...}, "token_metering": true, ...}` — the `auth` block
tells you at a glance whether the instance is protected.

## Deployment

`docker-compose.yml` is the **development** setup: it bind-mounts the source, publishes
the backend on `8002`, runs the UI via `next dev`, and includes an unauthenticated n8n
editor. All four are right for a laptop and wrong for a public hostname.

For a deployment use the separate files, which change exactly those things:

| | |
|---|---|
| **[`deploy/GO_LIVE.md`](deploy/GO_LIVE.md)** | **start here** — the runbook: decisions, key generation, the verification matrix, rotation, rollback |
| [`DEPLOYMENT-PLAN.md`](DEPLOYMENT-PLAN.md) | *why* each of those choices exists, and the five things that were wrong for a deployment |
| `docker-compose.prod.yml` | backend on the internal network only, UI on `127.0.0.1` behind Caddy, no source mounts |
| `deploy/Caddyfile.rag` | TLS + password gate; the only path in from the internet |
| `deploy/ejentic-rag.service` | systemd, rebuilds on restart, survives reboot |

```bash
docker compose -f docker-compose.prod.yml up -d --build   # after reading GO_LIVE.md
```

Two things worth knowing before you start:

- **Secrets never enter an image.** `backend/.dockerignore` and `frontend/.dockerignore` are
  load-bearing, not housekeeping: both Dockerfiles end in `COPY . .`, so without them a build
  bakes every `RAG_KEY_*` into an image layer, where `docker history` reads it straight back
  out. Runtime env only, from `/etc/ejentic-rag/server.env` — outside the git checkout.
- **One deployed UI answers at one clearance,** for everyone who opens it. There is no
  "log in and see more" without per-user login. Decide that deliberately — it is Decision 2
  in the runbook, and it is the one that actually matters.

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
# 1a. Python offline suite — auth, clearance, upload safety, streaming, tenancy
cd backend
python tests/run_all.py
# expect: RESULT: N passed, 0 failed  (exit 0) — no network, no API keys touched

# 1b. Python: registry loads, merges, validates, and fails closed
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

# 1c. TypeScript SDK: typecheck, unit tests (mock server), build
cd ../sdk
npm install
npm run typecheck   # 0 errors
npm test            # passing (includes apiKey -> X-API-Key header test)
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
# health — public, and reports whether auth is on (so an open instance is visible)
curl -s http://localhost:8002/ | python3 -m json.tool
#  { "client": "ejentic", "index": "ejentic-global", "auth": {"required": true, ...}, "token_metering": true, ... }

# multi-tenant registry — now authenticated (leaks tenant topology)
curl -s -H "X-API-Key: $RAG_KEY_EXECUTIVE" http://localhost:8002/clients | python3 -m json.tool
#  { "active_client": "ejentic", "client_count": 1, "clients": [ { "id": "ejentic", ... } ] }

# grounded answer + X-* token headers (role comes FROM the key, not the body)
curl -s -D - -X POST http://localhost:8002/api/rag \
  -H "X-API-Key: $RAG_KEY_GUEST" -H 'Content-Type: application/json' \
  -d '{"query":"What services does Ejentic offer?","platform":"curl"}'
#  X-Prompt-Tokens / X-Completion-Tokens / X-Total-Tokens / X-Gated / X-Saved-Tokens present

# cross-tenant guard — MUST reject with HTTP 409 (proves isolation is enforced)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8002/api/rag \
  -H "X-API-Key: $RAG_KEY_GUEST" -H 'Content-Type: application/json' \
  -d '{"query":"hi","client":"not-this-instance"}'

# token dashboard — authenticated (leaks query text)
curl -s -H "X-API-Key: $RAG_KEY_EXECUTIVE" http://localhost:8002/metrics | python3 -m json.tool
```

#### 3a. Prove the authentication boundary (the finding this build closes)

```bash
# no key -> 401 (auth is required and no honour-system fallback)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8002/api/rag \
  -H 'Content-Type: application/json' -d '{"query":"hi"}'                       # 401

# guest key asking for executive -> 403, loud (never a silent downgrade)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8002/api/rag \
  -H "X-API-Key: $RAG_KEY_GUEST" -H 'Content-Type: application/json' \
  -d '{"query":"What was Q2 revenue?","clearance_level":"executive"}'          # 403

# guest key, executive question -> answered from PUBLIC only, so the gate REFUSES
curl -s -X POST http://localhost:8002/api/rag \
  -H "X-API-Key: $RAG_KEY_GUEST" -H 'Content-Type: application/json' \
  -d '{"query":"What was Q2 revenue?"}'                    # escalation line, no $2.4M

# executive key, same question -> ANSWERED (proves retrieval still works)
curl -s -X POST http://localhost:8002/api/rag \
  -H "X-API-Key: $RAG_KEY_EXECUTIVE" -H 'Content-Type: application/json' \
  -d '{"query":"What was Q2 revenue?"}'                    # grounded answer with [Source N]

# executive key NARROWING to guest -> allowed, answers only public material
curl -s -X POST http://localhost:8002/api/rag \
  -H "X-API-Key: $RAG_KEY_EXECUTIVE" -H 'Content-Type: application/json' \
  -d '{"query":"What was Q2 revenue?","clearance_level":"guest"}'   # refused, like a guest
```

### 4. End-to-end through the SDK (server running)

```bash
cd sdk
node --experimental-strip-types examples/usage.ts   # or: npm link + run in a TS service
# prints: active client, a grounded answer, token usage, lifetime metrics
```

### 5. UI (optional)

```bash
cd frontend
cp .env.example .env.local     # set RAG_API_KEY (server-side only, never NEXT_PUBLIC_)
npm install && npm run dev     # open http://localhost:3002
# the header shows the clearance the UI's key grants — it no longer offers a dropdown,
# because the key (held server-side by the /api/rag proxy) is what decides the role.
```

Any failing step above should correspond to a real bug: an unauthenticated call
answered instead of `401`ing (step 3a), a widened clearance served instead of
`403` (step 3a), clearances that leak (fail-closed check), an unknown `RAG_CLIENT`
accepted instead of refused (step 1b), a drift from the frozen API contracts (SDK
tests), or a silent cross-tenant request (step 3, 409).

---

See backend/RUNBOOK.md for the ingestion & verification onboarding process,
and sdk/README.md for consuming the RAG from TypeScript services.