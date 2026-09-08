"""Offline tests for the authentication boundary (backend/auth.py).

THE HOLE THIS EXISTS FOR
------------------------
Before auth.py, clearance was a FIELD IN THE REQUEST BODY. A plain curl with
`"clearance_level": "executive"` and no credential returned board-only material —
Q2 revenue and an acquisition bid. Authorization was fine; authentication did not
exist, and authorization on an unauthenticated claim is an honour system.

These tests pin the boundary itself: the key decides the role, the body may only
narrow it, a misconfigured instance refuses to boot, and the endpoints actually
enforce all of that. No network, no real keys.

Run:  RAG_OFFLINE=1 venv/bin/python tests/test_auth.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (base_cfg, check, env, finish, import_main, section,  # noqa: E402
                     test_client, with_auth)

import auth  # noqa: E402
import client_registry as registry  # noqa: E402

GUEST = "guest-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
EMP = "employee-key-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
EXEC = "executive-key-cccccccccccccccccccccccccccc"

KEYS = dict(RAG_KEY_GUEST=GUEST, RAG_KEY_EMPLOYEE=EMP, RAG_KEY_EXECUTIVE=EXEC)
NO_KEYS = dict(RAG_KEY_GUEST=None, RAG_KEY_EMPLOYEE=None, RAG_KEY_EXECUTIVE=None)

# ---------------------------------------------------------------------------
section("key -> role mapping")
# ---------------------------------------------------------------------------
cfg = with_auth(required=True)

with env(**KEYS):
    check("guest key resolves to guest", auth.resolve_role(GUEST, cfg) == "guest")
    check("employee key resolves to employee", auth.resolve_role(EMP, cfg) == "employee")
    check("executive key resolves to executive", auth.resolve_role(EXEC, cfg) == "executive")

    check("an unknown key resolves to nothing", auth.resolve_role("wrong", cfg) is None)
    check("an empty key resolves to nothing", auth.resolve_role("", cfg) is None)
    check("None resolves to nothing", auth.resolve_role(None, cfg) is None)
    # A prefix of a real key must not pass. This is the specific thing a naive
    # `startswith` or truncated comparison would get wrong.
    check("a PREFIX of a real key is rejected", auth.resolve_role(GUEST[:20], cfg) is None)
    check("a real key plus junk is rejected", auth.resolve_role(GUEST + "x", cfg) is None)
    check("whitespace around a key is tolerated",
          auth.resolve_role(f"  {EXEC}  ", cfg) == "executive")

    # authenticate() is the endpoint-facing wrapper.
    check("authenticate maps a key to its role", auth.authenticate(EXEC, cfg) == "executive")
    for bad, why in ((None, "missing"), ("", "empty"), ("nope", "unknown")):
        try:
            auth.authenticate(bad, cfg)
            check(f"a {why} key is refused", False, "it authenticated")
        except auth.AuthError as e:
            check(f"a {why} key is refused with 401", e.status == 401, f"got {e.status}")

    # The 401 message must not tell an attacker WHICH way they were wrong.
    try:
        auth.authenticate("nope", cfg)
    except auth.AuthError as e:
        detail = e.detail.lower()
        check("the 401 for a bad key leaks no env-var name",
              "rag_key" not in detail, e.detail)
        check("the 401 for a bad key leaks no role list",
              "executive" not in detail, e.detail)

# A role declared in config but with NO key value set must not be usable — an
# unset key would otherwise compare equal to an empty presented key.
with env(RAG_KEY_GUEST=GUEST, RAG_KEY_EMPLOYEE=None, RAG_KEY_EXECUTIVE=None):
    check("a role whose key env var is unset cannot be authenticated",
          auth.resolve_role("", cfg) is None and auth.resolve_role(EXEC, cfg) is None)
    check("available_roles reports only roles with a key set",
          auth.available_roles(cfg) == ["guest"], str(auth.available_roles(cfg)))
    check("configured_roles still reports every declared role",
          auth.configured_roles(cfg) == ["employee", "executive", "guest"])

# ---------------------------------------------------------------------------
section("constant-time comparison")
# ---------------------------------------------------------------------------
# We assert the MECHANISM, not a timing measurement: timing tests are flaky on a
# shared machine, but a plain `==` in the source is a real leak (it short-circuits
# on the first wrong byte, so response time reveals how much of the key was right).
import inspect  # noqa: E402

src = inspect.getsource(auth.resolve_role)
check("resolve_role uses hmac.compare_digest", "compare_digest" in src)
check("resolve_role does not compare keys with ==",
      "== expected" not in src and "presented ==" not in src)
check("every role is compared even after a match (no early break)",
      "break" not in src, "an early break makes timing depend on which role matched")

# ---------------------------------------------------------------------------
section("the narrowing rule: down is fine, up is 403")
# ---------------------------------------------------------------------------
check("omitting a request clearance keeps what the key grants",
      auth.effective_clearance("executive", "", cfg) == "executive")
check("None keeps what the key grants",
      auth.effective_clearance("executive", None, cfg) == "executive")
check("asking for your own role is a no-op",
      auth.effective_clearance("guest", "guest", cfg) == "guest")

check("executive may narrow to employee",
      auth.effective_clearance("executive", "employee", cfg) == "employee")
check("executive may narrow to guest",
      auth.effective_clearance("executive", "guest", cfg) == "guest")
check("employee may narrow to guest",
      auth.effective_clearance("employee", "guest", cfg) == "guest")
check("case and whitespace are normalised when narrowing",
      auth.effective_clearance("executive", "  GUEST  ", cfg) == "guest")

for granted, want in (("guest", "employee"), ("guest", "executive"),
                      ("employee", "executive")):
    try:
        auth.effective_clearance(granted, want, cfg)
        check(f"{granted} key cannot request {want}", False, "it was allowed")
    except auth.AuthError as e:
        check(f"{granted} key cannot request {want}", e.status == 403, f"got {e.status}")

# A widening attempt must RAISE, not silently downgrade. A silent downgrade is the
# subtle failure: the caller believes it had executive reach and quietly gets
# public answers, which reads as broken retrieval rather than a refused request.
try:
    got = auth.effective_clearance("guest", "executive", cfg)
    check("widening raises rather than silently downgrading", False, f"returned {got!r}")
except auth.AuthError:
    check("widening raises rather than silently downgrading", True)

# An unknown role is a mistake worth surfacing: the retriever would fail it closed
# to the least-privileged tier, which looks like "the knowledge base is empty".
for bogus in ("admin", "root", "superuser", "guest;--"):
    try:
        auth.effective_clearance("executive", bogus, cfg)
        check(f"unknown clearance {bogus!r} is rejected", False, "it was accepted")
    except auth.AuthError as e:
        check(f"unknown clearance {bogus!r} is rejected with 400", e.status == 400,
              f"got {e.status}")

# ---------------------------------------------------------------------------
section("admin gate")
# ---------------------------------------------------------------------------
auth.require_admin("executive", cfg)
check("the configured admin role passes require_admin", True)
for role in ("guest", "employee", "nobody"):
    try:
        auth.require_admin(role, cfg)
        check(f"{role} is refused admin", False, "it was allowed")
    except auth.AuthError as e:
        check(f"{role} is refused admin", e.status == 403, f"got {e.status}")

# No admin_role configured must mean NOBODY, not everybody. "Unconfigured" is the
# state a half-finished onboarding leaves behind, so it has to fail closed.
no_admin = with_auth(required=True, admin_role="")
for role in ("guest", "employee", "executive"):
    try:
        auth.require_admin(role, no_admin)
        check(f"with no admin_role configured, {role} is NOT admin", False, "allowed")
    except auth.AuthError as e:
        check(f"with no admin_role configured, {role} is NOT admin", e.status == 403)

# ---------------------------------------------------------------------------
section("header parsing")
# ---------------------------------------------------------------------------
check("X-API-Key is read", auth.key_from_headers(GUEST, None) == GUEST)
check("Authorization: Bearer is read", auth.key_from_headers(None, f"Bearer {GUEST}") == GUEST)
check("bearer is case-insensitive", auth.key_from_headers(None, f"bearer {GUEST}") == GUEST)
check("X-API-Key wins when both are present",
      auth.key_from_headers(GUEST, f"Bearer {EXEC}") == GUEST)
check("a non-bearer Authorization is ignored",
      auth.key_from_headers(None, f"Basic {GUEST}") is None)
check("no headers means no key", auth.key_from_headers(None, None) is None)
check("blank headers mean no key", auth.key_from_headers("  ", "   ") is None)

# ---------------------------------------------------------------------------
section("fail-closed on misconfiguration")
# ---------------------------------------------------------------------------
# THE POINT: required:true with no keys in the environment must refuse to boot.
# Degrading to open access here would reintroduce the exact hole auth.py removes,
# and it would do it silently on a deployment that believed it was protected.
with env(**NO_KEYS):
    try:
        registry._validate_auth(with_auth(required=True), "test-config")
        check("required:true with NO key env vars refuses to boot", False,
              "validation passed")
    except SystemExit as e:
        check("required:true with NO key env vars refuses to boot", e.code == 1)

    # required:false is the local-dev default and must still load.
    try:
        registry._validate_auth(with_auth(required=False), "test-config")
        check("required:false loads with no keys (local dev)", True)
    except SystemExit:
        check("required:false loads with no keys (local dev)", False, "it refused")

with env(**KEYS):
    try:
        registry._validate_auth(with_auth(required=True), "test-config")
        check("required:true with keys present loads", True)
    except SystemExit:
        check("required:true with keys present loads", False, "it refused")

# A partially-configured instance loads but must WARN, not pretend all roles work.
with env(RAG_KEY_GUEST=GUEST, RAG_KEY_EMPLOYEE=None, RAG_KEY_EXECUTIVE=None):
    try:
        registry._validate_auth(with_auth(required=True), "test-config")
        check("required:true with SOME keys loads (partial config)", True)
    except SystemExit:
        check("required:true with SOME keys loads (partial config)", False, "refused")
    st = auth.status(with_auth(required=True))
    check("status names the roles that cannot be used",
          st["roles_missing_keys"] == ["employee", "executive"],
          str(st["roles_missing_keys"]))

# required:true with an EMPTY key map can never authenticate anyone.
with env(**KEYS):
    try:
        registry._validate_auth(with_auth(required=True, keys={}, admin_role=""), "test")
        check("required:true with an empty key map refuses to boot", False, "passed")
    except SystemExit as e:
        check("required:true with an empty key map refuses to boot", e.code == 1)

# A SECRET pasted where an env-var NAME belongs is the mistake this pattern exists
# to prevent, so the loader has to catch it.
for bad_value in ("sk-abc123def456", "hunter2", "rag_key_guest"):
    try:
        registry._validate_auth(
            with_auth(required=False, keys={"guest": bad_value}, admin_role="guest"),
            "test")
        check(f"a non-env-var-looking value {bad_value!r} is rejected", False, "passed")
    except SystemExit as e:
        check(f"a non-env-var-looking value {bad_value!r} is rejected", e.code == 1)

# A key granting a role the retriever doesn't know would fail closed on every
# query — a silent dead end, so it's a config error.
try:
    registry._validate_auth(
        with_auth(required=False, keys={"partner": "RAG_KEY_PARTNER"}, admin_role="partner"),
        "test")
    check("a key for a role missing from clearance_levels is rejected", False, "passed")
except SystemExit as e:
    check("a key for a role missing from clearance_levels is rejected", e.code == 1)

# An admin_role nobody holds means no caller could ever administer the instance.
try:
    registry._validate_auth(with_auth(required=False, admin_role="nobody"), "test")
    check("an admin_role not in auth.keys is rejected", False, "passed")
except SystemExit as e:
    check("an admin_role not in auth.keys is rejected", e.code == 1)

# ---------------------------------------------------------------------------
section("auth disabled (local dev) is not MORE permissive")
# ---------------------------------------------------------------------------
# A dev instance skipping the key must not accidentally hand out the widest role.
off = with_auth(required=False)
with env(**NO_KEYS):
    role = auth.authenticate(None, off)
    check("with auth off, a keyless caller gets the LEAST privileged role",
          role == "guest", f"got {role!r}")
    try:
        auth.effective_clearance(role, "executive", off)
        check("with auth off, the dev role still cannot widen to executive", False,
              "it was allowed")
    except auth.AuthError as e:
        check("with auth off, the dev role still cannot widen to executive",
              e.status == 403)

check("is_enabled reflects the config", auth.is_enabled(cfg) and not auth.is_enabled(off))

# ---------------------------------------------------------------------------
section("status() is safe to expose publicly")
# ---------------------------------------------------------------------------
with env(**KEYS):
    st = auth.status(with_auth(required=True))
    blob = repr(st)
    check("status leaks no key value",
          GUEST not in blob and EMP not in blob and EXEC not in blob)
    check("status leaks no env-var NAME", "RAG_KEY" not in blob, blob)
    check("status reports required", st["required"] is True)
    check("status lists roles", st["roles"] == ["employee", "executive", "guest"])
    check("status lists which roles have keys",
          st["roles_with_keys_set"] == ["employee", "executive", "guest"])
    check("status names the admin role", st["admin_role"] == "executive")

# ---------------------------------------------------------------------------
section("endpoints actually enforce it")
# ---------------------------------------------------------------------------
# Everything above is pure logic. This is the part that would still be broken if a
# single endpoint forgot to call it — which is how the original hole existed.
m = import_main()

# What the last stubbed call was told. Lets a test assert not only the answer but WHAT
# the endpoint forwarded — the actor especially, which is invisible in the response.
LAST_CALL: dict = {}


async def _fake_answer(query, clearance_level, platform, upload_token="", actor=None):
    """Stand-in for the whole retrieve->rerank->synthesize pipeline.

    Must return answer_once's real 4-tuple (text, meter, gated, saved) — the
    endpoint unpacks it to build the X-* token headers. It echoes the clearance it
    was CALLED with, which is how we prove the endpoint passed the key's role
    through rather than the body's claim.

    `actor` mirrors the real signature: the endpoint forwards a sanitised X-Actor for
    the audit trail. It is recorded rather than echoed into the answer, because it must
    never influence what the caller can read — only what the audit row says.
    """
    LAST_CALL.clear()
    LAST_CALL.update(query=query, clearance_level=clearance_level,
                     platform=platform, upload_token=upload_token, actor=actor)
    return f"ANSWER for clearance={clearance_level}", m.TokenMeter(), False, 0


with env(**KEYS):
    # The app's CFG was merged at import with auth.required=false (the shipped
    # local default), so flip the live config for these endpoint checks.
    saved_auth = m.CFG.get("auth")
    m.CFG["auth"] = {"required": True,
                     "keys": dict(RAG_KEY_GUEST="RAG_KEY_GUEST",
                                  RAG_KEY_EMPLOYEE="RAG_KEY_EMPLOYEE",
                                  RAG_KEY_EXECUTIVE="RAG_KEY_EXECUTIVE"),
                     "admin_role": "executive"}
    # keys map must be role -> env NAME
    m.CFG["auth"]["keys"] = {"guest": "RAG_KEY_GUEST",
                             "employee": "RAG_KEY_EMPLOYEE",
                             "executive": "RAG_KEY_EXECUTIVE"}

    client, mod, undo = test_client(answer_once=_fake_answer)
    try:
        # --- No key at all -------------------------------------------------
        for method, path, body in (
            ("post", "/api/rag", {"query": "hi"}),
            ("post", "/chat", {"query": "hi"}),
            ("get", "/metrics", None),
            ("get", "/clients", None),
            ("get", "/whoami", None),
            ("post", "/ingest", {}),
        ):
            r = getattr(client, method)(path, json=body) if body is not None \
                else getattr(client, method)(path)
            check(f"{method.upper()} {path} rejects a keyless caller",
                  r.status_code == 401, f"got {r.status_code}")

        # --- Liveness stays public ----------------------------------------
        r = client.get("/")
        check("GET / stays public (load balancers need it)", r.status_code == 200)
        check("GET / reports the auth posture", "auth" in r.json())
        check("GET / leaks no key",
              GUEST not in r.text and EXEC not in r.text)

        # --- A bogus key is refused ---------------------------------------
        r = client.post("/api/rag", json={"query": "hi"},
                        headers={"X-API-Key": "not-a-real-key"})
        check("a bogus key is refused with 401", r.status_code == 401, f"got {r.status_code}")

        # --- A valid key works, and the body cannot widen ------------------
        r = client.post("/api/rag", json={"query": "hi"}, headers={"X-API-Key": GUEST})
        check("a guest key is answered", r.status_code == 200, f"got {r.status_code}")
        check("the answer is computed at the KEY's clearance",
              "clearance=guest" in r.json().get("response", ""), r.text[:120])
        check("the response reports the clearance used",
              r.headers.get("X-Clearance") == "guest", str(r.headers.get("X-Clearance")))

        r = client.post("/api/rag", json={"query": "hi", "clearance_level": "executive"},
                        headers={"X-API-Key": GUEST})
        check("a guest key asking for executive is 403", r.status_code == 403,
              f"got {r.status_code}")

        r = client.post("/api/rag", json={"query": "hi", "clearance_level": "guest"},
                        headers={"X-API-Key": EXEC})
        check("an executive key may narrow to guest", r.status_code == 200)
        check("the narrowed answer really used guest",
              "clearance=guest" in r.json().get("response", ""), r.text[:120])

        # THE ORIGINAL EXPLOIT, as a permanent regression test: the body alone
        # must never be enough to reach executive material.
        r = client.post("/api/rag", json={"query": "What was Q2 revenue?",
                                          "clearance_level": "executive"})
        check("the original exploit (body-only executive claim) is refused",
              r.status_code == 401, f"got {r.status_code}")

        # --- Bearer form works too ----------------------------------------
        r = client.post("/api/rag", json={"query": "hi"},
                        headers={"Authorization": f"Bearer {EMP}"})
        check("a Bearer key is accepted", r.status_code == 200, f"got {r.status_code}")

        # --- X-Actor: recorded for the audit trail, NEVER authorization ----
        # The internal frontend sends this so an audit row can say WHO asked. It is
        # caller-supplied, so the rules are: forward it sanitised, and let it change
        # nothing about what the caller may read.
        r = client.post("/api/rag", json={"query": "hi"},
                        headers={"X-API-Key": GUEST, "X-Actor": "ada"})
        check("a request carrying X-Actor still succeeds", r.status_code == 200)
        check("the endpoint forwards the actor for the audit trail",
              LAST_CALL.get("actor") == "ada", str(LAST_CALL.get("actor")))
        check("carrying an actor does NOT change the clearance used",
              "clearance=guest" in r.json().get("response", ""), r.text[:120])

        # A guest key claiming to be the executive by name gets exactly nothing extra:
        # the name is recorded, the tier still comes from the key.
        r = client.post("/api/rag", json={"query": "What was Q2 revenue?"},
                        headers={"X-API-Key": GUEST, "X-Actor": "peter"})
        check("naming a privileged person in X-Actor grants no privilege",
              r.status_code == 200 and "clearance=guest" in r.json().get("response", ""),
              r.text[:120])
        check("  ...and the claimed name is still recorded, not silently dropped",
              LAST_CALL.get("actor") == "peter", str(LAST_CALL.get("actor")))

        # Junk is sanitised to None at the boundary rather than reaching the DB.
        for junk, why in (("ada bob", "space"),
                          ("ada\nfake log line", "newline"),
                          ("a" * 200, "over-long"),
                          ("<script>x</script>", "HTML")):
            r = client.post("/api/rag", json={"query": "hi"},
                            headers={"X-API-Key": GUEST, "X-Actor": junk})
            check(f"a bogus actor ({why}) is dropped, and the request still works",
                  r.status_code == 200 and LAST_CALL.get("actor") is None,
                  f"status={r.status_code} actor={LAST_CALL.get('actor')!r}")

        r = client.post("/api/rag", json={"query": "hi"}, headers={"X-API-Key": GUEST})
        check("no X-Actor header at all -> actor is None (public deployment)",
              LAST_CALL.get("actor") is None, str(LAST_CALL.get("actor")))

        # --- /whoami tells a caller about ITSELF ---------------------------
        r = client.get("/whoami", headers={"X-API-Key": EMP})
        check("/whoami reports the key's role", r.json().get("role") == "employee", r.text[:120])
        check("/whoami lists only narrowing options",
              set(r.json().get("can_narrow_to", [])) == {"guest", "employee"},
              str(r.json().get("can_narrow_to")))
        check("/whoami leaks no key", EMP not in r.text and EXEC not in r.text)
        r = client.get("/whoami", headers={"X-API-Key": EXEC})
        check("/whoami marks the admin role", r.json().get("is_admin") is True)
        r = client.get("/whoami", headers={"X-API-Key": GUEST})
        check("/whoami does not mark a non-admin as admin",
              r.json().get("is_admin") is False)

        # --- /ingest is admin-only and append-by-default -------------------
        for key, role in ((GUEST, "guest"), (EMP, "employee")):
            r = client.post("/ingest", json={}, headers={"X-API-Key": key})
            check(f"/ingest refuses {role}", r.status_code == 403, f"got {r.status_code}")

        # --- cross-tenant guard survives auth ------------------------------
        r = client.post("/api/rag", json={"query": "hi", "client": "acme"},
                        headers={"X-API-Key": EXEC})
        check("a cross-tenant request is still 409", r.status_code == 409,
              f"got {r.status_code}")
    finally:
        undo()
        m.CFG["auth"] = saved_auth

# ---------------------------------------------------------------------------
section("keys in .env are visible to validation, whatever the import order")
# ---------------------------------------------------------------------------
# REGRESSION. The offline suite injects keys straight into os.environ, so it
# never exercised how a real boot gets them: from backend/.env via dotenv.
# database.py resolves its per-tenant DB path at MODULE level, which touches the
# registry -- and the registry validates auth. Because main.py called
# load_dotenv() ~100 lines AFTER importing database, validation ran against an
# environment that had not been populated yet, and a server whose .env held three
# perfectly good keys refused to boot. Every unit test passed the whole time.
#
# The fix put load_dotenv() in client_registry itself (the module that reads
# these vars), so no caller can get the order wrong. This pins that: a subprocess
# that imports the registry with the key vars scrubbed from its environment must
# still see them, because the registry loaded .env on its own.
import subprocess  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dotenv_path = os.path.join(BACKEND, ".env")

if not os.path.exists(dotenv_path):
    check("SKIPPED: no backend/.env on this machine (nothing to load)", True)
else:
    probe = (
        "import os, sys; sys.path.insert(0, %r);"
        "import client_registry;"
        "print(','.join(k for k in ('RAG_KEY_GUEST','RAG_KEY_EMPLOYEE','RAG_KEY_EXECUTIVE')"
        " if os.environ.get(k)))" % BACKEND
    )
    scrubbed = {k: v for k, v in os.environ.items() if not k.startswith("RAG_KEY_")}
    # Prove the vars really are absent, so a pass cannot come from inheritance.
    check("the probe environment has no RAG_KEY_* vars to inherit",
          not any(k.startswith("RAG_KEY_") for k in scrubbed))

    named = set()
    for line in open(dotenv_path, encoding="utf-8", errors="replace"):
        s = line.strip()
        if s.startswith("RAG_KEY_") and "=" in s and s.split("=", 1)[1].strip():
            named.add(s.split("=", 1)[0].strip())

    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, env=scrubbed, cwd="/")
    loaded = set(filter(None, out.stdout.strip().split(",")))
    check("importing client_registry alone loads backend/.env",
          loaded == named and bool(named),
          f"expected {sorted(named)}, got {sorted(loaded)}; stderr={out.stderr[:200]}")
    # cwd="/" above matters: the load must be anchored to the backend directory,
    # not to wherever the process happens to have been started from.
    check("the .env load does not depend on the process's cwd", loaded == named)

finish("test_auth")
