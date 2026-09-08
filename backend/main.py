"""
Ejentic AI Enterprise RAG System — FastAPI "Brain".

Implements the 3-Step Production Standard as a linear, controllable pipeline
(not a black-box agent) so every stage is observable and token-bounded:

  Step 1 — Ingestion (ingest_knowledge.py — the ONE canonical path):
           semantic chunking + the client's configured embed model + Pinecone
           upsert with a `clearance` metadata tag on every chunk. It is
           deliberately the only script that writes vectors, so there is exactly
           one place the security-critical tag is attached and validated.

  Step 2 — Advanced Retrieval (this file):
           query rewriting  ->  hybrid search (dense+sparse, auto-degrades to
           dense on a cosine index)  ->  reranking (a LOCAL cross-encoder by
           default, with a dependency-free BM25 lexical reranker as the floor)  ->
           extreme grounding prompt with mandatory citations.

  Step 3 — Guardrails & Observability (this file):
           a retrieval confidence gate that refuses to hallucinate (and, as a
           bonus, skips the LLM entirely on weak retrieval — a real token saving)
           + LangSmith env wiring so traces are captured when configured
           + per-query TOKEN METERING (a measurable architecture): every LLM hop
           is counted (authoritative provider usage when available, else a
           labelled estimate), persisted to the audit DB, and exposed via
           GET /metrics and X-*-Tokens headers — including how many tokens the
           confidence gate saved. See token_meter.py.

TOKEN-MANAGEMENT decisions baked in (the mission's #1 priority):
  * Confidence gate short-circuits synthesis on empty/weak retrieval — no answer
    tokens are spent when we have nothing grounded to say.
  * Query rewriting is heuristic-gated — the extra LLM call only fires for short
    or ambiguous queries, not for already-specific ones.
  * Reranking trims to a small top-N and each source is length-capped, so the
    synthesis prompt stays lean regardless of how much we retrieved.

API CONTRACTS ARE FROZEN (a separate AI owns the Next.js UI and n8n owns
Telegram): `/chat` streams SSE `data: {"chunk": "..."}\n\n`; `/api/rag` returns
`{"status": "success", "response": "..."}`. Do not change these shapes.

SECURITY — the clearance filter is only as meaningful as the authentication in
front of it. The caller's role comes from an API KEY (auth.py), never from the
request body; a request may NARROW its own clearance but never widen it; and
/ingest, which can delete every vector in the namespace, is admin-only. Before
that existed, `"clearance_level": "executive"` in a plain curl read board-only
material with no credential at all.
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, Header
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import json
import math
import re
import secrets
import os
import sys
from collections import OrderedDict
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from llama_index.core import VectorStoreIndex, Settings, SimpleDirectoryReader
from llama_index.core.retrievers import VectorIndexRetriever
from llama_index.core.schema import QueryBundle, NodeWithScore
from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.vector_stores.types import (
    MetadataFilters,
    MetadataFilter,
    FilterOperator,
    FilterCondition,
    VectorStoreQueryMode,
)
from llama_index.vector_stores.pinecone import PineconeVectorStore
from llama_index.llms.nvidia import NVIDIA
from llama_index.embeddings.nvidia import NVIDIAEmbedding
from pinecone import Pinecone

from database import init_db, log_query, get_token_metrics, DB_PATH
from token_meter import TokenMeter, estimate_tokens
import client_registry as registry
import auth as authmod

# ---------------------------------------------------------------------------
# PATCH: llama-index's NVIDIA client validates the model against a LIVE
# `models.list()` call to NVIDIA's API at construction time. That listing is
# intermittent under load and spuriously drops models that actually work
# (verified independently via direct API calls). When a model is absent from
# the listing we WARN rather than raise — the default endpoint is correct and
# the model is known-good, so failing the whole boot over a flaky catalog
# check would be a false negative. This keeps the server resilient.
# ---------------------------------------------------------------------------
_orig_validate = NVIDIA._validate_model


def _tolerant_validate(self, model_name: str) -> None:
    try:
        _orig_validate(self, model_name)
    except ValueError as e:
        if "unknown" in str(e) or "available_models" in str(e):
            import warnings
            warnings.warn(
                f"[llm] model {model_name!r} not in NVIDIA's live model listing "
                f"(intermittent); proceeding anyway as it is known to work. "
                f"Original: {e}"
            )
            return
        raise


NVIDIA._validate_model = _tolerant_validate


# ---------------------------------------------------------------------------
# RETRY HELPER for LLM inference.
# NVIDIA's API intermittently returns transient errors (404, connection reset,
# 5xx) under load — the model itself is known-good (verified via direct API
# calls). A short retry with backoff makes every LLM hop (query-rewrite,
# synthesis) resilient without changing behavior on the happy path.
# ---------------------------------------------------------------------------
import time as _time

_TRANSIENT_SUBSTRINGS = ("404", "page not found", "connection", "timeout",
                         "500", "502", "503", "504", "temporarily",
                         "unavailable", "inference connection")


def _is_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in _TRANSIENT_SUBSTRINGS)


async def _retry_achat(messages, *, attempts: int = 3):
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            resp = await Settings.llm.achat(messages)
            return resp
        except Exception as e:
            last_err = e
            if attempt < attempts and _is_transient(e):
                wait = 2 * attempt
                print(f"[llm] achat attempt {attempt} transient ({_short(e)}); retrying in {wait}s...")
                await asyncio.sleep(wait)
            else:
                raise
    raise last_err


async def _retry_astream_chat(messages, *, attempts: int = 3):
    """Stream chat deltas, retrying ONLY while nothing has been emitted yet.

    THE BUG THIS FIXES: the old version retried on any transient failure, but it
    had already yielded deltas downstream — restarting the generator re-sent text
    the client had, so a mid-stream blip produced a visibly duplicated answer
    ("Hello worldHello world!"). A stream is not replayable once a single token is
    out the door.

    So: retry freely before the first delta (the connection simply hadn't started),
    and once ANY delta has been yielded, re-raise instead. The caller
    (answer_stream) already recovers a truncated stream by synthesizing
    non-streaming from the SAME grounded context — that path is the right one, and
    sim_crash_test.py proves it works.
    """
    for attempt in range(1, attempts + 1):
        emitted = False
        try:
            # astream_chat returns a coroutine -> await to get the async iterable
            stream = await Settings.llm.astream_chat(messages)
            async for chunk in stream:
                emitted = True
                yield chunk
            return
        except Exception as e:
            if emitted or attempt >= attempts or not _is_transient(e):
                raise
            wait = 2 * attempt
            print(f"[llm] astream attempt {attempt} transient ({_short(e)}); "
                  f"nothing emitted yet, retrying in {wait}s...")
            await asyncio.sleep(wait)

# ---------------------------------------------------------------------------
# Environment & observability
# ---------------------------------------------------------------------------
load_dotenv()

# LangSmith / LangChain tracing. We honor the spec verbatim: if LANGCHAIN_API_KEY
# is present, tracing is switched on and every LangChain-instrumented component
# reports to the "Ejentic-RAG-Agent" project. (LlamaIndex primitives don't emit
# to LangSmith natively; the env is still set so any LangChain-based hop traces,
# and so operators can point their collector at this process.)
os.environ["LANGCHAIN_TRACING_V2"] = os.environ.get("LANGCHAIN_TRACING_V2", "false")
if os.environ.get("LANGCHAIN_API_KEY"):
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ.setdefault("LANGCHAIN_PROJECT", "Ejentic-RAG-Agent")

PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")  # reserved; kept for parity with .env

# --- Multi-tenancy: this instance serves ONE active client -----------------
# Booted from backend/clients/<RAG_CLIENT>.json, so the index, namespace,
# models, retrieval knobs and persona are all data, not code. A different
# client = a different instance (see client_registry.py).
ACTIVE_CLIENT = os.environ.get("RAG_CLIENT", "").strip() or registry.active_client_id()
CFG = registry.get_client(ACTIVE_CLIENT)

INDEX_NAME = CFG["index_name"]
PINECONE_NAMESPACE = CFG["namespace"]

# --- Tunables: client config wins, env override wins over that (quick runs) -
LLM_MODEL = os.environ.get("LLM_MODEL") or CFG["llm_model"]
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT") or os.environ.get("LLM_REQUEST_TIMEOUT") or "300")
EMBED_MODEL = os.environ.get("EMBED_MODEL") or CFG["embed_model"]
RERANK_MODEL = os.environ.get("RERANK_MODEL") or CFG["rerank_model"]  # only used when RERANK_TRY_NVIDIA=true (e.g. self-hosted NIM)

RETRIEVE_TOP_K = int(os.environ.get("RETRIEVE_TOP_K") or CFG["retrieve_top_k"])
RERANK_TOP_N = int(os.environ.get("RERANK_TOP_N") or CFG["rerank_top_n"])
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD") or CFG["confidence_threshold"])
MAX_CONTEXT_CHARS = int(os.environ.get("MAX_CONTEXT_CHARS") or CFG["max_context_chars"])
HYBRID_ALPHA = float(os.environ.get("HYBRID_ALPHA") or CFG["hybrid_alpha"])
RERANK_ALPHA = float(os.environ.get("RERANK_ALPHA") or CFG["rerank_alpha"])
# NVIDIA's PUBLIC hosted reranking API reached end-of-life on 2026-05-18 (HTTP 410
# Gone) — no model name revives it. So the dead tier is NOT tried by default;
# reranking runs on a LOCAL cross-encoder instead. Set RERANK_TRY_NVIDIA=true only
# when you have a reachable (e.g. self-hosted) NIM rerank endpoint at RERANK_MODEL.
RERANK_TRY_NVIDIA = os.environ.get("RERANK_TRY_NVIDIA", "false").lower() == "true"
_qr_env = os.environ.get("ENABLE_QUERY_REWRITE")
if _qr_env is not None:
    ENABLE_QUERY_REWRITE = _qr_env.lower() == "true"
else:
    ENABLE_QUERY_REWRITE = bool(CFG["query_rewrite_enabled"])

# The escalation line + persona come from the client config (per-tenant tone).
ESCALATION_LINE = os.environ.get("ESCALATION_LINE") or CFG["escalation_line"]
PERSONA_NAME = CFG["persona_name"]
PERSONA_STYLE = CFG["persona_style"]

GROUNDING_SYSTEM_PROMPT = (
    f"You are the {PERSONA_NAME} — {PERSONA_STYLE}. You answer ONLY from the numbered "
    "context passages provided. You must cite the sources you use inline as [Source N]. "
    "When the context enumerates specific items — a list of technologies, product "
    "names, figures, or steps — reproduce EVERY item from the context; never condense "
    "a list down to a subset or drop items for brevity. Respond in PLAIN TEXT only: "
    "never use markdown formatting of any kind — no asterisks, no hashes, no "
    "backticks. Write lists as simple lines. If the retrieved context does "
    "not contain the answer, you must gracefully "
    f"say exactly: '{ESCALATION_LINE}' Never invent facts, prices, policies, or "
    "sources that are not in the context."
)

# ---------------------------------------------------------------------------
# Global singletons (built once at import; guarded so the app still boots for
# health checks even if keys are missing).
# ---------------------------------------------------------------------------
pinecone_index = None
vector_store = None
global_index = None
reranker = None
HYBRID_ENABLED = False

# Uploaded PDFs, keyed by the opaque token /upload returns. This used to be a
# single process-wide `pdf_index` folded into EVERY caller's query — so one
# person's uploaded document became retrievable context for everyone hitting the
# instance, with no clearance filter. Now a query only sees a PDF whose token it
# presents. Bounded so an upload flood cannot exhaust memory.
MAX_UPLOADS = int(os.environ.get("MAX_UPLOADS", "8"))
_uploads: "OrderedDict[str, dict]" = OrderedDict()

# Sidecar so an upload survives a restart. The in-memory index does not, and
# without this a restart silently forgets the user's document while the file is
# still sitting in data/uploads. It maps token -> stored file, and is read ONLY
# when a caller presents that token, so rehydration stays per-caller: the old
# behaviour re-indexed "the latest upload" into a process-wide index, which
# handed one person's document to whoever queried next.
# (The tokens live next to the files they unlock, so this grants no access that
# read permission on data/uploads doesn't already give.)
_UPLOAD_SIDECAR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "uploads", "_tokens.json")


def _sidecar_read() -> dict:
    try:
        with open(_UPLOAD_SIDECAR) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _sidecar_write(entries: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_UPLOAD_SIDECAR), exist_ok=True)
        with open(_UPLOAD_SIDECAR, "w") as fh:
            json.dump(entries, fh)
    except Exception as e:
        print(f"[upload] could not persist upload token map: {_short(e)}")


def _remember_upload(index, filename: str, path: str = "") -> str:
    """Store an uploaded PDF's index under a fresh unguessable token (LRU-capped)."""
    token = secrets.token_urlsafe(24)
    _uploads[token] = {"index": index, "filename": filename, "path": path}
    _uploads.move_to_end(token)
    while len(_uploads) > MAX_UPLOADS:
        _uploads.popitem(last=False)   # evict the least-recently-used
    if path:
        entries = _sidecar_read()
        entries[token] = {"path": path, "filename": filename}
        # Keep the sidecar bounded the same way the memory cache is.
        for stale in list(entries)[:-max(MAX_UPLOADS, 1)]:
            entries.pop(stale, None)
        _sidecar_write(entries)
    return token


def _upload_index(token: str):
    """The index for this token, or None. Unknown/expired tokens are simply
    ignored rather than erroring — the query still runs against the KB.

    On a miss we try the sidecar once: the process may have restarted since the
    upload, and re-reading the file the token points at is what lets the caller
    keep querying their document across a restart.
    """
    key = (token or "").strip()
    if not key:
        return None
    entry = _uploads.get(key)
    if entry:
        _uploads.move_to_end(key)
        return entry["index"]

    saved = _sidecar_read().get(key)
    if not saved or not os.path.exists(saved.get("path", "")):
        return None
    try:
        docs, _skipped = _load_upload_documents(saved["path"], saved.get("filename", ""))
        if not docs:
            return None
        index = VectorStoreIndex.from_documents(docs)
    except Exception as e:
        print(f"[upload] re-index after restart failed: {_short(e)}")
        return None
    _uploads[key] = {"index": index, "filename": saved.get("filename", ""),
                     "path": saved["path"]}
    _uploads.move_to_end(key)
    while len(_uploads) > MAX_UPLOADS:
        _uploads.popitem(last=False)
    print(f"[upload] re-indexed after restart: {saved.get('filename', '')}")
    return index


def _short(e, n: int = 110) -> str:
    """Collapse an exception to a single tidy log line."""
    return str(e).replace("\n", " ")[:n]


_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Tiny stopword set so common words don't dominate the lexical score.
_STOP = frozenset(
    "the a an of to and or in on for is are was were be been with as at by from "
    "that this it its into your you our we they what which who whom how when "
    "where why do does did done has have had will would can could should".split()
)


def _tokenize(text: str) -> list:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP]


class LexicalReranker:
    """Dependency-free BM25 reranker over the small retrieved candidate set.

    Reorders the dense-retrieved nodes by blending their vector score with a
    classic BM25 lexical score computed over just those candidates. No model, no
    API call, no tokens — reranking costs nothing per query and can never be
    rate-limited. It rescues exact-term matches (names, codes, dollar figures)
    that pure semantic search can rank too low. A hosted / cross-encoder reranker
    is a drop-in upgrade; this is the honest, budget-friendly floor and a down
    payment on the hybrid-search work. Duck-types the LlamaIndex postprocessor
    interface (`postprocess_nodes`) without subclassing it (rule 1: no framework
    coupling)."""

    def __init__(self, top_n: int, alpha: float = 0.5, k1: float = 1.5, b: float = 0.75):
        self.top_n = top_n
        self.alpha = alpha  # weight on the dense score; (1 - alpha) on the BM25 score
        self.k1 = k1
        self.b = b

    @staticmethod
    def _minmax(xs: list) -> list:
        """Scale to [0,1]. A flat signal (all values equal) contributes nothing,
        so the other signal decides the order rather than injecting noise."""
        lo, hi = min(xs), max(xs)
        if hi - lo < 1e-9:
            return [0.0 for _ in xs]
        return [(x - lo) / (hi - lo) for x in xs]

    def _bm25(self, q_terms: list, docs: list) -> list:
        n = len(docs)
        avgdl = (sum(len(d) for d in docs) / n) if n else 0.0
        df = {t: sum(1 for d in docs if t in d) for t in set(q_terms)}
        scores = []
        for d in docs:
            dl = len(d)
            tf = {}
            for t in d:
                tf[t] = tf.get(t, 0) + 1
            s = 0.0
            for t in q_terms:
                if df.get(t, 0) == 0:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                f = tf.get(t, 0)
                denom = f + self.k1 * (1 - self.b + self.b * (dl / avgdl if avgdl else 0.0))
                s += (idf * (f * (self.k1 + 1)) / denom) if denom else 0.0
            scores.append(s)
        return scores

    def postprocess_nodes(self, nodes, query_bundle=None, query_str: str = None):
        if not nodes:
            return nodes
        query = query_str
        if query is None and query_bundle is not None:
            query = getattr(query_bundle, "query_str", None) or str(query_bundle)
        q_terms = _tokenize(query or "")
        if not q_terms:  # nothing to match on -> keep dense order, just trim
            return list(nodes)[: self.top_n]

        docs = [_tokenize(n.node.get_content()) for n in nodes]
        dense = self._minmax([(n.score or 0.0) for n in nodes])
        lexical = self._minmax(self._bm25(q_terms, docs))
        blended = [self.alpha * dense[i] + (1 - self.alpha) * lexical[i] for i in range(len(nodes))]
        order = sorted(range(len(nodes)), key=lambda i: blended[i], reverse=True)
        return [nodes[i] for i in order[: self.top_n]]


def _probe_reranker(rr) -> None:
    """Make a REAL rerank call so a reranker that merely *constructs* but fails
    on use (e.g. a retired hosted endpoint returning 404/410) is rejected here,
    at build time — not silently, once per query, in production."""
    from llama_index.core.schema import TextNode, NodeWithScore, QueryBundle
    probe = [
        NodeWithScore(node=TextNode(text="Ejentic offers RAG and autonomous AI agents."), score=0.9),
        NodeWithScore(node=TextNode(text="Paris is the capital of France."), score=0.4),
    ]
    rr.postprocess_nodes(probe, query_bundle=QueryBundle("What does Ejentic offer?"))


def _build_reranker():
    """Reranker with a graceful, VERIFIED fallback chain. Each network-backed
    tier must pass a real probe call before we trust it, so we never advertise a
    stage that silently no-ops at query time.

    Priority: local sentence-transformers cross-encoder (probed) -> dependency-free
    BM25 lexical reranker (always available, $0/query). The lexical tier is the
    guaranteed floor, so retrieval is always reranked by *something* real.

    NVIDIA's PUBLIC hosted reranking endpoint reached end-of-life on 2026-05-18
    (HTTP 410 Gone), so it is NOT tried by default — it would only fail a probe on
    every boot. Set RERANK_TRY_NVIDIA=true (e.g. for a self-hosted NIM) to put it
    back at the front of the chain."""
    # 0) OPTIONAL NVIDIA NIM cross-encoder — OFF by default (public SaaS EOL'd
    #    2026-05-18 -> 410 Gone). Attempted only when RERANK_TRY_NVIDIA=true, e.g.
    #    when RERANK_MODEL points at a reachable self-hosted NIM. Probed either
    #    way, because construction alone won't reveal a dead endpoint.
    if RERANK_TRY_NVIDIA:
        try:
            from llama_index.postprocessor.nvidia_rerank import NVIDIARerank
            rr = NVIDIARerank(model=RERANK_MODEL, api_key=NVIDIA_API_KEY, top_n=RERANK_TOP_N)
            _probe_reranker(rr)
            print(f"[rerank] NVIDIA cross-encoder active (probed OK): {RERANK_MODEL}")
            return rr
        except Exception as e:
            print(f"[rerank] NVIDIA reranker unavailable ({_short(e)}); trying local cross-encoder.")

    # 1) Local sentence-transformers cross-encoder — the DEFAULT real reranker:
    #    $0/query, offline, never retired. Probe it too, in case the model can't load.
    try:
        from llama_index.core.postprocessor import SentenceTransformerRerank
        rr = SentenceTransformerRerank(
            model="cross-encoder/ms-marco-MiniLM-L-6-v2", top_n=RERANK_TOP_N
        )
        _probe_reranker(rr)
        print("[rerank] Local sentence-transformers cross-encoder active (probed OK).")
        return rr
    except Exception as e:
        print(f"[rerank] Local cross-encoder unavailable ({_short(e)}); "
              f"using dependency-free BM25 lexical reranker.")

    # 2) Dependency-free lexical (BM25) reranker — no deps, no tokens, no API
    #    calls; always available, so retrieval is never left unranked.
    print(f"[rerank] BM25 lexical reranker active (no deps, no tokens; alpha={RERANK_ALPHA}).")
    return LexicalReranker(top_n=RERANK_TOP_N, alpha=RERANK_ALPHA)


# RAG_OFFLINE=1 skips ALL model and vector-store construction at import.
#
# Importing this module normally builds an NVIDIA embedding client, an LLM client
# and a Pinecone connection — real network calls, before a single request. The
# offline test suite needs the pure logic in here (auth, clearance filters, the
# stream helpers) without any of that, and a suite that quietly talks to Pinecone
# on someone's laptop is not an offline suite. The endpoints degrade exactly as
# they already do when Pinecone is unreachable: global_index stays None.
RAG_OFFLINE = (os.environ.get("RAG_OFFLINE") or "").strip().lower() in ("1", "true", "yes")

if RAG_OFFLINE:
    print("[offline] RAG_OFFLINE=1 — skipping model + Pinecone init (tests only).")
elif not PINECONE_API_KEY or not NVIDIA_API_KEY:
    print("Error: API keys are not properly configured (PINECONE_API_KEY / NVIDIA_API_KEY).")
else:
    # Step 1 models — embeddings + LLM on the NVIDIA stack.
    Settings.embed_model = NVIDIAEmbedding(model=EMBED_MODEL, api_key=NVIDIA_API_KEY)

    # The NVIDIA client validates the model against a live `models.list()` call
    # at construction. That listing is intermittent under load and can spuriously
    # reject a working model, so retry a few times before giving up.
    import time
    last_err = None
    for attempt in range(1, 6):
        try:
            Settings.llm = NVIDIA(model=LLM_MODEL, api_key=NVIDIA_API_KEY, timeout=LLM_TIMEOUT)
            print(f"[llm] {LLM_MODEL} initialized (attempt {attempt}).")
            last_err = None
            break
        except Exception as e:
            last_err = e
            if attempt < 5:
                print(f"[llm] init attempt {attempt} failed ({_short(e)}); retrying...")
                time.sleep(2 * attempt)
            else:
                print(f"[llm] init failed after 5 attempts: {_short(e)}")
    if last_err is not None:
        print("[llm] WARNING: LLM unavailable — queries will return offline.")
    # Semantic chunking target (500/50) — applies to future ingestion & /upload.
    Settings.chunk_size = int(os.environ.get("CHUNK_SIZE", "500"))
    Settings.chunk_overlap = int(os.environ.get("CHUNK_OVERLAP", "50"))

    try:
        print("Initializing Pinecone connection...")
        pc = Pinecone(api_key=PINECONE_API_KEY)
        pinecone_index = pc.Index(INDEX_NAME)

        # Auto-detect the index metric. Real hybrid search (dense+sparse) requires
        # a dotproduct index; on a cosine index we degrade to dense + reranking
        # rather than fail at query time.
        try:
            metric = pc.describe_index(INDEX_NAME).metric
            HYBRID_ENABLED = metric == "dotproduct"
            print(f"[hybrid] Pinecone index metric = '{metric}'. "
                  f"Hybrid search {'ENABLED' if HYBRID_ENABLED else 'DISABLED -> dense+rerank fallback'}.")
        except Exception as e:
            print(f"[hybrid] Could not read index metric ({e}); assuming dense-only.")
            HYBRID_ENABLED = False

        vector_store = PineconeVectorStore(
            pinecone_index=pinecone_index, namespace=PINECONE_NAMESPACE
        )
        global_index = VectorStoreIndex.from_vector_store(vector_store=vector_store)
        reranker = _build_reranker()
        print("Global index ready.")
    except Exception as e:
        print(f"Error initializing Pinecone: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown. Replaces the deprecated @app.on_event("startup")."""
    await init_db()
    print(f"Audit Database Initialized ({DB_PATH}).")
    if not authmod.is_enabled(CFG):
        print("⚠️  AUTH IS OFF — clearance is taken from the request body, so any "
              "caller can ask for any role. Fine locally; NEVER expose this "
              "instance. Set auth.required=true in the client config.")
    else:
        st = authmod.status(CFG)
        print(f"🔐 auth ON — roles with keys: {', '.join(st['roles_with_keys_set']) or 'NONE'}"
              + (f" | missing: {', '.join(st['roles_missing_keys'])}" if st["roles_missing_keys"] else ""))
    yield


app = FastAPI(title="Ejentic AI Enterprise RAG System", lifespan=lifespan)

# CORS: an explicit allow-list, NOT "*". The old `allow_origins=["*"]` together
# with `allow_credentials=True` let any web page on the internet read a deployed
# instance from a visitor's browser. Override for a real deployment with
# CORS_ORIGINS="https://app.example.com,https://admin.example.com".
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS", "http://localhost:3002,http://127.0.0.1:3002").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", authmod.API_KEY_HEADER,
                   "X-Upload-Token"],
    # The token headers are on the RESPONSE, and a browser cannot read a response
    # header unless it is explicitly exposed — without this the UI sees them as
    # absent and silently reports no token usage.
    expose_headers=["X-Prompt-Tokens", "X-Completion-Tokens", "X-Total-Tokens",
                    "X-Token-Source", "X-Gated", "X-Saved-Tokens", "X-Clearance"],
)


class QueryRequest(BaseModel):
    query: str
    # The clearance a caller ASKS for. It is no longer trusted on its own: the
    # role comes from the API key, and this may only NARROW it (see auth.py).
    clearance_level: str = ""
    platform: str = "WEB_UI"
    client: str = ""  # optional; defaults to this instance's active client


class IngestRequest(BaseModel):
    """Body for POST /ingest.

    `rebuild` defaults to FALSE on purpose. The old endpoint always ran a clean
    rebuild, which deletes every vector in the namespace — one unauthenticated
    POST wiped the client's whole knowledge base. Destroying data now has to be
    asked for explicitly, by an admin.
    """
    rebuild: bool = False
    file: str = ""


def _authed_role(x_api_key: str | None, authorization: str | None) -> str:
    """Resolve the caller's role from headers, as an HTTP-ready failure."""
    try:
        return authmod.authenticate(
            authmod.key_from_headers(x_api_key, authorization), CFG)
    except authmod.AuthError as e:
        raise authmod.as_http(e)


def _clearance_for(role: str, requested: str) -> str:
    """Narrow-only clearance resolution, as an HTTP-ready failure."""
    try:
        return authmod.effective_clearance(role, requested, CFG)
    except authmod.AuthError as e:
        raise authmod.as_http(e)


# ---------------------------------------------------------------------------
# Multi-tenancy: resolve the caller's clearance LEVEL onto the stored
# `clearance` metadata VALUES configured for the active client
# (public / internal / executive for the default 'ejentic' client).
# ---------------------------------------------------------------------------
def _least_privileged_tags(cfg: dict) -> list:
    """Fail-closed fallback: the tags of the least-privileged role, i.e. the
    non-wildcard role with the fewest allowed tags."""
    candidates = [
        (role, tags) for role, tags in cfg["clearance_levels"].items()
        if isinstance(tags, list) and tags
    ]
    if not candidates:
        return ["public"]
    return sorted(candidates, key=lambda pair: len(pair[1]))[0][1]


def build_clearance_filter(clearance_level: str, cfg: dict | None = None):
    cfg = cfg or CFG
    # Coerce defensively. Pydantic gives us a str through the API, but this is the
    # function that decides what a caller can SEE, and every internal caller must
    # get a fail-closed filter rather than an AttributeError. A 500 here would be a
    # crash on the security path — noisy, but also a worse failure than a refusal.
    level = clearance_level if isinstance(clearance_level, str) else ""
    level = level.strip().lower()
    tags = cfg["clearance_levels"].get(level)

    if tags == "*":
        return None  # unrestricted

    if isinstance(tags, list) and tags:
        filters = [
            MetadataFilter(key="clearance", value=tag, operator=FilterOperator.EQ)
            for tag in tags
        ]
        if len(filters) == 1:
            return MetadataFilters(filters=filters)
        return MetadataFilters(filters=filters, condition=FilterCondition.OR)

    # Unknown role -> fail CLOSED to the least privileged tier (safest).
    tags = _least_privileged_tags(cfg)
    return MetadataFilters(
        filters=[MetadataFilter(key="clearance", value=tags[0], operator=FilterOperator.EQ)]
    )


def resolve_request_client(request_client: str) -> dict:
    """Validate the optional `client` field on a request.

    A single instance boots one index/model set for its ACTIVE client, so it can
    only serve that client. Requesting a different one is a loud 409 (a distinct
    deployment, not silent cross-tenant fallback). Raises ValueError; endpoints
    convert it to HTTP 409.
    """
    cid = (request_client or "").strip() or ACTIVE_CLIENT
    if cid != ACTIVE_CLIENT:
        raise ValueError(
            f"client '{cid}' is not the active client on this instance "
            f"(active: '{ACTIVE_CLIENT}'). A different client is a separate "
            "deployment: start another instance with RAG_CLIENT=<id> "
            "(see backend/clients/)."
        )
    return CFG


# ---------------------------------------------------------------------------
# Step 2a — Query rewriting (heuristic-gated to save tokens)
# ---------------------------------------------------------------------------
_VAGUE_TOKENS = {"it", "they", "them", "this", "that", "those", "these", "he",
                 "she", "here", "there", "one", "ones", "thing", "stuff"}


def _needs_rewrite(query: str) -> bool:
    """Only rewrite when it's likely to help: short queries or ones leaning on
    pronouns/vague terms. Specific, well-formed questions are used verbatim so we
    don't burn an LLM call for nothing."""
    words = [w.strip("?.!,").lower() for w in query.split()]
    if len(words) < 8:
        return True
    return any(w in _VAGUE_TOKENS for w in words)


async def rewrite_query(query: str, meter=None) -> str:
    if not ENABLE_QUERY_REWRITE or not _needs_rewrite(query):
        return query
    try:
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=(
                "Rewrite the user's message into a single, concise, keyword-rich "
                "search query that maximizes document retrieval recall. Expand "
                "abbreviations and resolve vague references. Respond with ONLY the "
                "rewritten query — no quotes, no preamble.")),
            ChatMessage(role=MessageRole.USER, content=query),
        ]
        resp = await _retry_achat(messages)
        rewritten = (resp.message.content or "").strip().strip('"').split("\n")[0]
        # Meter the rewrite hop — it's a real (if small) token spend on the query.
        if meter is not None:
            meter.record_response(
                resp,
                fallback_prompt_text=_messages_text(messages),
                fallback_completion_text=rewritten,
            )
        return rewritten or query
    except Exception as e:
        print(f"[rewrite] fell back to original query: {e}")
        return query


# ---------------------------------------------------------------------------
# Step 2b/2c — Retrieve (hybrid or dense) then rerank
# ---------------------------------------------------------------------------
async def retrieve_and_rerank(query: str, clearance_level: str, meter=None,
                              upload_token: str = ""):
    """Returns (nodes, max_raw_score, rewritten_query). `nodes` is the reranked,
    length-capped top-N ready for grounded synthesis.

    `upload_token` folds in ONLY the PDF that this caller uploaded (see /upload).
    """
    filters = build_clearance_filter(clearance_level)
    search_query = await rewrite_query(query, meter=meter)

    retriever = VectorIndexRetriever(
        index=global_index,
        similarity_top_k=RETRIEVE_TOP_K,
        filters=filters,
        vector_store_query_mode=(
            VectorStoreQueryMode.HYBRID if HYBRID_ENABLED else VectorStoreQueryMode.DEFAULT
        ),
        alpha=HYBRID_ALPHA if HYBRID_ENABLED else None,
    )

    raw_nodes = await retriever.aretrieve(search_query)

    # Fold in THIS CALLER'S uploaded PDF (only if they presented its token) so it
    # competes in the same rerank. It isn't clearance-filtered because it is the
    # caller's own document — which is exactly why it must not be shared: without
    # the token check, everyone's queries would retrieve everyone's uploads.
    own_pdf = _upload_index(upload_token)
    if own_pdf is not None:
        try:
            pdf_retriever = own_pdf.as_retriever(similarity_top_k=3)
            raw_nodes = list(raw_nodes) + list(await pdf_retriever.aretrieve(search_query))
        except Exception as e:
            print(f"[upload] pdf retrieval skipped: {e}")

    max_raw_score = max((n.score or 0.0 for n in raw_nodes), default=0.0)

    # Rerank (cross-encoder / LLM). Reranker prunes to RERANK_TOP_N itself.
    nodes = raw_nodes
    if reranker is not None and raw_nodes:
        try:
            nodes = reranker.postprocess_nodes(raw_nodes, query_bundle=QueryBundle(query))
        except Exception as e:
            print(f"[rerank] postprocess failed, using raw order: {e}")
            nodes = raw_nodes[:RERANK_TOP_N]
    else:
        nodes = raw_nodes[:RERANK_TOP_N]

    return nodes, max_raw_score, search_query


def _build_grounded_messages(question: str, nodes) -> list:
    """Assemble the citation-ready prompt. Each source is numbered and length-
    capped so the input token count stays bounded no matter what we retrieved."""
    blocks = []
    for i, nws in enumerate(nodes, start=1):
        md = getattr(nws.node, "metadata", {}) or {}
        cl: str = md.get("clearance", "unknown")
        src = md.get("file_name") or md.get("source") or md.get("title") or f"kb-{i}"
        text = (nws.node.get_content() or "")[:MAX_CONTEXT_CHARS]
        blocks.append(f"[Source {i}] (clearance={cl}, ref={src})\n{text}")
    context_str = "\n\n".join(blocks) if blocks else "(no context retrieved)"

    user_content = (
        f"Context passages:\n{context_str}\n\n"
        f"User question: {question}\n\n"
        "Answer using ONLY the context above and cite each fact as [Source N]."
    )
    return [
        ChatMessage(role=MessageRole.SYSTEM, content=GROUNDING_SYSTEM_PROMPT),
        ChatMessage(role=MessageRole.USER, content=user_content),
    ]


def _messages_text(messages) -> str:
    """Flatten a ChatMessage list to plain text (for token estimation)."""
    return "\n".join(str(getattr(m, "content", "") or "") for m in messages)


def _estimate_gate_savings(question: str, nodes) -> int:
    """When the confidence gate skips synthesis, estimate the INPUT tokens avoided.

    HONESTY NOTE: this must price the prompt we WOULD ACTUALLY HAVE SENT. The
    grounded prompt only ever carries the top RERANK_TOP_N sources, so counting
    every retrieved node inflated the figure whenever retrieval returned a wide
    but low-scoring set — the savings metric flattered itself exactly when the
    gate fired most. Trim first, then count. Still a conservative floor: it counts
    the prompt not sent, never the answer not generated.
    """
    try:
        would_send = list(nodes)[:RERANK_TOP_N]
        return estimate_tokens(_messages_text(_build_grounded_messages(question, would_send)))
    except Exception:
        return 0


def _passes_confidence(nodes, max_raw_score: float) -> bool:
    """Step 3 guardrail: refuse to answer (and skip the LLM entirely) when
    retrieval is empty or the best match is below the trust threshold."""
    return bool(nodes) and max_raw_score >= CONFIDENCE_THRESHOLD


_ASSISTANT_MARKER = "assistant:"


def _strip_assistant_prefix(text: str) -> str:
    # Some LlamaIndex/LLM paths echo a leading "assistant:" role marker.
    stripped = text.lstrip()
    if stripped.lower().startswith(_ASSISTANT_MARKER):
        return stripped[len(_ASSISTANT_MARKER):].lstrip()
    return text


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _strip_markdown(text: str) -> str:
    """Remove markdown decoration so answers never read as AI boilerplate.

    The persona contract is plain text: asterisks (bold/italic), backticks
    (code), leading heading hashes, and [text](url) links are all flattened.
    Underscores are deliberately KEPT — they appear in real identifiers."""
    if not text:
        return text
    text = _MD_LINK_RE.sub(r"\1", text)            # [text](url) -> text
    text = text.replace("**", "").replace("*", "")  # bold/italic markers
    text = text.replace("`", "")                    # inline code ticks
    lines = []
    for line in text.splitlines():
        lines.append(re.sub(r"^\s{0,3}#{1,6}\s+", "", line))  # heading hashes
    return "\n".join(lines).replace("\n\n\n+", "\n\n")


def _plain_delta(delta: str) -> str:
    """Per-chunk sanitizer for streaming: drop characters that are NEVER
    wanted in output (markdown asterisks/backticks) so nothing AI-flavored
    reaches the UI even mid-stream. Cheap and order-independent."""
    return delta.replace("*", "").replace("`", "") if delta else delta


def _could_be_marker_prefix(buffer: str) -> bool:
    """Could this partial buffer still grow into a leading 'assistant:' marker?

    THE BUG THIS FIXES: the old inline check asked
    `buffer.startswith("assistant")`, which is the wrong direction for a PARTIAL
    buffer. With buffer="A" that is False, so the code decided "not a marker" and
    flushed immediately — meaning a real `assistant:` arriving split across deltas
    ("A" + "ssistant:" + " Hi") sailed straight through to the user. The right
    question is whether the MARKER starts with the buffer.
    """
    probe = buffer.lstrip().lower()
    if not probe:
        return True   # nothing to judge yet; keep holding
    return _ASSISTANT_MARKER.startswith(probe)


def _consume_prefix_delta(buffer: str, delta: str):
    """Feed one streamed delta through the marker check.

    Returns (new_buffer, text_to_yield, done_checking). While the buffer could
    still become 'assistant:' we hold it; the moment it can't, we release it
    unchanged; once the full marker is in hand we strip it and release the rest.
    Extracted from answer_stream so this fiddly state machine is unit-testable.

    Anything released here is the FIRST text of the answer, so it is left-stripped:
    without that, a marker arriving one character at a time consumed 'assistant:'
    and then let the following space through, so the answer began with whitespace
    while the same marker in a single delta came out clean. Releasing nothing keeps
    us in checking mode rather than declaring the prefix handled.
    """
    buffer += delta
    if len(buffer.lstrip()) >= len(_ASSISTANT_MARKER):
        # Enough characters to decide for certain.
        out = _strip_assistant_prefix(buffer).lstrip()
        return ("", out, True) if out else ("", "", False)
    if not _could_be_marker_prefix(buffer):
        # It can never become the marker -> release verbatim, stop checking.
        out = buffer.lstrip()
        return ("", out, True) if out else ("", "", False)
    return buffer, "", False    # still ambiguous: keep buffering


# ---------------------------------------------------------------------------
# Answer producers (shared by both endpoints)
# ---------------------------------------------------------------------------
async def answer_once(query: str, clearance_level: str, platform: str,
                      upload_token: str = "", actor: str = None):
    """Non-streaming answer for /api/rag (n8n). Returns (text, meter, gated,
    saved_tokens) so the endpoint can surface token counts in headers without
    touching the frozen JSON body.

    `actor` is recorded in the audit trail and used for nothing else — clearance was
    already decided from the API key before this is called."""
    meter = TokenMeter()
    if global_index is None:
        return "System Offline: AI Core is booting or missing API keys.", meter, False, 0
    try:
        nodes, max_score, _ = await retrieve_and_rerank(
            query, clearance_level, meter=meter, upload_token=upload_token)
        if not _passes_confidence(nodes, max_score):
            saved = _estimate_gate_savings(query, nodes)
            asyncio.create_task(log_query(
                f"{platform}_{clearance_level}", query, "[GATE] " + ESCALATION_LINE,
                prompt_tokens=meter.prompt_tokens, completion_tokens=meter.completion_tokens,
                total_tokens=meter.total_tokens, estimated_saved_tokens=saved,
                token_source=meter.source if meter.total_tokens else "gate", gated=True,
                client=ACTIVE_CLIENT, actor=actor,
            ))
            return ESCALATION_LINE, meter, True, saved

        messages = _build_grounded_messages(query, nodes)
        resp = await _retry_achat(messages)
        text = _strip_markdown(_strip_assistant_prefix((resp.message.content or "").strip()))
        meter.record_response(
            resp, fallback_prompt_text=_messages_text(messages), fallback_completion_text=text
        )
        asyncio.create_task(log_query(
            f"{platform}_{clearance_level}", query, text,
            prompt_tokens=meter.prompt_tokens, completion_tokens=meter.completion_tokens,
            total_tokens=meter.total_tokens, token_source=meter.source, gated=False,
            client=ACTIVE_CLIENT, actor=actor,
        ))
        return text, meter, False, 0
    except Exception as e:
        asyncio.create_task(log_query(f"{platform}_{clearance_level}", query,
                                      f"ERROR: {e}", client=ACTIVE_CLIENT, actor=actor))
        return f"I encountered a cognitive error while processing that request: {e}", meter, False, 0


async def answer_stream(query: str, clearance_level: str, platform: str,
                        upload_token: str = "", actor: str = None):
    """Async token generator for /chat (Web UI). Yields raw text chunks; the
    endpoint wraps each into the SSE `data: {"chunk": ...}` envelope.

    `actor` is recorded in the audit trail and used for nothing else — clearance was
    already decided from the API key before this is called."""
    if global_index is None:
        yield "System Offline: AI Core is booting or missing API keys."
        return

    meter = TokenMeter()
    nodes, max_score, _ = await retrieve_and_rerank(
        query, clearance_level, meter=meter, upload_token=upload_token)
    if not _passes_confidence(nodes, max_score):
        saved = _estimate_gate_savings(query, nodes)
        asyncio.create_task(log_query(
            f"{platform}_{clearance_level}", query, "[GATE] " + ESCALATION_LINE,
            prompt_tokens=meter.prompt_tokens, completion_tokens=meter.completion_tokens,
            total_tokens=meter.total_tokens, estimated_saved_tokens=saved,
            token_source=meter.source if meter.total_tokens else "gate", gated=True,
            client=ACTIVE_CLIENT, actor=actor,
        ))
        yield ESCALATION_LINE
        return

    messages = _build_grounded_messages(query, nodes)
    collected = []
    prefix_checked = False
    buffer = ""
    fallback_handled = False
    try:
        async for chunk in _retry_astream_chat(messages):
            delta = _plain_delta(chunk.delta or "")
            if not delta:
                continue
            collected.append(delta)

            # Strip a leading "assistant:" that may arrive split across deltas.
            # The state machine lives in _consume_prefix_delta so it can be tested
            # directly — the inline version had an inverted prefix check that let a
            # split marker through.
            if not prefix_checked:
                buffer, out, prefix_checked = _consume_prefix_delta(buffer, delta)
                if out:
                    yield out
                continue

            yield delta

        if buffer:  # flush any residual held for prefix inspection
            cleaned = _strip_assistant_prefix(buffer)
            if cleaned:
                yield cleaned
    except Exception as stream_err:
        # NVIDIA's engine can crash mid-stream (observed live: vLLM "EngineCore
        # encountered an issue"). The SSE response is already committed, so the
        # stream cannot be replayed — recover by synthesizing NON-streaming with
        # the SAME grounded context and delivering the complete answer as one
        # chunk. The UI always gets a usable reply; the frozen SSE contract is
        # untouched.
        print(f"[stream] streaming failed ({_short(stream_err)}); "
              f"falling back to non-streaming synthesis.")
        resp = await _retry_achat(messages)
        text = _strip_markdown(_strip_assistant_prefix((resp.message.content or "").strip()))
        meter.record_response(
            resp, fallback_prompt_text=_messages_text(messages),
            fallback_completion_text=text,
        )
        delivered = _strip_assistant_prefix("".join(collected)).strip()
        if delivered:
            yield "\n\n[stream interrupted — answer recovered] " + text
        else:
            yield text
        asyncio.create_task(log_query(
            f"{platform}_{clearance_level}", query,
            (delivered + "\n\n" + text).strip() if delivered else text,
            prompt_tokens=meter.prompt_tokens, completion_tokens=meter.completion_tokens,
            total_tokens=meter.total_tokens, token_source=meter.source, gated=False,
            client=ACTIVE_CLIENT, actor=actor,
        ))
        fallback_handled = True
        return
    finally:
        if not fallback_handled:
            full = _strip_assistant_prefix("".join(collected)).strip()
            if full:
                # Streaming rarely reports provider usage, so estimate: prompt from the
                # grounded messages we sent, completion from the accumulated answer.
                meter.record_estimate(_messages_text(messages), full)
                asyncio.create_task(log_query(
                    f"{platform}_{clearance_level}", query, full,
                    prompt_tokens=meter.prompt_tokens, completion_tokens=meter.completion_tokens,
                    total_tokens=meter.total_tokens, token_source=meter.source, gated=False,
                    client=ACTIVE_CLIENT, actor=actor,
                ))


# ---------------------------------------------------------------------------
# Endpoints — SHAPES ARE FROZEN CONTRACTS (frontend + n8n depend on them)
# ---------------------------------------------------------------------------
@app.get("/")
def read_root():
    """Liveness. Deliberately PUBLIC (load balancers and uptime checks need it),
    and it reports the auth posture so an UNPROTECTED instance is visible at a
    glance rather than assumed safe. It exposes no key and no env-var name."""
    return {
        "status": "ok",
        "message": "Ejentic AI Enterprise RAG System — Core Intelligence Active.",
        "client": ACTIVE_CLIENT,
        "client_name": CFG.get("name", ACTIVE_CLIENT),
        "hybrid_search": HYBRID_ENABLED,
        "reranker": reranker.__class__.__name__ if reranker else None,
        "index": INDEX_NAME,
        "token_metering": True,
        "metrics_endpoint": "/metrics",
        "auth": authmod.status(CFG),
    }


@app.get("/whoami")
def whoami(x_api_key: str | None = Header(default=None, alias=authmod.API_KEY_HEADER),
           authorization: str | None = Header(default=None)):
    """What the presented key grants. Authenticated, and tells the caller only
    about ITSELF.

    This exists so a UI can DISPLAY the caller's clearance instead of offering a
    dropdown to choose it. The old dropdown was the vulnerability in miniature: it
    implied the browser decides its own clearance. Now the key decides, and the UI
    asks the server what that key is worth.

    It lists the roles this key could narrow TO, so a UI can offer a genuine
    narrowing control (an executive previewing the guest view) without ever being
    able to offer widening — that list is computed server-side from the key."""
    role = _authed_role(x_api_key, authorization)
    levels = CFG.get("clearance_levels") or {}
    can_narrow_to = sorted(
        r for r in levels if authmod._rank(r, CFG) <= authmod._rank(role, CFG)
    )
    return {
        "client": ACTIVE_CLIENT,
        "role": role,
        "clearance_tags": ("*" if levels.get(role) == "*"
                           else list(levels.get(role) or [])),
        "can_narrow_to": can_narrow_to,
        "auth_required": authmod.is_enabled(CFG),
        "is_admin": role == authmod.auth_config(CFG)["admin_role"],
    }


@app.get("/clients")
def clients(x_api_key: str | None = Header(default=None, alias=authmod.API_KEY_HEADER),
            authorization: str | None = Header(default=None)):
    """Admin surface: every registered client config (data, not code). Lets
    operators see which deployments exist and what each serves without digging
    through backend/clients/. READ ONLY — changing a client is editing JSON.

    Authenticated: this describes tenant topology (indexes, namespaces, models),
    which is reconnaissance material and not a liveness signal."""
    _authed_role(x_api_key, authorization)
    return {
        "active_client": ACTIVE_CLIENT,
        "client_count": len(registry.list_clients()),
        "clients": registry.list_clients(),
    }


@app.post("/chat")
async def chat(request: QueryRequest,
               x_api_key: str | None = Header(default=None, alias=authmod.API_KEY_HEADER),
               authorization: str | None = Header(default=None),
               x_upload_token: str | None = Header(default=None),
               x_actor: str | None = Header(default=None, alias=authmod.ACTOR_HEADER)):
    """Web UI endpoint — Server-Sent Events. Emits `data: {"chunk": "..."}\\n\\n`
    per token and a terminating `data: [DONE]\\n\\n`.

    Authentication happens BEFORE the StreamingResponse is created, so a 401/403
    is a normal JSON error the client can read — raising inside the generator
    would arrive as a 200 with an error event, which is far easier to miss."""
    if global_index is None:
        raise HTTPException(status_code=500, detail="Agent not initialized.")
    role = _authed_role(x_api_key, authorization)
    clearance = _clearance_for(role, request.clearance_level)
    # WHO, for the audit trail only. Resolved AFTER the key decided the clearance
    # above, so it cannot influence it.
    actor = authmod.clean_actor(x_actor)
    try:
        resolve_request_client(request.client)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    async def event_generator():
        try:
            async for piece in answer_stream(
                request.query, clearance, request.platform,
                upload_token=x_upload_token or "", actor=actor,
            ):
                yield f"data: {json.dumps({'chunk': piece})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "X-Clearance": clearance},
    )


@app.post("/api/rag")
async def n8n_rag_endpoint(request: QueryRequest,
                           x_api_key: str | None = Header(default=None, alias=authmod.API_KEY_HEADER),
                           authorization: str | None = Header(default=None),
                           x_upload_token: str | None = Header(default=None),
                           x_actor: str | None = Header(default=None, alias=authmod.ACTOR_HEADER)):
    """n8n orchestration endpoint — strict JSON in, strict JSON out.
    Returns `{"status": "success", "response": "..."}`.

    Token counts are exposed via X-* response HEADERS only, so the JSON body
    stays byte-for-byte compatible with the frozen contract n8n depends on."""
    if global_index is None:
        raise HTTPException(status_code=500, detail="Agent not initialized.")
    role = _authed_role(x_api_key, authorization)
    clearance = _clearance_for(role, request.clearance_level)
    # WHO, for the audit trail only — resolved after the key decided clearance.
    actor = authmod.clean_actor(x_actor)
    try:
        resolve_request_client(request.client)
        response_text, meter, gated, saved = await answer_once(
            request.query, clearance, request.platform,
            upload_token=x_upload_token or "", actor=actor,
        )
        headers = {
            "X-Prompt-Tokens": str(meter.prompt_tokens),
            "X-Completion-Tokens": str(meter.completion_tokens),
            "X-Total-Tokens": str(meter.total_tokens),
            "X-Token-Source": meter.source,
            "X-Gated": "true" if gated else "false",
            "X-Saved-Tokens": str(saved),
            # Which clearance actually applied, after narrowing. Lets a caller see
            # that its request was honoured at a lower level than it asked for.
            "X-Clearance": clearance,
        }
        return JSONResponse(
            content={"status": "success", "response": response_text}, headers=headers
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/metrics")
async def metrics(x_api_key: str | None = Header(default=None, alias=authmod.API_KEY_HEADER),
                  authorization: str | None = Header(default=None)):
    """Token-accounting dashboard data: lifetime totals (prompt/completion/total
    tokens, queries answered vs. gated, estimated tokens saved by the confidence
    gate) plus the most recent per-query rows. Pure DB read — costs no tokens.

    Authenticated: `recent` contains real user QUERY TEXT, so this is a privacy
    surface, not a public dashboard. Scoped to this tenant's rows only."""
    _authed_role(x_api_key, authorization)
    data = await get_token_metrics(limit=20, client=ACTIVE_CLIENT)
    data["config"] = {
        "client": ACTIVE_CLIENT,
        "llm_model": LLM_MODEL,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "retrieve_top_k": RETRIEVE_TOP_K,
        "rerank_top_n": RERANK_TOP_N,
        "query_rewrite_enabled": ENABLE_QUERY_REWRITE,
    }
    return data


@app.post("/ingest")
async def trigger_ingestion(
    request: IngestRequest | None = None,
    x_api_key: str | None = Header(default=None, alias=authmod.API_KEY_HEADER),
    authorization: str | None = Header(default=None),
):
    """Re-run ingestion. ADMIN ONLY, and APPEND by default.

    This endpoint used to be unauthenticated AND always ran a clean rebuild —
    `ingest_knowledge.py` with no --append calls reset_namespace(), which is
    `delete(delete_all=True)`. One anonymous POST therefore wiped the client's
    entire knowledge base. Two changes: only the admin role may call it, and
    destroying data now requires asking for it explicitly with {"rebuild": true}.
    """
    import subprocess

    role = _authed_role(x_api_key, authorization)
    try:
        authmod.require_admin(role, CFG)
    except authmod.AuthError as e:
        raise authmod.as_http(e)

    req = request or IngestRequest()
    # Canonical, clearance-aware ingestion (see ingest_knowledge.py / RUNBOOK.md).
    cmd = [sys.executable, "ingest_knowledge.py"]
    if not req.rebuild:
        cmd.append("--append")
    if req.file:
        # Guard the shell-adjacent argument: a knowledge file must be a plain
        # filename inside backend/, never a path that walks out of it.
        safe = os.path.basename(req.file.strip())
        if not safe or not safe.endswith(".json"):
            raise HTTPException(status_code=400,
                                detail="file must be a .json filename in backend/")
        cmd += ["--file", safe]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        return {
            "status": "success",
            "message": ("Rebuilt index (namespace cleared first)." if req.rebuild
                        else "Ingestion completed (appended; nothing deleted)."),
            "rebuild": req.rebuild,
            "logs": result.stdout,
        }
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e.stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# PDF extraction (page-by-page, quality-filtered)
# ---------------------------------------------------------------------------
# SimpleDirectoryReader's default extractor map is EMPTY when the optional
# `llama-index-readers-file` package isn't installed — and with no registered
# .pdf reader it indexes PDFs as RAW BINARY TEXT (bytes decoded with errors),
# producing mojibake chunks that look indexed but answer nothing. pypdf
# extracts the same files cleanly, so we extract page-by-page ourselves,
# attach page metadata, and skip any page that decodes to garbage.
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

_PAGE_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "was", "were", "are",
    "have", "has", "had", "not", "but", "all", "can", "will", "one", "two",
    "contact", "experience", "education", "skills", "project", "leadership",
    "resume", "work", "team", "data", "manage", "develop", "design", "years",
}


def _page_is_readable(text: str) -> bool:
    """Heuristic: does a decoded page look like human text, not raw bytes?"""
    if len(text.strip()) < 30:
        return False
    ctrl = sum(1 for c in text if not c.isprintable() and c not in "\n\t\r")
    if ctrl / len(text) > 0.05:
        return False
    tokens = re.findall(r"[a-zA-Z]+", text)
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t.lower() in _PAGE_WORDS)
    return hits / len(tokens) >= 0.10


def _extract_pdf_documents(path, filename):
    """Extract a PDF page-by-page into LlamaIndex Documents with metadata.

    Returns (documents, skipped_pages). Pages that decode to garbage are
    skipped — designer PDFs often mix readable and binary-encoded pages.
    """
    from llama_index.core.schema import Document

    docs, skipped = [], 0
    reader = PdfReader(str(path))
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            skipped += 1
            continue
        if _page_is_readable(text):
            docs.append(
                Document(
                    text=text,
                    metadata={"file_name": filename, "page": i + 1},
                )
            )
        else:
            skipped += 1
    return docs, skipped


def _load_upload_documents(path, filename):
    """Read one stored upload into Documents, preferring the pypdf path."""
    if str(filename).lower().endswith(".pdf") and PdfReader is not None:
        return _extract_pdf_documents(path, filename)
    return SimpleDirectoryReader(input_files=[str(path)]).load_data(), 0


# Uploads: what we accept, and how big. A generated name + an extension allow-list
# is what keeps a hostile filename from choosing where the file lands.
ALLOWED_UPLOAD_EXT = {".pdf", ".txt", ".md"}
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))


def _safe_upload_path(upload_dir: str, original_name: str) -> str:
    """Where to store an upload, chosen by US rather than by the uploader.

    THE BUG THIS FIXES: the old code did os.path.join(upload_dir, file.filename)
    with no sanitisation, so a filename of "../../../../tmp/pwned.pdf" resolved
    outside the upload directory — an arbitrary file write from an unauthenticated
    POST. Two independent defences, because one is never enough here:
      1. the stored name is generated (random hex + a validated extension), so no
         caller-supplied character reaches the filesystem at all, and
      2. we still assert the resolved path stays inside upload_dir, which catches
         anything a future refactor might reintroduce.
    """
    ext = os.path.splitext(original_name or "")[1].lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file type {ext or '(none)'}; allowed: "
                   f"{', '.join(sorted(ALLOWED_UPLOAD_EXT))}",
        )
    root = os.path.realpath(upload_dir)
    path = os.path.realpath(os.path.join(root, f"{secrets.token_hex(16)}{ext}"))
    if os.path.commonpath([root, path]) != root:
        raise HTTPException(status_code=400, detail="invalid upload path")
    return path


@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...),
                     x_api_key: str | None = Header(default=None, alias=authmod.API_KEY_HEADER),
                     authorization: str | None = Header(default=None)):
    """Ingest a one-off document into an in-memory index PRIVATE to the caller.

    Returns an `upload_token`; send it back as the `X-Upload-Token` header on
    /chat or /api/rag to have that document folded into the retrieve->rerank
    pipeline. It is not clearance-filtered — it is the caller's own file — which
    is precisely why it is token-scoped rather than global: the old code kept ONE
    process-wide index and mixed it into every caller's queries.
    """
    _authed_role(x_api_key, authorization)

    upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    file_path = _safe_upload_path(upload_dir, file.filename or "")

    # Stream to disk with a hard size cap so one request can't fill the volume.
    size = 0
    try:
        with open(file_path, "wb") as buffer:
            while True:
                block = await file.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"file exceeds the {MAX_UPLOAD_BYTES // (1024*1024)}MB limit",
                    )
                buffer.write(block)
    except HTTPException:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise

    try:
        print(f"Parsing upload: {file.filename!r} -> {os.path.basename(file_path)}")
        documents, skipped = _load_upload_documents(file_path, file.filename or "")
        if skipped:
            print(f"[upload] skipped {skipped} unreadable page(s)")
    except HTTPException:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise
    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(
            status_code=400,
            detail=f"Could not read this file -- it may be corrupt, or not really a "
                   f"{os.path.splitext(file_path)[1] or 'supported'} file: {_short(e)}",
        )

    extracted = sum(len((d.text or "").strip()) for d in documents)
    if extracted < 50:
        # Previously this failed SILENTLY: the 200 response masked a no-op
        # index, so the user believed their doc was queryable when it wasn't.
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not extract readable text from {file.filename} "
                f"({extracted} chars, {skipped} unreadable pages). It may be "
                "scanned/image-only or password-protected — the knowledge "
                "base cannot index it."
            ),
        )

    token = _remember_upload(VectorStoreIndex.from_documents(documents),
                             file.filename or "", file_path)
    print(f"[upload] {file.filename}: {len(documents)} pages, {extracted} chars indexed")
    return {
        "status": "success",
        "message": (
            f"Ingested {file.filename} ({len(documents)} pages, "
            f"{extracted} chars). It is now queryable."
        ),
        "upload_token": token,
        # Echo the ORIGINAL name for display only. The bytes live under the
        # generated name from _safe_upload_path; a client that treated this as
        # a path would be trusting caller-supplied text, which is the bug.
        "filename": file.filename or "",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
