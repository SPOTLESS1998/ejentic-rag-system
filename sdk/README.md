# @ejentic/rag-sdk

TypeScript client SDK for the **Ejentic Enterprise RAG API** (the FastAPI
backend in `../backend`). It gives every Ejentic system — Next.js apps, agent
runners, pipelines, n8n-less services — **one typed, uniform way** to query the
RAG brain, without knowing HTTP/cURL or the SSE protocol.

It is intentionally thin: no framework, no runtime deps, uses global `fetch`
(works in Node 18+, browsers, edge runtimes, and tests via an injectable
`fetchImpl`).

## Install

```bash
cd sdk
npm install        # dev deps (typescript, vitest)
npm run build      # emits dist/ (typescript declaration files included)
```

Then consume it from another Ejentic package:

```ts
import { RAGClient } from "@ejentic/rag-sdk";
// or
import { rag, getClient, createClient } from "@ejentic/rag-sdk";
```

## Quickstart

```ts
import { RAGClient } from "@ejentic/rag-sdk";

const rag = new RAGClient({ baseUrl: "http://localhost:8002", platform: "lead-gen" });

// Grounded answer + token accounting (non-streaming)
const { response, tokenUsage } = await rag.query("What services does Ejentic offer?", {
  clearanceLevel: "guest",
});
console.log(response, tokenUsage.totalTokens);

// Streaming (SSE) for chat UIs
for await (const evt of rag.stream("What is Project Delta?", { clearanceLevel: "employee" })) {
  if (evt.type === "chunk") process.stdout.write(evt.text);
}

// One-off document upload, metrics, health
await rag.upload(file, "report.pdf");
const metrics = await rag.metrics();
const health = await rag.health();
```

## API surface

| Method        | HTTP            | Purpose                                            |
| ------------- | --------------- | -------------------------------------------------- |
| `query()`     | `POST /api/rag` | Non-streaming grounded answer + X-* token headers  |
| `stream()`    | `POST /chat`    | Server-Sent Events token stream (typed events)     |
| `streamText()`| `POST /chat`    | Same stream, concatenated into a single string     |
| `upload()`    | `POST /upload`  | Index a caller-provided PDF/text for the session   |
| `metrics()`   | `GET /metrics`  | Token-accounting dashboard (totals + recent rows)  |
| `clients()`   | `GET /clients`  | Registered client configs (admin surface)          |
| `health()`    | `GET /`         | Liveness + active client/index info                |

Every method accepts an optional `AbortSignal`; `query()`/`stream()` also take
`{ clearanceLevel, platform, client }` (see `src/types.ts`).

## Multi-tenancy

A RAG instance boots for **one active client** (its `RAG_CLIENT`). The SDK
never requests a different client silently — the backend answers `409` if the
requested client isn't the instance's own, so cross-tenant data is impossible
by construction. `src/registry.ts` maps known client ids -> base URLs so
services can resolve "which deployment do I point at" without hardcoding.

## Test

```bash
npm test   # vitest against a mock fetch (no backend needed)
```

## Where this lives in the repo

```
ejentic-rag-system/
├─ backend/        # FastAPI RAG brain (Python) — the source of truth
├─ frontend/       # Next.js chat UI
└─ sdk/            # <-- you are here: TS SDK for every Ejentic consumer
```