"""
Per-client configuration registry for the Ejentic Enterprise RAG.

WHY THIS EXISTS
---------------
One shared RAG engine serves many Ejentic processes/clients (lead-gen,
customer-service, HR support, knowledge cores for external companies, ...).
Everything that makes the pipeline "theirs" lives in plain JSON under
`backend/clients/`, never in code:

  * which Pinecone index + namespace holds their data,
  * which NVIDIA models the pipeline uses (embed / LLM / rerank),
  * the retrieval tunables (top-k, top-n, confidence threshold, alphas, ...),
  * the persona the model speaks as (system prompt, escalation line),
  * the clearance-roles -> stored-clearance-tags mapping.

RUNNING & TAILORING
-------------------
* One engine instance == one ACTIVE client. Data isolation is by deployment:
  start the service with `RAG_CLIENT=<id>` (env) and it boots with that
  client's index, namespace, models and persona. A second client means a
  second instance with a different RAG_CLIENT. This is deliberate: no
  cross-tenant data path exists in a single process.
* Add a client by dropping `clients/<id>.json` (fields are partial; the
  defaults in DEFAULT_CONFIG fill the gaps). No Python changes required.
* Every knob can still be overridden per-process with the legacy env vars
  (LLM_MODEL, RETRIEVE_TOP_K, CONFIDENCE_THRESHOLD, ...) for quick tuning.

The API understands a `client` request field for forward-compatibility, but a
single instance only serves its own active client — requesting a different
client fails loudly (HTTP 409) rather than risking cross-tenant data.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

CLIENTS_DIR = Path(__file__).resolve().parent / "clients"

# The full schema + production-safe defaults. A client JSON only overrides the
# keys it cares about; everything else inherits these.
DEFAULT_CONFIG: dict[str, Any] = {
    "id": "ejentic",
    "name": "Ejentic AI Knowledge Core",
    "description": "Default client: Ejentic's own business knowledge base "
                   "(public marketing, internal handbook, executive board data).",
    "index_name": "ejentic-global",
    "namespace": "ejentic-internal",
    # --- Models (NVIDIA NIM) ---
    "embed_model": "nvidia/nemotron-3-embed-1b",
    "embed_dim": 2048,
    "llm_model": "mistralai/mistral-nemotron",
    "rerank_model": "nvidia/nv-rerankqa-mistral-4b-v3",  # EOL on NIM as of 2026-09; falls back to BM25
    # --- Retrieval tunables ---
    "retrieve_top_k": 10,          # wide net for recall
    "rerank_top_n": 4,             # tight set for precision/tokens
    "confidence_threshold": 0.20,  # min cosine score to trust retrieval
    "max_context_chars": 1600,     # per-source cap => bounded prompts
    "hybrid_alpha": 0.5,           # 1.0 = pure dense, 0.0 = pure sparse
    "rerank_alpha": 0.5,           # lexical rerank blend: 1.0 dense, 0.0 BM25
    "query_rewrite_enabled": True,
    # --- Ingestion ---
    "chunk_size": 500,
    "chunk_overlap": 50,
    # --- Multi-tenancy: request role -> stored clearance tag(s) ---
    # "*" means unrestricted. Anything else maps to an explicit list. A role
    # absent from this map is treated as the LEAST privileged level (fail-closed).
    "clearance_levels": {
        "guest": ["public"],
        "employee": ["public", "internal"],
        "executive": "*",
    },
    # --- Persona ---
    "persona_name": "Ejentic Customer Success Agent",
    "persona_style": "professional, precise, and premium in tone",
    "escalation_line": (
        "I need more context or I don't have that information on hand. "
        "Would you like me to escalate you to a human?"
    ),
}

REQUIRED_KEYS = {
    "index_name", "namespace", "embed_model", "embed_dim", "llm_model",
    "rerank_model", "retrieve_top_k", "rerank_top_n", "confidence_threshold",
    "max_context_chars", "hybrid_alpha", "rerank_alpha",
    "query_rewrite_enabled", "chunk_size", "chunk_overlap",
    "clearance_levels", "persona_name", "persona_style", "escalation_line",
}

_loaded: dict[str, dict[str, Any]] | None = None


def _fatal(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _merge(base: dict, override: dict) -> dict:
    """Deep-ish merge: clearance_levels is nested, everything else is flat."""
    out = dict(base)
    out.update({k: v for k, v in override.items() if k != "clearance_levels"})
    if isinstance(override.get("clearance_levels"), dict):
        merged_roles: dict[str, Any] = {
            role: (list(tags) if isinstance(tags, list) else tags)
            for role, tags in base.get("clearance_levels", {}).items()
        }
        for role, tags in override["clearance_levels"].items():
            merged_roles[role] = list(tags) if isinstance(tags, list) else tags
        out["clearance_levels"] = merged_roles
    return out


def _validate(cfg: dict, source: str) -> None:
    missing = REQUIRED_KEYS - set(cfg)
    if missing:
        _fatal(f"{source}: missing config keys: {sorted(missing)}")
    cl = cfg["clearance_levels"]
    if not isinstance(cl, dict) or not cl:
        _fatal(f"{source}: 'clearance_levels' must be a non-empty object")
    for role, tags in cl.items():
        if not isinstance(tags, (list, str)):
            _fatal(f"{source}: clearance_levels[{role!r}] must be a list or '*'")
    for num in ("retrieve_top_k", "rerank_top_n", "max_context_chars",
                "embed_dim", "chunk_size", "chunk_overlap"):
        if not isinstance(cfg[num], int) or cfg[num] <= 0:
            _fatal(f"{source}: '{num}' must be a positive integer")
    for flt in ("confidence_threshold", "hybrid_alpha", "rerank_alpha"):
        if not isinstance(cfg[flt], (int, float)) or not (0.0 <= float(cfg[flt]) <= 1.0):
            _fatal(f"{source}: '{flt}' must be between 0 and 1")


def _load_all() -> dict[str, dict[str, Any]]:
    """Load every `clients/*.json` once and validate it against the schema."""
    global _loaded
    if _loaded is not None:
        return _loaded

    if not CLIENTS_DIR.is_dir():
        _fatal(f"clients directory not found: {CLIENTS_DIR}")

    registry: dict[str, dict[str, Any]] = {}
    for path in sorted(CLIENTS_DIR.glob("*.json")):
        cid = path.stem
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as e:
            _fatal(f"{path.name} is not valid JSON: {e}")

        if not isinstance(raw, dict):
            _fatal(f"{path.name} must be a JSON object (client config)")

        file_id = raw.get("id") or cid
        if file_id != cid:
            _fatal(
                f"{path.name}: 'id' field ({file_id!r}) must match the filename "
                f"({cid!r}). Filename is the client's identity."
            )

        cfg = _merge(DEFAULT_CONFIG, raw)
        _validate(cfg, path.name)
        registry[cid] = cfg

    if not registry:
        _fatal("no client configs found under backend/clients/")

    _loaded = registry
    return registry


def active_client_id() -> str:
    """The instance-wide active client, from RAG_CLIENT env (default 'ejentic')."""
    cid = os.environ.get("RAG_CLIENT", "ejentic").strip()
    if cid not in _load_all():
        _fatal(
            f"RAG_CLIENT='{cid}' is not registered. Available: "
            f"{', '.join(sorted(_load_all()))} (see backend/clients/)"
        )
    return cid


def get_client(client_id: str | None = None) -> dict[str, Any]:
    """Full merged config for a client id (defaults to the active client)."""
    cid = client_id or active_client_id()
    registry = _load_all()
    if cid not in registry:
        raise KeyError(
            f"client '{cid}' not registered. Available: {', '.join(sorted(registry))}"
        )
    return registry[cid]


def list_clients() -> list[dict[str, Any]]:
    """Compact summaries for the GET /clients admin endpoint."""
    summaries = []
    for cid, cfg in _load_all().items():
        summaries.append({
            "id": cid,
            "name": cfg.get("name", cid),
            "description": cfg.get("description", ""),
            "index_name": cfg["index_name"],
            "namespace": cfg["namespace"],
            "llm_model": cfg["llm_model"],
            "embed_model": cfg["embed_model"],
            "clearance_roles": sorted(cfg["clearance_levels"]),
        })
    return summaries