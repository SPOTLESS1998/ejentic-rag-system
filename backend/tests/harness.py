"""Shared scaffolding for the RAG offline test suite.

Plain-script style, matching the lead-gen suite: no pytest, no network, no real
keys. Every file is runnable on its own (`venv/bin/python tests/test_auth.py`) and
`tests/run_all.py` runs the set.

WHY THE IMPORT DANCE BELOW
--------------------------
`main.py` builds a Pinecone client, an NVIDIA embedding model and a cross-encoder
at import time. None of that can run in a test, so importing main requires:

  * fake keys in the environment (the modules refuse to load without them),
  * a temp audit DB (or the tests write into the real one),
  * tolerance for the Pinecone connection failing — main already catches that and
    leaves `global_index = None`, which is exactly the state we want.

Tests that only need the pure logic (auth, clearance filters, the stream helpers)
get it via `import_main()`. Tests that need HTTP use `client()` for a TestClient
with the retrieval seams stubbed.
"""
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# --- The environment every test needs before importing backend modules -----
# Fake, obviously-not-real values. The modules only check presence at import.
os.environ.setdefault("PINECONE_API_KEY", "test-not-a-real-key")
os.environ.setdefault("NVIDIA_API_KEY", "test-not-a-real-key")
os.environ.setdefault("RAG_CLIENT", "ejentic")
# Keep the audit DB out of backend/data/ so a test run never touches real rows.
os.environ.setdefault("RAG_AUDIT_DB",
                      str(Path(tempfile.gettempdir()) / "rag_test_audit.db"))
# Don't download a cross-encoder in a test run; BM25 is the documented fallback.
os.environ.setdefault("RERANK_LOCAL", "false")
os.environ.setdefault("RERANK_TRY_NVIDIA", "false")
# THE flag that makes this suite genuinely offline: main.py skips building the
# NVIDIA clients and the Pinecone connection. Without it, importing main makes
# real network calls (and a 401 against Pinecone) before any test runs.
os.environ["RAG_OFFLINE"] = "1"

PASS = 0
FAIL = 0
FAILED_NAMES: list[str] = []


def check(name: str, cond, extra: str = "") -> bool:
    """Record one assertion. Returns the condition so callers can branch."""
    global PASS, FAIL
    ok = bool(cond)
    suffix = f"  ({extra})" if extra else ""
    if ok:
        PASS += 1
        print(f"  ✅ {name}{suffix}")
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"  ❌ {name}{suffix}")
    return ok


def section(title: str) -> None:
    print(f"\n[{title}]")


def report(suite: str) -> int:
    """Print the standard footer and return the process exit code."""
    print(f"\n{'=' * 60}")
    print(f"  {suite}: RESULT: {PASS} passed, {FAIL} failed")
    if FAILED_NAMES:
        for n in FAILED_NAMES:
            print(f"    failed: {n}")
    print(f"{'=' * 60}")
    return 1 if FAIL else 0


def finish(suite: str) -> None:
    sys.exit(report(suite))


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def base_cfg(**overrides) -> dict:
    """A merged client config, cheap to build and safe to mutate per test."""
    import client_registry as registry
    cfg = dict(registry.get_client("ejentic"))
    cfg["clearance_levels"] = dict(cfg["clearance_levels"])
    cfg["auth"] = {k: (dict(v) if isinstance(v, dict) else v)
                   for k, v in (cfg.get("auth") or {}).items()}
    cfg.update(overrides)
    # Mirror the one merge rule that a plain dict.update() cannot express:
    # client_registry._merge drops an inherited `clearance_tags` when a tenant
    # declares its own `clearance_levels`, because whoever owns the role map
    # owns the vocabulary. Without this, a fixture that overrides the role map
    # keeps ejentic's tags and every tag-vocabulary test measures a config that
    # could never exist in production. _merge remains the source of truth; this
    # only keeps the fixture honest about it.
    if "clearance_levels" in overrides and "clearance_tags" not in overrides:
        cfg.pop("clearance_tags", None)
    return cfg


def with_auth(required=True, keys=None, admin_role="executive", **overrides) -> dict:
    """A config with a specific auth block, without touching any file on disk."""
    cfg = base_cfg(**overrides)
    cfg["auth"] = {
        "required": required,
        "keys": dict(keys if keys is not None else {
            "guest": "RAG_KEY_GUEST",
            "employee": "RAG_KEY_EMPLOYEE",
            "executive": "RAG_KEY_EXECUTIVE",
        }),
        "admin_role": admin_role,
    }
    return cfg


class env:
    """Context manager that sets/removes env vars and restores them after.

    Used for the key env vars so one test's keys can't leak into another's.
    """

    def __init__(self, **values):
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, old in self.saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
        return False


# ---------------------------------------------------------------------------
# Importing the app
# ---------------------------------------------------------------------------
_main = None


def import_main():
    """Import backend/main.py once, tolerating the dead Pinecone connection.

    main.py catches its own init failure and leaves global_index None, so the
    import succeeds offline. Anything that genuinely needs a live index is not
    tested here — that's what the live script is for.
    """
    global _main
    if _main is None:
        import main as _m
        _main = _m
    return _main


def test_client(**stubs):
    """A FastAPI TestClient with the retrieval/LLM seams replaced.

    `stubs` are attribute names on main to monkeypatch (e.g. answer_once=...).
    The point is to exercise the ENDPOINT layer — auth, status codes, headers —
    without a vector store or an LLM. Restore is the caller's job via the
    returned `undo()`.
    """
    from fastapi.testclient import TestClient
    m = import_main()

    saved = {}
    for name, value in stubs.items():
        saved[name] = getattr(m, name, None)
        setattr(m, name, value)

    # A truthy index so endpoints don't 500 with "Agent not initialized".
    if "global_index" not in stubs:
        saved.setdefault("global_index", m.global_index)
        m.global_index = object()

    def undo():
        for name, old in saved.items():
            setattr(m, name, old)

    return TestClient(m.app), m, undo
