"""Offline tests for the multi-tenancy rule (MULTITENANCY.md) applied to the RAG.

THE THREE FAILURES THIS PINS
----------------------------
1. ONE SHARED AUDIT DB. `DATABASE_URL` was a hardcoded `./ejentic_audit.db` and
   `audit_logs` had no client column, so two tenants on one host shared one audit
   trail — and GET /metrics reported their COMBINED totals, with one client's real
   query text visible in another client's dashboard.

2. A HARDCODED TAG VOCABULARY. `VALID_CLEARANCE = {"public","internal","executive"}`
   lived in ingest_knowledge.py, so a tenant whose tiers are partner/legal could
   not ingest without editing Python — a business fact in code.

3. UNTAGGED VECTORS. scrape_and_ingest.py wrote chunks with no `clearance` tag to a
   hardcoded namespace. Untagged chunks are invisible to every non-executive role,
   which is the exact failure ingest_knowledge.py's docstring warns about.

Run:  venv/bin/python tests/test_tenancy.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import base_cfg, check, finish, section  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
import client_registry as registry  # noqa: E402

# ---------------------------------------------------------------------------
section("the audit DB is per-tenant BY PATH")
# ---------------------------------------------------------------------------
# database.py resolves its path at import, so each tenant is checked in a
# subprocess with a different RAG_CLIENT.
import subprocess  # noqa: E402

PROBE = (
    "import os, database as d; "
    "print(d.ACTIVE_CLIENT + '|' + str(d.DB_PATH) + '|' + d.DATABASE_URL)"
)


def db_for(client_id, extra_env=None):
    env = {**os.environ, "RAG_CLIENT": client_id}
    env.pop("RAG_AUDIT_DB", None)          # let the default path logic run
    env.update(extra_env or {})
    out = subprocess.run(
        [str(BACKEND / "venv" / "bin" / "python"), "-c", PROBE],
        cwd=str(BACKEND), env=env, capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None, None, out.stderr.strip()[:200]
    active, path, url = out.stdout.strip().split("|")
    return active, path, url


active, path, url = db_for("ejentic")
check("the default client resolves an audit DB path", path is not None, str(url))
if path:
    check("the DB path is namespaced by client",
          os.sep + "ejentic" + os.sep in path, path)
    check("the DB lives under backend/data/", os.sep + "data" + os.sep in path, path)
    check("the DB is no longer the old shared ejentic_audit.db",
          not path.endswith("ejentic_audit.db"), path)
    check("the URL is the async sqlite driver", url.startswith("sqlite+aiosqlite:///"), url)

# Two tenants must not resolve to the same file. Built as a real temp client so
# this exercises the registry, not a mock.
tmp_client = "zz_test_tenant"
tmp_path = BACKEND / "clients" / f"{tmp_client}.json"
made = False
try:
    tmp_path.write_text(json.dumps({
        "id": tmp_client,
        "name": "Temp Test Tenant",
        "index_name": "zz-test-index",
        "namespace": "zz-test-ns",
        "clearance_levels": {"partner": ["partner_docs"], "principal": "*"},
        "auth": {"required": False,
                 "keys": {"partner": "RAG_KEY_PARTNER"},
                 "admin_role": "partner"},
    }, indent=2))
    made = True

    a_active, a_path, _ = db_for("ejentic")
    b_active, b_path, b_err = db_for(tmp_client)
    check("a second tenant loads from its own JSON", b_active == tmp_client, str(b_err))
    if b_path:
        check("two tenants resolve to DIFFERENT DB files", a_path != b_path,
              f"{a_path} vs {b_path}")
        check("the second tenant's path carries ITS id",
              os.sep + tmp_client + os.sep in b_path, b_path)

    # -----------------------------------------------------------------------
    section("a tenant's config is data, not code")
    # -----------------------------------------------------------------------
    cfg = registry.get_client(tmp_client)
    check("a brand-new tenant needs no Python change", cfg["id"] == tmp_client)
    check("unspecified keys inherit the defaults",
          cfg["retrieve_top_k"] == registry.DEFAULT_CONFIG["retrieve_top_k"])
    check("its own index name is used", cfg["index_name"] == "zz-test-index")

    # THE MERGE HAZARD: a tenant declaring only {"partner": [...]} must NOT inherit
    # the default `"executive": "*"` — an unrestricted role they never asked for and
    # could not remove.
    check("clearance_levels is REPLACED, not merged with the defaults",
          set(cfg["clearance_levels"]) == {"partner", "principal"},
          str(sorted(cfg["clearance_levels"])))
    check("no inherited 'executive' wildcard role",
          "executive" not in cfg["clearance_levels"])
    check("auth.keys is REPLACED, not merged",
          set(cfg["auth"]["keys"]) == {"partner"}, str(sorted(cfg["auth"]["keys"])))
    check("auth top-level keys still merge (required came from defaults)",
          "required" in cfg["auth"])

    check("the tag vocabulary is the tenant's own",
          registry.tenant_clearance_tags(cfg) == ["partner_docs"],
          str(registry.tenant_clearance_tags(cfg)))
finally:
    if made:
        tmp_path.unlink(missing_ok=True)
        registry._loaded = None       # force a reload so later tests see reality
        shutil.rmtree(BACKEND / "data" / tmp_client, ignore_errors=True)

# ---------------------------------------------------------------------------
section("a WILDCARD role does not hide its tags from the vocabulary")
# ---------------------------------------------------------------------------
# REGRESSION, found 2026-09-11 by running ingestion against production. Deriving
# the tag vocabulary from the role map cannot see a tag that only a wildcard role
# reads: our own map is {"executive": "*"}, which build_clearance_filter turns
# into "no filter", so the `executive` TAG is served by retrieval while appearing
# nowhere in the config. Derivation returned ["public","internal"], and ingesting
# the corpus that was ALREADY DEPLOYED failed with "record #8 has invalid
# clearance 'executive'". The index had been un-re-ingestable for five days.
# Fix: an explicit `clearance_tags` declaration, proven complete by _validate.
import copy  # noqa: E402

_ej = registry.get_client("ejentic")
check("ejentic declares its tag vocabulary explicitly",
      isinstance(_ej.get("clearance_tags"), list) and _ej["clearance_tags"])
check("the executive tag IS in the vocabulary (the bug)",
      "executive" in registry.tenant_clearance_tags(_ej),
      str(registry.tenant_clearance_tags(_ej)))
check("vocabulary is exactly the three declared tags",
      registry.tenant_clearance_tags(_ej) == ["public", "internal", "executive"],
      str(registry.tenant_clearance_tags(_ej)))

# Backward compatibility: a tenant with no declaration keeps deriving.
_legacy = copy.deepcopy(_ej)
_legacy.pop("clearance_tags", None)
check("no declaration still derives from the role map",
      registry.tenant_clearance_tags(_legacy) == ["public", "internal"],
      str(registry.tenant_clearance_tags(_legacy)))

# A declared vocabulary that omits a tag some role is GRANTED would silently
# reject content that role is entitled to read, so it must die at config load.
def _validates(c):
    try:
        registry._validate(c, "test")
        return True
    except SystemExit:
        return False

_gap = copy.deepcopy(_ej)
_gap["clearance_tags"] = ["public", "executive"]        # drops 'internal'
check("declaring a vocabulary that omits a granted tag is REFUSED",
      not _validates(_gap))
for _bad, _label in (([], "empty list"),
                     (["public", "internal", "executive", ""], "empty-string tag"),
                     ("public,internal", "a string instead of a list")):
    _c = copy.deepcopy(_ej)
    _c["clearance_tags"] = _bad
    check(f"clearance_tags as {_label} is REFUSED", not _validates(_c))
check("the real ejentic config still validates", _validates(copy.deepcopy(_ej)))
check("a typo'd tag is still outside the vocabulary (ingestion fails closed)",
      "exective" not in registry.tenant_clearance_tags(_ej))

# ---------------------------------------------------------------------------
section("every audit row is tenant-STAMPED, and every read filters on it")
# ---------------------------------------------------------------------------
# Belt and suspenders: the path keeps tenants apart on disk, and the column means
# a shared or migrated file still cannot mix their rows.
import asyncio  # noqa: E402

TMP_DB = Path(tempfile.mkdtemp(prefix="rag_tenancy_")) / "audit.db"
os.environ["RAG_AUDIT_DB"] = str(TMP_DB)

# database.py reads RAG_AUDIT_DB at import; make sure we get a fresh module.
for mod in ("database",):
    sys.modules.pop(mod, None)
import database as db  # noqa: E402

check("audit_logs has a client column", hasattr(db.AuditLog, "client"))
check("the client column is indexed (every read filters on it)",
      db.AuditLog.client.index is True)
check("the migration adds client to a pre-existing table",
      "client" in db._TOKEN_COLUMNS, str(sorted(db._TOKEN_COLUMNS)))


async def exercise():
    await db.init_db()
    await db.log_query("WEB_guest", "tenant A question", "A answer",
                       prompt_tokens=10, completion_tokens=5, total_tokens=15,
                       token_source="provider", client="tenant_a")
    await db.log_query("WEB_exec", "tenant B SECRET question", "B answer",
                       prompt_tokens=100, completion_tokens=50, total_tokens=150,
                       token_source="provider", client="tenant_b")
    await db.log_query("WEB_guest", "gated question", "[GATE] no",
                       estimated_saved_tokens=42, gated=True, client="tenant_a")
    return (await db.get_token_metrics(client="tenant_a"),
            await db.get_token_metrics(client="tenant_b"))


a, b = asyncio.run(exercise())

check("metrics report which tenant they belong to", a["client"] == "tenant_a")
check("tenant A sees only its own query count", a["totals"]["queries"] == 2,
      str(a["totals"]["queries"]))
check("tenant B sees only its own query count", b["totals"]["queries"] == 1,
      str(b["totals"]["queries"]))
check("token totals are NOT combined across tenants",
      a["totals"]["total_tokens"] == 15 and b["totals"]["total_tokens"] == 150,
      f"A={a['totals']['total_tokens']} B={b['totals']['total_tokens']}")
check("gated counts are per-tenant",
      a["totals"]["gated"] == 1 and b["totals"]["gated"] == 0)
check("saved-token totals are per-tenant",
      a["totals"]["estimated_saved_tokens"] == 42
      and b["totals"]["estimated_saved_tokens"] == 0)
check("avg tokens per answer uses only this tenant's answered rows",
      a["totals"]["avg_tokens_per_answer"] == 15.0,
      str(a["totals"]["avg_tokens_per_answer"]))

# THE PRIVACY LEAK: one client's query TEXT must never appear in another's dashboard.
a_text = json.dumps(a["recent"])
b_text = json.dumps(b["recent"])
check("tenant A's dashboard does not contain tenant B's query text",
      "SECRET" not in a_text, a_text[:120])
check("tenant B's dashboard does not contain tenant A's query text",
      "tenant A question" not in b_text, b_text[:120])
check("each tenant's recent list holds only its own rows",
      len(a["recent"]) == 2 and len(b["recent"]) == 1,
      f"A={len(a['recent'])} B={len(b['recent'])}")

# An unknown tenant must see nothing, not everything.
empty = asyncio.run(db.get_token_metrics(client="tenant_nonexistent"))
check("an unknown tenant sees zero rows, not all rows",
      empty["totals"]["queries"] == 0 and empty["recent"] == [])

shutil.rmtree(TMP_DB.parent, ignore_errors=True)

# ---------------------------------------------------------------------------
section("no house business fact is hardcoded in the pipeline")
# ---------------------------------------------------------------------------
SOURCES = ["main.py", "ingest_knowledge.py", "database.py", "auth.py",
           "client_registry.py", "token_meter.py"]


def source_of(name):
    return (BACKEND / name).read_text()


def code_of(name):
    """Source with comments and docstrings stripped, so these checks judge CODE.

    A naive substring search over the raw file flags the comments that explain what
    was removed and why — e.g. "this used to be a hardcoded {public, internal,
    executive}". Those comments are the most valuable lines in the file: they stop
    someone re-adding the bug. So the checks below tokenize and drop them.
    """
    import io
    import tokenize

    src = source_of(name)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError:
        return src          # unparsable: fall back to the raw text

    # Blank out comments and triple-quoted blocks IN PLACE, keeping every other
    # character at its original position. Joining tokens instead would split
    # dotted names like `registry.tenant_clearance_tags` across lines and make
    # substring checks silently useless.
    lines = src.splitlines(keepends=True)

    def blank(tok):
        srow, scol = tok.start
        erow, ecol = tok.end
        for row in range(srow, erow + 1):
            line = lines[row - 1]
            a = scol if row == srow else 0
            b = ecol if row == erow else len(line)
            keep_nl = line[b:] if row == erow else "\n"
            lines[row - 1] = line[:a] + " " * (b - a) + keep_nl

    for tok in toks:
        if tok.type == tokenize.COMMENT:
            blank(tok)
        elif tok.type == tokenize.STRING and tok.string[:3] in ('"""', "'''") \
                and tok.line.lstrip().startswith(tok.string[:3]):
            blank(tok)      # a docstring / standalone triple-quoted block
    return "".join(lines)


# The tag vocabulary must come from config. A literal set of tag names in code is
# the specific regression: it silently limits which tenants can ingest at all.
ing = source_of("ingest_knowledge.py")
ing_code = code_of("ingest_knowledge.py")
check("ingest derives VALID_CLEARANCE from the registry",
      "registry.tenant_clearance_tags" in ing_code)
check("ingest no longer hardcodes the tag set in CODE",
      not ('"public"' in ing_code and '"internal"' in ing_code
           and '"executive"' in ing_code),
      "the explanatory comment naming the old set is fine and deliberate")

# Ejentic's own index/namespace may appear in DEFAULT_CONFIG (they ARE the default
# client's values) but must not be hardcoded into the pipeline modules.
for name in ("main.py", "ingest_knowledge.py", "database.py"):
    src = code_of(name)
    for literal in ("ejentic-global", "ejentic-internal"):
        check(f"{name} does not hardcode {literal!r}", literal not in src)

check("database.py does not build a path from a hardcoded DB filename",
      "ejentic_audit.db" not in code_of("database.py"),
      "the docstring may name the old file to explain what changed")
check("database.py resolves its path from the active client",
      "ACTIVE_CLIENT" in code_of("database.py"))

# Auth config must name env VARS, never hold key values.
auth_src = source_of("auth.py")
check("auth.py reads keys from the environment", "os.environ.get(env_name)" in auth_src)
cfg_json = (BACKEND / "clients" / "ejentic.json").read_text()
check("the client config holds env-var NAMES, not key values",
      "RAG_KEY_GUEST" in cfg_json)
for leak in ("sk-", "pcsk_", "nvapi-"):
    check(f"the client config contains no {leak!r} secret", leak not in cfg_json)

# ---------------------------------------------------------------------------
section("the untagged ingestion script is gone")
# ---------------------------------------------------------------------------
check("scrape_and_ingest.py no longer exists",
      not (BACKEND / "scrape_and_ingest.py").exists(),
      "it wrote chunks with NO clearance tag, invisible to every non-executive role")
stale_refs = [s for s in SOURCES if "scrape_and_ingest" in source_of(s)]
check("no module still points at the deleted script", not stale_refs, str(stale_refs))
check("ingest_knowledge.py is the canonical path",
      (BACKEND / "ingest_knowledge.py").exists())
check("the RUNBOOK explains why there is only one ingestion path",
      "scrape_and_ingest" in (BACKEND / "RUNBOOK.md").read_text(),
      "a deleted script still needs a pointer so nobody re-adds it")

# ---------------------------------------------------------------------------
section("ingestion refuses to write an untaggable document")
# ---------------------------------------------------------------------------
# The whole point of ingest_knowledge.py: a chunk with a bad tag is invisible.
check("ingest validates against the tenant's tag set", "VALID_CLEARANCE" in ing)
check("ingest fails loudly on an invalid clearance", "invalid clearance" in ing)
check("ingest refuses when the tenant declares NO tags",
      "declares no clearance tags" in ing,
      "otherwise nothing could be tagged OR retrieved")
check("ingest excludes the clearance tag from the embedding",
      "excluded_embed_metadata_keys" in ing)
check("append is opt-in, so a rebuild is a deliberate choice",
      "--append" in ing)

# ---------------------------------------------------------------------------
section("the API's cross-tenant boundary")
# ---------------------------------------------------------------------------
main_src = source_of("main.py")
check("a request naming another client is refused with 409",
      "409" in main_src and "resolve_request_client" in main_src)
check("one process serves exactly one client",
      "ACTIVE_CLIENT" in main_src)
check("/metrics is scoped to the active client",
      "get_token_metrics(limit=20, client=ACTIVE_CLIENT)" in main_src)
check("audit writes are stamped with the client",
      main_src.count("client=ACTIVE_CLIENT") >= 4,
      f"{main_src.count('client=ACTIVE_CLIENT')} stamped log_query calls")

finish("test_tenancy")
