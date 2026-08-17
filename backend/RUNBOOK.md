# Ejentic RAG — Ingestion & Verification Runbook

This is the **repeatable process** for standing up (or rebuilding) the knowledge
base behind the RAG system. Follow it top to bottom to onboard a new client, or
to reset the demo. It is written so a junior engineer can run it end to end.

The system enforces **clearance-based access**: every document carries a
`clearance` tag (`public`, `internal`, or `executive`), and the retrieval layer
only returns documents a given user is allowed to see. Ingestion is where that
tag is attached, so ingestion is security-critical.

---

## 0. Prerequisites (once per machine)

- Python virtualenv at `backend/venv` (this repo's env is built by `uv`, Python 3.11).
- A `.env` file in `backend/` with at least:
  ```
  PINECONE_API_KEY=...
  NVIDIA_API_KEY=...
  PINECONE_INDEX_NAME=ejentic-global      # optional; this is the default
  PINECONE_NAMESPACE=ejentic-internal     # optional; this is the default
  ```
- Install deps once:  `venv/bin/pip install -r requirements.txt`
  (or, since this env was built by uv: `uv pip install --python venv/bin/python -r requirements.txt`)

> Never commit `.env`. It holds live API keys.

---

## 1. Prepare the knowledge file

The RAG's knowledge lives in a JSON array where each object has `text` and
`clearance` (one of `public`, `internal`, `executive`). You rarely type this by
hand — your data starts as PDFs and documents. There are two ways to produce it.

### 1a. From real documents (PDFs / text) — the normal case

Drop each document into a subfolder **named for who may see it**. The folder name
becomes the clearance tag, so tagging is a physical act you can't mistype:

```
knowledge_sources/
├── public/        company-overview.pdf   services.txt
├── internal/      employee-handbook.pdf
└── executive/     q2-board-deck.pdf
```

Then run the converter — it extracts the text from every file and writes the
tagged JSON for you:

```bash
python pdf_to_knowledge.py                 # reads ./knowledge_sources
python pdf_to_knowledge.py --src acme_docs # a different client's folder
```

It produces `knowledge_from_docs.json`. **Open it and skim it** — this is your
chance to catch a mis-filed document before it enters the index. One file becomes
one record; big documents are chunked automatically at ingest time and every
chunk keeps its folder's clearance tag. Supported types today: `.pdf`, `.txt`,
`.md`. (Real documents you drop in `knowledge_sources/` are git-ignored so
proprietary files aren't committed by accident.)

### 1b. By hand (small or quick data)

For a handful of facts, just write the JSON directly:

```json
[
  {"text": "Public marketing copy anyone may see.",  "clearance": "public"},
  {"text": "Internal handbook, staff only.",          "clearance": "internal"},
  {"text": "Board-only financials.",                  "clearance": "executive"}
]
```

The shipped demo file is [`ejentic_knowledge.json`](ejentic_knowledge.json)
(5 public, 2 internal, 2 executive) and is what `ingest_knowledge.py` reads by
default. To onboard a new client you only change the data — never the code.

### Scanned documents & hard copies (OCR)

A scanned PDF or a photo of paper is a *picture* of text — there is no text
inside to extract. `pdf_to_knowledge.py` detects this (a PDF that yields almost
no characters) and lists exactly which files need **OCR** (optical character
recognition — software that reads text out of an image) first.

The repeatable path for paper:
1. **Scan to PDF** — a phone app (Adobe Scan, Microsoft Lens, Apple Notes'
   "Scan Documents") or an office scanner. One document → one PDF.
2. **Drop each scan straight into its clearance folder** (`public/`, `internal/`,
   `executive/`) — same as any other document.
3. **OCR everything in place** with the batch helper. It finds the scans, adds a
   real text layer to each, and leaves already-readable PDFs untouched (safe to
   re-run):
   ```bash
   brew install ocrmypdf                 # one-time (installs the OCR engine)
   python ocr_scans.py                   # OCRs scans in ./knowledge_sources
   python ocr_scans.py --src acme_docs   # a different client's folder
   ```
4. Run `pdf_to_knowledge.py` as normal — the scans now read like any other PDF.

> **One file at a time instead?** `ocr_scans.py` just wraps this per-file command,
> which you can also run by hand:
> ```bash
> ocrmypdf in_scan.pdf out_searchable.pdf   # adds a real text layer
> ```
>
> OCR needs a system install (Homebrew), so it's kept separate from the core
> pipeline. If your documents are digital PDFs (text you can select and copy),
> you can skip OCR entirely — step 1a already handles them.


### Clearance model (who sees what)

| User role   | Can retrieve                          |
|-------------|---------------------------------------|
| `guest`     | `public` only                         |
| `employee`  | `public` + `internal`                 |
| `executive` | everything (`public`+`internal`+`executive`) |

Unknown roles fail **closed** (treated as guest). This is enforced in
`build_clearance_filter()` in [`main.py`](main.py).

---

## 2. Ingest (embed + upload to the vector database)

```bash
cd backend
venv/bin/python ingest_knowledge.py                 # clean rebuild (default)
venv/bin/python ingest_knowledge.py --append        # add without wiping
venv/bin/python ingest_knowledge.py --file acme.json # a different client's file
```

What it does, in order:
1. **Validates** every record — refuses to run if any `text` is empty or any
   `clearance` is misspelled (a bad tag would make a document invisible).
2. Creates the Pinecone index if missing (cosine metric, dimension 1024 to match
   the `nv-embedqa-e5-v5` embedding model).
3. On a clean rebuild, **wipes the namespace first** so you don't mix old and new.
4. Embeds each document via NVIDIA NIM and upserts it **with its clearance tag**.

> **Why not `scrape_and_ingest.py` or `ingest_mock_data.py`?**
> `scrape_and_ingest.py` ingests a plain text file with **no clearance tag** —
> every chunk becomes invisible to non-executives. `ingest_mock_data.py` was an
> earlier attempt (now fixed, but superseded). **`ingest_knowledge.py` is the one
> canonical path.** `POST /ingest` on the running server also calls it.

---

## 3. Verify clearance isolation (the proof / the demo)

```bash
cd backend
venv/bin/python verify_clearance.py
```

This asks the same questions as guest / employee / executive and prints a matrix.
Expected with the shipped demo data:

```
question tier |   guest   | employee  | executive
--------------------------------------------------
public        |  ANSWER   |  ANSWER   |  ANSWER
internal      | escalate  |  ANSWER   |  ANSWER
executive     | escalate  | escalate  |  ANSWER
```

`escalate` means the role wasn't cleared to see anything relevant, so the
**confidence gate** refused to answer (and spent ~0 answer tokens). This is the
security boundary working — and it's a strong thing to show a client live.

---

## 3a. Grade the answers (the data-driven eval)

`verify_clearance.py` (§3) *shows* the behaviour matrix, but it doesn't grade the
wording, doesn't scan for leaks, and always exits 0. The eval harness is the
**pass/fail proof** — it runs a synthetic test set through the real pipeline and
either passes or exits non-zero, so it can gate a release or back up an accuracy
claim to a client.

```bash
cd backend
venv/bin/python eval_rag.py                 # full run (grades answers), needs .env keys
venv/bin/python eval_rag.py --no-judge      # fast: leak + behaviour only, ~0 extra tokens
venv/bin/python eval_rag.py --delay 5       # pace calls harder if the endpoint rate-limits
venv/bin/python eval_rag.py --cases acme.json   # a different client's test set
```

It scores **three things**, and each maps to a promise we make a client:

| Dimension       | Question it answers                                   | How it's judged |
|-----------------|-------------------------------------------------------|-----------------|
| **clearance**   | Did any reply leak a fact the role can't see?         | Deterministic substring scan — **any leak is a hard fail (exit 2)**. Security is never left to a fuzzy judge. |
| **correctness** | Did the answer actually state the known facts?        | **LLM-as-judge**, Pass/Fail against the ground-truth `must_include` list. |
| **gate / robustness** | Does it refuse when it *should* (out-of-scope, unknown role, prompt-injection) instead of guessing? | Deterministic — the reply must be a refusal. |

The run prints a per-dimension table and a token ledger (**system** tokens spent,
**saved-by-gate** tokens, **judge** tokens — kept separate so the eval's cost is
never confused with the product's), and writes a machine-readable
`eval_report.json` (git-ignored — it's a run artifact, regenerated each time).

**Exit codes make it CI-gateable:** `0` = all good · `1` = below `--min-pass`
(default 1.0) · `2` = a clearance leak (the loudest failure) · `3` = AI core
offline (missing keys / no index) · `4` = **incomplete** — a rate-limit or network
error left some cases unverified (see the note on `ERRORED` below).

**The test set is data, not code — `eval_cases.json`.** Each case is one object:

```json
{
  "id": "internal-delta-employee",
  "dimension": "correctness",
  "role": "employee",
  "question": "What is Project Delta?",
  "expect": "answer",
  "must_include": ["autonomous coding agent", "50% of GitHub issues", "Q4 2026"],
  "forbidden": ["2.4M", "OmniScrape"]
}
```

To grow coverage for a **new client**, add cases to this file — no code changes.
Rules of thumb: for every internal/executive fact, add a `correctness` case for a
role that *should* see it **and** a `clearance` case (`expect: "refuse"`, the fact
in `forbidden`) for a role that shouldn't. `must_include`/`forbidden` are matched
case-insensitively, so list the shortest unambiguous form of each fact.

> **Rate limits & the `ERRORED` state.** Free-tier model endpoints throttle
> (HTTP 429) under a burst of calls. The harness handles this itself: it paces
> calls (`--delay`, default 2s), retries an infra error a few times with backoff
> (`--retries`, default 3), and if a call *still* can't complete it marks that case
> **`ERRORED`** — "we couldn't test this" — instead of miscounting a transport
> failure as the system misbehaving. Errored cases never pass and never read as
> secure; they just don't count toward the pass rate, and they make the run exit
> `4` (incomplete) so you know to re-run. If you see several, raise `--delay` (e.g.
> `--delay 5`) or `--retries`. A `--no-judge` run is a fast way to confirm security
> (leaks + refusals) with far fewer calls and ~0 judge tokens.

---

## 4. Token accounting (the "measurable architecture")

Every query records its token usage to the audit database (`ejentic_audit.db`),
preferring the provider's authoritative count and falling back to a labelled
estimate. Two ways to observe it:

- **Per response:** the `/api/rag` endpoint returns `X-Prompt-Tokens`,
  `X-Completion-Tokens`, `X-Total-Tokens`, `X-Token-Source`, `X-Gated`, and
  `X-Saved-Tokens` headers.
- **Aggregate:** `GET /metrics` returns totals (queries, answered vs gated,
  token sums, average tokens per answer) plus the most recent rows and the
  active model config.

`X-Saved-Tokens` / the `estimated_saved_tokens` total quantify the input tokens
**avoided** when the confidence gate skips synthesis — i.e. the money the gate
saves by not calling the LLM on questions it can't ground.

---

## 5. Run the server

```bash
cd backend
venv/bin/uvicorn main:app --host 0.0.0.0 --port 8002
```

Health check: `GET http://localhost:8002/` should report
`"token_metering": true` and `"metrics_endpoint": "/metrics"`.

---

## Quick reference — full rebuild from scratch

```bash
cd backend
venv/bin/python ocr_scans.py                                 # 0. (only if scans) make scans readable
venv/bin/python pdf_to_knowledge.py                          # 1. docs -> tagged JSON
venv/bin/python ingest_knowledge.py --file knowledge_from_docs.json  # 2. build index
venv/bin/python verify_clearance.py                          # 3. prove roles differ
venv/bin/python eval_rag.py                                  # 3a. grade answers (pass/fail)
venv/bin/uvicorn main:app --port 8002                        # 4. serve
```

(Skip step 0 if you already have a tagged JSON — then step 1 is just
`ingest_knowledge.py`, which reads `ejentic_knowledge.json` by default.)

That's the whole repeatable process. To do it for a different business: put their
documents in `knowledge_sources/` and rerun from step 0.
