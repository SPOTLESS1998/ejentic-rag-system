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

from dotenv import load_dotenv

# Load backend/.env HERE, not in the caller. Validation below reads the auth key
# env vars, and any module that touches the registry at import time (database.py
# derives its per-tenant DB path that way) would otherwise validate against an
# environment that has not been populated yet -- an instance with perfectly good
# keys in .env would refuse to boot purely because of import order. Anchored to
# this file's directory so the result does not depend on the process's cwd.
load_dotenv(Path(__file__).resolve().parent / ".env")

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
    # --- Authentication: role -> the NAME of the env var holding that role's key.
    # Never a key value (MULTITENANCY.md). `required: false` is a LOCAL-DEV
    # default; a deployment that serves anyone must set it true, and validation
    # below then refuses to boot unless the keys actually exist. See auth.py.
    "auth": {
        "required": False,
        "keys": {
            "guest": "RAG_KEY_GUEST",
            "employee": "RAG_KEY_EMPLOYEE",
            "executive": "RAG_KEY_EXECUTIVE",
        },
        "admin_role": "executive",
    },
}

REQUIRED_KEYS = {
    "index_name", "namespace", "embed_model", "embed_dim", "llm_model",
    "rerank_model", "retrieve_top_k", "rerank_top_n", "confidence_threshold",
    "max_context_chars", "hybrid_alpha", "rerank_alpha",
    "query_rewrite_enabled", "chunk_size", "chunk_overlap",
    "clearance_levels", "persona_name", "persona_style", "escalation_line",
    "auth",
}

# Keys whose value is a nested object and must be MERGED per-key rather than
# replaced wholesale — otherwise a client that overrides one sub-key silently
# drops the rest of the defaults.
_NESTED_KEYS = ("clearance_levels", "auth")

_loaded: dict[str, dict[str, Any]] | None = None


def _fatal(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _merge(base: dict, override: dict) -> dict:
    """Overlay a client's JSON onto the defaults.

    Flat keys are replaced. The two nested keys need care:

    * `clearance_levels` is REPLACED wholesale when the client declares it, not
      merged. Merging was a real hazard: a tenant declaring only
      {"partner": ["partner_docs"]} would silently INHERIT our default
      `"executive": "*"` — an unrestricted role they never asked for and cannot
      remove. A tenant's role map must be exactly what they wrote.
    * `auth` merges at its top level (so a client can flip `required` without
      restating every key name) but its `keys` map is likewise REPLACED when
      given, for the same reason: inherited roles are roles nobody chose.
    """
    out = dict(base)
    out.update({k: v for k, v in override.items() if k not in _NESTED_KEYS})

    if isinstance(override.get("clearance_levels"), dict) and override["clearance_levels"]:
        out["clearance_levels"] = {
            role: (list(tags) if isinstance(tags, list) else tags)
            for role, tags in override["clearance_levels"].items()
        }
        # `clearance_tags` follows `clearance_levels` for the SAME reason that
        # map is replaced rather than merged: a tenant declaring its own roles
        # would otherwise inherit OUR tag vocabulary (public/internal/executive)
        # — a vocabulary they never asked for. That inherited list then either
        # validates their content against the wrong tags or, more likely, trips
        # the completeness check and refuses to boot with a message about tags
        # they never wrote. Whoever owns the role map owns the vocabulary.
        if "clearance_tags" not in override:
            out.pop("clearance_tags", None)
    else:
        out["clearance_levels"] = {
            role: (list(tags) if isinstance(tags, list) else tags)
            for role, tags in base.get("clearance_levels", {}).items()
        }

    merged_auth = dict(base.get("auth") or {})
    over_auth = override.get("auth")
    if isinstance(over_auth, dict):
        merged_auth.update({k: v for k, v in over_auth.items() if k != "keys"})
        if isinstance(over_auth.get("keys"), dict):
            merged_auth["keys"] = dict(over_auth["keys"])
    merged_auth["keys"] = dict(merged_auth.get("keys") or {})
    out["auth"] = merged_auth
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

    # `clearance_tags` is OPTIONAL: absent means "derive the vocabulary from the
    # role map" (the original behaviour, kept for tenants that have no wildcard
    # role). When declared it becomes the vocabulary ingestion validates against,
    # so a malformed declaration must fail HERE — at config load, before any
    # ingestion can act on it — not halfway through a namespace rebuild.
    declared = cfg.get("clearance_tags")
    if declared is not None:
        if not isinstance(declared, list) or not declared:
            _fatal(f"{source}: 'clearance_tags' must be a non-empty list of tag names")
        clean = set()
        for tag in declared:
            if not isinstance(tag, str) or not tag.strip():
                _fatal(f"{source}: 'clearance_tags' contains a non-string/empty tag: {tag!r}")
            clean.add(tag.strip())
        # A declared vocabulary that omits a tag some role is granted would reject
        # content that role is entitled to read — a silent hole in the tenant's
        # knowledge base. Cheap to check, impossible to debug later.
        named = {
            t.strip() for tags in cl.values() if isinstance(tags, list)
            for t in tags if isinstance(t, str) and t.strip()
        }
        gap = sorted(named - clean)
        if gap:
            _fatal(
                f"{source}: 'clearance_tags' omits {gap}, which clearance_levels "
                f"grants to a role — every tag a role can read must be declared"
            )
    for num in ("retrieve_top_k", "rerank_top_n", "max_context_chars",
                "embed_dim", "chunk_size", "chunk_overlap"):
        if not isinstance(cfg[num], int) or cfg[num] <= 0:
            _fatal(f"{source}: '{num}' must be a positive integer")
    for flt in ("confidence_threshold", "hybrid_alpha", "rerank_alpha"):
        if not isinstance(cfg[flt], (int, float)) or not (0.0 <= float(cfg[flt]) <= 1.0):
            _fatal(f"{source}: '{flt}' must be between 0 and 1")
    _validate_auth(cfg, source)


def _validate_auth(cfg: dict, source: str) -> None:
    """Auth must be coherent, and 'required' must actually be enforceable.

    THE POINT OF THIS FUNCTION: an instance that declares `required: true` but has
    no key values in its environment cannot authenticate anybody. Left unchecked
    that would degrade to "nobody can get in" at best and, with a bug, to open
    access at worst. Either way it is a configuration error, so we refuse to boot
    here — loudly, at load, before a single query is served.
    """
    auth = cfg.get("auth")
    if not isinstance(auth, dict):
        _fatal(f"{source}: 'auth' must be an object (see auth.py)")

    keys = auth.get("keys")
    if not isinstance(keys, dict):
        _fatal(f"{source}: auth.keys must be an object mapping role -> env var NAME")

    levels = cfg["clearance_levels"]
    for role, env_name in keys.items():
        if not isinstance(env_name, str) or not env_name.strip():
            _fatal(f"{source}: auth.keys[{role!r}] must be the NAME of an env var")
        # Guard against the mistake this pattern exists to prevent: a real secret
        # pasted where an env-var name belongs. Names are SHOUTY_SNAKE_CASE.
        if not env_name.strip().replace("_", "").isalnum() or env_name != env_name.upper():
            _fatal(
                f"{source}: auth.keys[{role!r}] = {env_name!r} does not look like an "
                f"env var NAME (expected e.g. 'RAG_KEY_{role.upper()}'). Never put a "
                f"key value in config — see MULTITENANCY.md."
            )
        if role not in levels:
            _fatal(
                f"{source}: auth.keys has role {role!r}, which is not in "
                f"clearance_levels ({', '.join(sorted(levels))}). A key that grants "
                f"a role the retriever doesn't know would fail closed on every query."
            )

    admin = auth.get("admin_role")
    if admin is not None and admin != "":
        if not isinstance(admin, str) or admin not in keys:
            _fatal(
                f"{source}: auth.admin_role {admin!r} must be one of the roles in "
                f"auth.keys ({', '.join(sorted(keys))}) — otherwise no caller could "
                f"ever perform administrative actions."
            )

    if auth.get("required"):
        if not keys:
            _fatal(
                f"{source}: auth.required is true but auth.keys is empty — no caller "
                f"could ever be authenticated. Declare role -> env-var names."
            )
        unset = [f"{role} ({env})" for role, env in sorted(keys.items())
                 if not (os.environ.get(env) or "").strip()]
        if len(unset) == len(keys):
            _fatal(
                f"{source}: auth.required is true but NONE of the key env vars are "
                f"set: {', '.join(unset)}. Generate one per role with "
                f"`openssl rand -hex 32` and add them to backend/.env. Refusing to "
                f"boot rather than serve an instance that cannot authenticate."
            )
        if unset:
            print(
                f"WARNING: {source}: auth is on but these roles have no key set, so "
                f"they cannot be used: {', '.join(unset)}",
                file=sys.stderr,
            )


def tenant_clearance_tags(cfg: dict) -> list:
    """Every stored `clearance` tag this tenant's content may carry.

    ONE SOURCE OF TRUTH for the tag vocabulary, shared by retrieval and ingestion.
    Ingestion used to hardcode {"public","internal","executive"}, so a tenant whose
    tiers are e.g. partner/legal could not ingest without editing Python — exactly
    the kind of business fact MULTITENANCY.md forbids in code.

    Two sources, in order:

    1. An explicit `clearance_tags` list on the tenant's config. PREFERRED, and
       `_validate` proves it covers every tag the role map names.
    2. Otherwise, derived from the explicit role -> tag lists in
       `clearance_levels`.

    WHY THE EXPLICIT FORM EXISTS (found 2026-09-11 by actually running ingestion
    against production): deriving the vocabulary CANNOT see a tag that only a
    wildcard role can read. Our own `clearance_levels` maps
    `"executive": "*"`, which `build_clearance_filter` turns into "no filter at
    all" — so the `executive` TAG is served correctly by retrieval while being
    named nowhere in the config. Derivation therefore returned
    ["public", "internal"], and ingesting the very corpus that was already
    deployed failed with "record #8 has invalid clearance 'executive'". The
    deployed index had been un-re-ingestable for five days and nothing surfaced
    it, because ingestion is rare and the fail-closed check only fires then.

    🧠 Generalisable: a wildcard is a convenient way to say "sees everything",
    but it destroys information — you can no longer enumerate what "everything"
    is. Anything that needs the full vocabulary must be told it explicitly.
    """
    declared = cfg.get("clearance_tags")
    if isinstance(declared, list) and declared:
        seen, out = set(), []
        for tag in declared:
            t = tag.strip() if isinstance(tag, str) else ""
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        if out:
            return out

    seen, out = set(), []
    for tags in (cfg.get("clearance_levels") or {}).values():
        if not isinstance(tags, list):
            continue  # "*" — sees all tags, defines none
        for tag in tags:
            t = (tag or "").strip() if isinstance(tag, str) else ""
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


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