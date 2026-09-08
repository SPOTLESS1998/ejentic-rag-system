"""Offline tests for the audit trail's ACTOR column — who asked, not just what.

THE FAILURE THIS PINS
---------------------
The internal per-person deployment justifies its whole existence partly on
accountability: staff.ts's docblock says an audit row records "WHO asked, not just
'someone with the staff password'", and deploy/GO_LIVE.md repeats it. The frontend
proxy duly sends `X-Actor: <staff id>` on every backend call.

**The backend ignored it completely.** `grep -i actor` over the backend returned only
the word "refac*tor*". `log_query()` had no actor parameter and `audit_logs` had no
column, so the header was received and dropped on the floor. The clearance boundary was
never affected — nobody got access they shouldn't — but the documented accountability
did not exist. If executive material leaked, you could not tell WHICH executive ran the
query, which is precisely the question a shared key cannot answer and this tier was
built to answer.

WHAT IS BEING PROVEN
--------------------
  • The actor is SANITISED before storage. It arrives in a header, so it is
    caller-controlled: bad character sets, over-long values and control characters must
    not reach the DB or the log lines.
  • A rejected actor becomes NULL, never a mangled or truncated name. A WRONG name in an
    audit trail is worse than no name — it points an investigation at the wrong person.
  • The actor is recorded and GRANTS NOTHING. Clearance still comes from the API key
    alone, so sending someone else's id must not change what you can read.
  • The column is added ADDITIVELY to a pre-existing DB (the same ALTER-if-missing
    pattern the token and client columns already use), and old rows are NOT backfilled
    with an invented actor.

Run:  venv/bin/python tests/test_audit_actor.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish, section  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
import auth as authmod  # noqa: E402

# ---------------------------------------------------------------------------
section("clean_actor() sanitises a caller-supplied id")
# ---------------------------------------------------------------------------
# The value lands in the audit DB and in log lines and comes straight off a header.
clean = authmod.clean_actor

check("None -> None", clean(None) is None)
check("empty string -> None", clean("") is None)
check("whitespace only -> None", clean("   ") is None)

check("a plain staff id passes through", clean("ada") == "ada")
check("hyphen and underscore are allowed", clean("ada-b_c") == "ada-b_c")
check("digits are allowed", clean("agent007") == "agent007")
check("a dot is allowed (first.last)", clean("ada.lovelace") == "ada.lovelace")
check("upper case is preserved, not silently lowercased", clean("Ada") == "Ada")
check("surrounding whitespace is trimmed", clean("  ada  ") == "ada")

# Anything that could poison a log line, a CSV export or a downstream query.
for bad, why in [
    ("ada bob", "space"),
    ("ada;DROP TABLE audit_logs", "SQL-ish punctuation"),
    ("ada'--", "quote and comment"),
    ("../../etc/passwd", "path traversal"),
    ("ada\nINFO fake log line", "newline (log injection)"),
    ("ada\r\nSet-Cookie: x=y", "CRLF (header/log injection)"),
    ("ada\x00bob", "NUL byte"),
    ("ada\tbob", "tab"),
    ("<script>alert(1)</script>", "HTML"),
    ("ada@example.com", "at-sign"),
    ("adá", "non-ASCII"),
    ("💀", "emoji"),
]:
    check(f"rejected: {why}", clean(bad) is None, repr(bad[:24]))

check("a 64-char id is accepted (the boundary)", clean("a" * 64) == "a" * 64)
check("a 65-char id is REJECTED, not truncated to a wrong name",
      clean("a" * 65) is None)
check("an absurdly long value is rejected outright", clean("a" * 10000) is None)

# ---------------------------------------------------------------------------
section("the audit table has somewhere to put it")
# ---------------------------------------------------------------------------
TMP_DB = Path(tempfile.mkdtemp(prefix="rag_actor_")) / "audit.db"
os.environ["RAG_AUDIT_DB"] = str(TMP_DB)
# database.py reads RAG_AUDIT_DB at import; ensure a fresh module.
sys.modules.pop("database", None)
import database as db  # noqa: E402

check("audit_logs has an actor column", hasattr(db.AuditLog, "actor"))
check("the actor column is indexed (you will filter on it in an investigation)",
      db.AuditLog.actor.index is True)
check("the migration adds actor to a PRE-EXISTING table",
      "actor" in db._TOKEN_COLUMNS, str(sorted(db._TOKEN_COLUMNS)))
check("the column is nullable — the public deployment identifies nobody",
      db.AuditLog.actor.nullable is True)

# ---------------------------------------------------------------------------
section("the actor is actually PERSISTED, end to end")
# ---------------------------------------------------------------------------


async def exercise():
    await db.init_db()
    # A per-person (internal) query.
    await db.log_query("WEB_executive", "What was Q2 revenue?", "$2.4M",
                       prompt_tokens=10, completion_tokens=5, total_tokens=15,
                       token_source="provider", client="ejentic", actor="peter")
    # A public-deployment query: nobody is individually identified.
    await db.log_query("WEB_guest", "What do you do?", "We build agents.",
                       prompt_tokens=8, completion_tokens=4, total_tokens=12,
                       token_source="provider", client="ejentic")
    # A gated query from a second person, to prove it is per-row not per-process.
    await db.log_query("WEB_employee", "gated question", "[GATE] no",
                       estimated_saved_tokens=42, gated=True,
                       client="ejentic", actor="ada")

    from sqlalchemy import select
    async with db.AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(db.AuditLog.actor, db.AuditLog.query_text, db.AuditLog.clearance_level)
            .order_by(db.AuditLog.id)
        )).all()
    return rows


rows = asyncio.run(exercise())
check("all three rows were written", len(rows) == 3, str(len(rows)))

by_actor = {r[0]: r[1] for r in rows}
check("the executive query is attributed to peter", "peter" in by_actor,
      str(sorted(str(k) for k in by_actor)))
check("the gated employee query is attributed to ada", "ada" in by_actor)
check("peter's row kept the right query text",
      by_actor.get("peter") == "What was Q2 revenue?")
check("the public-deployment row has actor NULL, not a placeholder",
      None in by_actor, str(sorted(str(k) for k in by_actor)))
check("actor is per-row, not smeared across the process",
      by_actor.get("ada") == "gated question")

# An over-long actor must not reach storage even if a caller bypasses clean_actor.
# log_query truncates defensively; the point is that the DB never holds an unbounded
# blob in a VARCHAR(64) column, since SQLite will not enforce the width itself.


async def defensive():
    await db.log_query("WEB_guest", "q", "a", client="ejentic", actor="z" * 500)
    from sqlalchemy import select, func
    async with db.AsyncSessionLocal() as s:
        return await s.scalar(
            select(func.max(func.length(db.AuditLog.actor)))
        )


longest = asyncio.run(defensive())
check("log_query caps a raw over-long actor at the column width",
      longest is not None and longest <= 64, f"max stored length={longest}")

# ---------------------------------------------------------------------------
section("the actor GRANTS NOTHING — attribution is not authorization")
# ---------------------------------------------------------------------------
# The header is caller-supplied. Anyone holding a key could forge it, so the ONLY thing
# it may affect is the audit row. Clearance must still come from the key alone.
check("the header constant is defined once, beside the API key header",
      authmod.ACTOR_HEADER == "X-Actor", authmod.ACTOR_HEADER)

src = (BACKEND / "main.py").read_text()
# Where the actor is resolved relative to where clearance is decided: the clearance
# call must come FIRST, so a reader can see the actor cannot influence it.
for endpoint in ("chat", "n8n_rag_endpoint"):
    idx = src.find(f"async def {endpoint}(")
    body = src[idx:idx + 1400]
    pos_clear = body.find("_clearance_for(")
    pos_actor = body.find("clean_actor(")
    check(f"{endpoint}: clearance is decided BEFORE the actor is read",
          pos_clear != -1 and pos_actor != -1 and pos_clear < pos_actor,
          f"clearance@{pos_clear} actor@{pos_actor}")

check("clean_actor is used at the boundary, not the raw header",
      "clean_actor(x_actor)" in src)
check("the raw x_actor header is never passed straight into a query path",
      "actor=x_actor" not in src)

# resolve_role decides the role; it must not consider the actor at all.
auth_src = (BACKEND / "auth.py").read_text()
resolve_src = auth_src[auth_src.find("def resolve_role("):]
resolve_src = resolve_src[:resolve_src.find("\ndef ", 10)]
check("resolve_role() never consults the actor",
      "actor" not in resolve_src.lower().replace("refactor", ""))

# ---------------------------------------------------------------------------
section("old rows are not given an invented actor")
# ---------------------------------------------------------------------------
# Deliberate asymmetry with the `client` column, which IS backfilled: that DB file
# belongs to one tenant so stamping it is correct. An actor is a PERSON, and guessing
# which person ran a historical query would point an investigation at the wrong name.
mig_src = (BACKEND / "database.py").read_text()
mig = mig_src[mig_src.find("def _migrate("):]
mig = mig[:mig.find("\nasync def ")]
check("_migrate does not UPDATE actor on existing rows",
      "SET actor" not in mig and "actor =" not in mig.replace("actor)", ""))
check("_migrate does create the actor index",
      "ix_audit_logs_actor" in mig)

finish("test_audit_actor")
