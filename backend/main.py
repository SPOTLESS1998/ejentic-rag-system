"""
Ejentic AI Enterprise RAG System — FastAPI "Brain".

Implements the 3-Step Production Standard as a linear, controllable pipeline
(not a black-box agent) so every stage is observable and token-bounded:

  Step 1 — Ingestion (see ingest_mock_data.py / scrape_and_ingest.py):
           semantic chunking + nvidia/nv-embedqa-e5-v5 + Pinecone upsert with a
           `clearance` metadata tag on every chunk (multi-tenancy).

  Step 2 — Advanced Retrieval (this file):
           query rewriting  ->  hybrid search (dense+sparse, auto-degrades to
           dense on a cosine index)  ->  reranking (cross-encoder when a rerank
           model is reachable, else a dependency-free BM25 lexical reranker)  ->
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
"""

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import json
import math
import re
import shutil
import os
import sys
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

from database import init_db, log_query, get_token_metrics
from token_meter import TokenMeter, estimate_tokens
import client_registry as registry

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


async def _retry_achat(messages, *, attempts: int = 3, meter=None,
                       fallback_prompt_text: str = ""):
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
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            # astream_chat returns a coroutine -> await to get the async iterable
            stream = await Settings.llm.astream_chat(messages)
            async for chunk in stream:
                yield chunk
            return
        except Exception as e:
            last_err = e
            if attempt < attempts and _is_transient(e):
                wait = 2 * attempt
                print(f"[llm] astream attempt {attempt} transient ({_short(e)}); retrying in {wait}s...")
                await asyncio.sleep(wait)
            else:
                raise
    raise last_err

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
RERANK_MODEL = os.environ.get("RERANK_MODEL") or CFG["rerank_model"]

RETRIEVE_TOP_K = int(os.environ.get("RETRIEVE_TOP_K") or CFG["retrieve_top_k"])
RERANK_TOP_N = int(os.environ.get("RERANK_TOP_N") or CFG["rerank_top_n"])
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD") or CFG["confidence_threshold"])
MAX_CONTEXT_CHARS = int(os.environ.get("MAX_CONTEXT_CHARS") or CFG["max_context_chars"])
HYBRID_ALPHA = float(os.environ.get("HYBRID_ALPHA") or CFG["hybrid_alpha"])
RERANK_ALPHA = float(os.environ.get("RERANK_ALPHA") or CFG["rerank_alpha"])
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
    "a list down to a subset or drop items for brevity. If the retrieved context does "
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
pdf_index = None  # in-memory index for a user-uploaded PDF (see /upload)


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

    Priority: NVIDIA NIM cross-encoder (probed) -> local sentence-transformers
    cross-encoder (probed, only if the package is installed) -> dependency-free
    BM25 lexical reranker (always available, $0/query). The lexical tier is the
    guaranteed floor, so retrieval is always reranked by *something* real."""
    # 1) NVIDIA NIM reranker — verify with a REAL call. Hosted rerank endpoints
    #    can be retired (we've observed 404/410), and construction alone won't
    #    reveal that, so probe before trusting it.
    try:
        from llama_index.postprocessor.nvidia_rerank import NVIDIARerank
        rr = NVIDIARerank(model=RERANK_MODEL, api_key=NVIDIA_API_KEY, top_n=RERANK_TOP_N)
        _probe_reranker(rr)
        print(f"[rerank] NVIDIA cross-encoder active (probed OK): {RERANK_MODEL}")
        return rr
    except Exception as e:
        print(f"[rerank] NVIDIA reranker unavailable ({_short(e)}); trying local cross-encoder.")

    # 2) Local sentence-transformers cross-encoder — only if the package is
    #    installed. Probe it too, in case the model can't load.
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

    # 3) Dependency-free lexical (BM25) reranker — no deps, no tokens, no API
    #    calls; always available, so retrieval is never left unranked.
    print(f"[rerank] BM25 lexical reranker active (no deps, no tokens; alpha={RERANK_ALPHA}).")
    return LexicalReranker(top_n=RERANK_TOP_N, alpha=RERANK_ALPHA)


if not PINECONE_API_KEY or not NVIDIA_API_KEY:
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


app = FastAPI(title="Ejentic AI Enterprise RAG System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    await init_db()
    print("Audit Database Initialized.")


class QueryRequest(BaseModel):
    query: str
    clearance_level: str = "guest"
    platform: str = "WEB_UI"
    client: str = ""  # optional; defaults to this instance's active client


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
    level = (clearance_level or "").strip().lower()
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
        resp = await _retry_achat(messages, meter=meter)
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
async def retrieve_and_rerank(query: str, clearance_level: str, meter=None):
    """Returns (nodes, max_raw_score, rewritten_query). `nodes` is the reranked,
    length-capped top-N ready for grounded synthesis."""
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

    # Fold in a user-uploaded PDF (if any) so it competes in the same rerank.
    # The upload is the caller's own document, so it isn't clearance-filtered.
    if pdf_index is not None:
        try:
            pdf_retriever = pdf_index.as_retriever(similarity_top_k=3)
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
    """When the confidence gate skips synthesis, estimate the INPUT tokens we
    avoided by not sending the grounded prompt. A conservative floor: it counts
    the prompt we didn't send, not the answer we didn't generate."""
    try:
        return estimate_tokens(_messages_text(_build_grounded_messages(question, nodes)))
    except Exception:
        return 0


def _passes_confidence(nodes, max_raw_score: float) -> bool:
    """Step 3 guardrail: refuse to answer (and skip the LLM entirely) when
    retrieval is empty or the best match is below the trust threshold."""
    return bool(nodes) and max_raw_score >= CONFIDENCE_THRESHOLD


def _strip_assistant_prefix(text: str) -> str:
    # Some LlamaIndex/LLM paths echo a leading "assistant:" role marker.
    stripped = text.lstrip()
    if stripped.lower().startswith("assistant:"):
        return stripped[len("assistant:"):].lstrip()
    return text


# ---------------------------------------------------------------------------
# Answer producers (shared by both endpoints)
# ---------------------------------------------------------------------------
async def answer_once(query: str, clearance_level: str, platform: str):
    """Non-streaming answer for /api/rag (n8n). Returns (text, meter, gated,
    saved_tokens) so the endpoint can surface token counts in headers without
    touching the frozen JSON body."""
    meter = TokenMeter()
    if global_index is None:
        return "System Offline: AI Core is booting or missing API keys.", meter, False, 0
    try:
        nodes, max_score, _ = await retrieve_and_rerank(query, clearance_level, meter=meter)
        if not _passes_confidence(nodes, max_score):
            saved = _estimate_gate_savings(query, nodes)
            asyncio.create_task(log_query(
                f"{platform}_{clearance_level}", query, "[GATE] " + ESCALATION_LINE,
                prompt_tokens=meter.prompt_tokens, completion_tokens=meter.completion_tokens,
                total_tokens=meter.total_tokens, estimated_saved_tokens=saved,
                token_source=meter.source if meter.total_tokens else "gate", gated=True,
            ))
            return ESCALATION_LINE, meter, True, saved

        messages = _build_grounded_messages(query, nodes)
        resp = await _retry_achat(messages, meter=meter)
        text = _strip_assistant_prefix((resp.message.content or "").strip())
        meter.record_response(
            resp, fallback_prompt_text=_messages_text(messages), fallback_completion_text=text
        )
        asyncio.create_task(log_query(
            f"{platform}_{clearance_level}", query, text,
            prompt_tokens=meter.prompt_tokens, completion_tokens=meter.completion_tokens,
            total_tokens=meter.total_tokens, token_source=meter.source, gated=False,
        ))
        return text, meter, False, 0
    except Exception as e:
        asyncio.create_task(log_query(f"{platform}_{clearance_level}", query, f"ERROR: {e}"))
        return f"I encountered a cognitive error while processing that request: {e}", meter, False, 0


async def answer_stream(query: str, clearance_level: str, platform: str):
    """Async token generator for /chat (Web UI). Yields raw text chunks; the
    endpoint wraps each into the SSE `data: {"chunk": ...}` envelope."""
    if global_index is None:
        yield "System Offline: AI Core is booting or missing API keys."
        return

    meter = TokenMeter()
    nodes, max_score, _ = await retrieve_and_rerank(query, clearance_level, meter=meter)
    if not _passes_confidence(nodes, max_score):
        saved = _estimate_gate_savings(query, nodes)
        asyncio.create_task(log_query(
            f"{platform}_{clearance_level}", query, "[GATE] " + ESCALATION_LINE,
            prompt_tokens=meter.prompt_tokens, completion_tokens=meter.completion_tokens,
            total_tokens=meter.total_tokens, estimated_saved_tokens=saved,
            token_source=meter.source if meter.total_tokens else "gate", gated=True,
        ))
        yield ESCALATION_LINE
        return

    messages = _build_grounded_messages(query, nodes)
    collected = []
    prefix_checked = False
    buffer = ""
    try:
        async for chunk in _retry_astream_chat(messages):
            delta = chunk.delta or ""
            if not delta:
                continue
            collected.append(delta)

            # Strip a leading "assistant:" that may arrive split across deltas:
            # buffer until we've seen enough to decide, then flush once.
            if not prefix_checked:
                buffer += delta
                if len(buffer) < len("assistant:") and not buffer.strip().lower().startswith("assistant"):
                    prefix_checked = True
                    yield buffer
                    buffer = ""
                    continue
                if len(buffer) >= len("assistant:"):
                    prefix_checked = True
                    cleaned = _strip_assistant_prefix(buffer)
                    if cleaned:
                        yield cleaned
                    buffer = ""
                continue

            yield delta

        if buffer:  # flush any residual held for prefix inspection
            cleaned = _strip_assistant_prefix(buffer)
            if cleaned:
                yield cleaned
    finally:
        full = _strip_assistant_prefix("".join(collected)).strip()
        if full:
            # Streaming rarely reports provider usage, so estimate: prompt from the
            # grounded messages we sent, completion from the accumulated answer.
            meter.record_estimate(_messages_text(messages), full)
            asyncio.create_task(log_query(
                f"{platform}_{clearance_level}", query, full,
                prompt_tokens=meter.prompt_tokens, completion_tokens=meter.completion_tokens,
                total_tokens=meter.total_tokens, token_source=meter.source, gated=False,
            ))


# ---------------------------------------------------------------------------
# Endpoints — SHAPES ARE FROZEN CONTRACTS (frontend + n8n depend on them)
# ---------------------------------------------------------------------------
@app.get("/")
def read_root():
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
    }


@app.get("/clients")
def clients():
    """Admin surface: every registered client config (data, not code). Lets
    operators see which deployments exist and what each serves without digging
    through backend/clients/. READ ONLY — changing a client is editing JSON."""
    return {
        "active_client": ACTIVE_CLIENT,
        "client_count": len(registry.list_clients()),
        "clients": registry.list_clients(),
    }


@app.post("/chat")
async def chat(request: QueryRequest):
    """Web UI endpoint — Server-Sent Events. Emits `data: {"chunk": "..."}\\n\\n`
    per token and a terminating `data: [DONE]\\n\\n`."""
    if global_index is None:
        raise HTTPException(status_code=500, detail="Agent not initialized.")
    try:
        resolve_request_client(request.client)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    async def event_generator():
        try:
            async for piece in answer_stream(
                request.query, request.clearance_level, request.platform
            ):
                yield f"data: {json.dumps({'chunk': piece})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/rag")
async def n8n_rag_endpoint(request: QueryRequest):
    """n8n orchestration endpoint — strict JSON in, strict JSON out.
    Returns `{"status": "success", "response": "..."}`.

    Token counts are exposed via X-* response HEADERS only, so the JSON body
    stays byte-for-byte compatible with the frozen contract n8n depends on."""
    if global_index is None:
        raise HTTPException(status_code=500, detail="Agent not initialized.")
    try:
        resolve_request_client(request.client)
        response_text, meter, gated, saved = await answer_once(
            request.query, request.clearance_level, request.platform
        )
        headers = {
            "X-Prompt-Tokens": str(meter.prompt_tokens),
            "X-Completion-Tokens": str(meter.completion_tokens),
            "X-Total-Tokens": str(meter.total_tokens),
            "X-Token-Source": meter.source,
            "X-Gated": "true" if gated else "false",
            "X-Saved-Tokens": str(saved),
        }
        return JSONResponse(
            content={"status": "success", "response": response_text}, headers=headers
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/metrics")
async def metrics():
    """Token-accounting dashboard data: lifetime totals (prompt/completion/total
    tokens, queries answered vs. gated, estimated tokens saved by the confidence
    gate) plus the most recent per-query rows. Pure DB read — costs no tokens."""
    data = await get_token_metrics(limit=20)
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
async def trigger_ingestion():
    import subprocess
    try:
        # Canonical, clearance-aware ingestion (see ingest_knowledge.py / RUNBOOK.md).
        # NOT scrape_and_ingest.py, which ingests untagged docs and makes the KB
        # invisible to every non-executive role.
        result = subprocess.run(
            [sys.executable, "ingest_knowledge.py"],
            capture_output=True, text=True, check=True,
        )
        return {"status": "success", "message": "Ingestion completed successfully", "logs": result.stdout}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e.stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    """Ingest a one-off PDF into an in-memory index. Subsequent queries fold its
    chunks into the same retrieve->rerank pipeline (no clearance filter, since
    it's the caller's own document)."""
    global pdf_index

    upload_dir = "data/uploads"
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        print(f"Parsing uploaded PDF: {file.filename}")
        documents = SimpleDirectoryReader(input_files=[file_path]).load_data()
        pdf_index = VectorStoreIndex.from_documents(documents)  # in-memory
        return {"status": "success", "message": f"Successfully ingested {file.filename}."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
