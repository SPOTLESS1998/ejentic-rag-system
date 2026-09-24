"""Offline tests for retrieval traceability — WHICH chunks fed an answer.

WHY THIS FILE EXISTS
--------------------
The audit trail recorded the question and the answer but not what the model was
READING. So "why did it say that?" was unanswerable after the fact, and
re-running the query is not the same experiment — the index, the config and the
model all move underneath you. A wrong answer could be seen but not
reconstructed.

TWO PROPERTIES CARRY THE WEIGHT HERE

  * WIRING (section 3). Every assertion about the trace format still passes if
    nobody ever calls `_source_trace`. Only driving a real query through the
    real logging path proves the column is populated. This is the same shape as
    the `actor` bug: the proxy sent it, the docs described it, and the middle was
    missing — nothing failed, the data simply was not there.

  * POINTERS, NOT PAYLOAD (section 2). The trace must carry ids, refs, clearance
    tags and scores — never chunk TEXT. The audit DB has different access rules
    from the vector store, so copying restricted passages into it would turn the
    audit trail into a way to read exactly what the clearance filter exists to
    withhold. A traceability feature that leaks is worse than no traceability.

Run:  venv/bin/python tests/test_traceability.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, env, finish, import_main, section, test_client  # noqa: E402

import database  # noqa: E402

m = import_main()

_GUEST_KEY = "a" * 64          # fake, correct shape; never a real credential


class _FakeNode:
    def __init__(self, node_id, metadata, text):
        self.node_id = node_id
        self.metadata = metadata
        self._text = text

    def get_content(self):
        return self._text


class _FakeNWS:
    def __init__(self, node_id, metadata, text, score):
        self.node = _FakeNode(node_id, metadata, text)
        self.score = score


_SECRET_TEXT = "Q2 revenue was $2.4M and the OmniScrape bid was $12M."
_NODES = [
    _FakeNWS("vec-001", {"clearance": "public", "file_name": "marketing.json"},
             "Ejentic builds AI agents.", 0.8123456),
    _FakeNWS("vec-002", {"clearance": "executive", "title": "Board pack"},
             _SECRET_TEXT, 0.41),
]


# ---------------------------------------------------------------------------
section("1. the trace records enough to reconstruct a bad answer")
# ---------------------------------------------------------------------------
_raw = m._source_trace(_NODES)
check("the trace is a string (ready for a TEXT column)", isinstance(_raw, str))
_parsed = json.loads(_raw)
check("it is valid JSON", isinstance(_parsed, list) and len(_parsed) == 2,
      f"parsed={_parsed}")

_first = _parsed[0]
check("each entry keeps its POSITION, so [Source N] in the answer maps back",
      _first["n"] == 1 and _parsed[1]["n"] == 2)
check("each entry carries the chunk id (the pointer used to re-fetch it)",
      _first["id"] == "vec-001", f"id={_first.get('id')}")
check("each entry carries a human-readable ref", _first["ref"] == "marketing.json")
check("`title` is used when `file_name` is absent",
      _parsed[1]["ref"] == "Board pack", f"ref={_parsed[1].get('ref')}")
check("each entry carries the clearance tag the chunk was stored with",
      _first["clearance"] == "public" and _parsed[1]["clearance"] == "executive")
check("the rerank score is kept (so a marginal retrieval is visible later)",
      isinstance(_first["score"], float) and abs(_first["score"] - 0.8123) < 0.001,
      f"score={_first['score']}")
check("scores are rounded, not stored to full float noise",
      len(str(_parsed[1]["score"])) <= 6, f"score={_parsed[1]['score']}")

check("no retrieval => empty trace, not the string 'null' or '[]'",
      m._source_trace([]) == "" and m._source_trace(None) == "")


# ---------------------------------------------------------------------------
section("2. 🔒 POINTERS, NOT PAYLOAD — the trace must not carry chunk text")
# ---------------------------------------------------------------------------
# The audit DB has different access rules from the vector store. Copying a
# restricted passage in here would make the audit trail a way to read what the
# clearance filter exists to withhold.
check("the executive chunk's TEXT is absent from the trace",
      _SECRET_TEXT not in _raw, "restricted content must not be copied here")
check("no dollar figure from the restricted chunk leaked in",
      "2.4M" not in _raw and "$12M" not in _raw, f"trace={_raw}")
check("the public chunk's text is absent too (this is not about secrecy alone — "
      "the column is a pointer, not a store)",
      "Ejentic builds AI agents." not in _raw)
check("but the restricted chunk IS still identifiable by id",
      "vec-002" in _raw, "traceability must survive the redaction")

# A trace is written from a fire-and-forget audit task, so it must never raise.
# Two different shapes of "bad node", which behave differently on purpose:
#   * an object that merely LACKS the attributes — the getattr defaults absorb
#     it, and it is recorded as a null entry. The position is still there, so
#     you can see that source N existed but told us nothing.
#   * an object that RAISES on access — caught, and explicitly marked, because
#     silently recording that as a null entry would hide a real defect.
_bare = json.loads(m._source_trace([object()]))
check("a node missing its attributes cannot raise", isinstance(_bare, list))
check("...and is still recorded positionally, with nulls",
      _bare[0]["n"] == 1 and _bare[0]["id"] is None, f"entry={_bare[0]}")


class _Exploding:
    @property
    def node(self):
        raise RuntimeError("boom")


_boom = m._source_trace([_Exploding()])
check("a node that RAISES on access cannot break the query", isinstance(_boom, str))
check("...and is marked unreadable rather than recorded as a normal null entry",
      "unreadable" in _boom, f"trace={_boom}")
check("a node with no score is tolerated",
      json.loads(m._source_trace([_FakeNWS("x", {}, "t", None)]))[0]["score"] is None)


# ---------------------------------------------------------------------------
section("3. WIRING — a real query actually persists the trace")
# ---------------------------------------------------------------------------
# Everything above passes if nothing ever calls _source_trace. This is the only
# part that would notice.
_captured = {}


async def _spy_log_query(clearance, query, response, **kw):
    _captured["clearance"] = clearance
    _captured["sources"] = kw.get("sources")
    _captured["called"] = _captured.get("called", 0) + 1


async def _fake_retrieve(query, clearance_level, meter=None, upload_token=""):
    return _NODES, 0.81, query


async def _fake_chat(messages, **kw):
    class _R:
        class message:
            content = "Ejentic builds AI agents. [Source 1]"
    return _R()


client, _mod, undo = test_client(retrieve_and_rerank=_fake_retrieve,
                                 log_query=_spy_log_query,
                                 _retry_achat=_fake_chat)
try:
    with env(RAG_KEY_GUEST=_GUEST_KEY):
        r = client.post("/api/rag", json={"query": "what services do you offer?"},
                        headers={"X-API-Key": _GUEST_KEY})
        check("the query succeeded", r.status_code == 200, f"status={r.status_code}")
        check("WIRING: log_query was called", _captured.get("called", 0) >= 1)
        check("WIRING: it was given a sources trace, not None",
              bool(_captured.get("sources")),
              "if this is empty, the column is never populated in production")
        _live = json.loads(_captured["sources"] or "[]")
        check("WIRING: the trace names the real retrieved chunk ids",
              [e["id"] for e in _live] == ["vec-001", "vec-002"],
              f"ids={[e.get('id') for e in _live]}")
        check("WIRING: and still carries no chunk text",
              _SECRET_TEXT not in (_captured["sources"] or ""))
finally:
    undo()


# ---------------------------------------------------------------------------
section("4. the column is additive, capped, and readable")
# ---------------------------------------------------------------------------
check("`sources` is a real column on the audit model",
      hasattr(database.AuditLog, "sources"))
check("it is in the ALTER-if-missing migration map, so existing DBs gain it",
      "sources" in database._TOKEN_COLUMNS,
      f"keys={sorted(database._TOKEN_COLUMNS)}")
check("it is nullable — historical rows predate it and NULL is the honest value",
      database.AuditLog.__table__.c.sources.nullable)
check("log_query accepts a `sources` argument",
      "sources" in database.log_query.__code__.co_varnames)

# A diagnostic column must not be able to bloat a row from a large retrieval.
import inspect  # noqa: E402
_src = inspect.getsource(database.log_query)
check("the stored value is length-capped", "[:4000]" in _src, "unbounded write")

# Written but never surfaced = nobody reads it. /metrics must expose it.
_metrics_src = inspect.getsource(database.get_token_metrics)
check("/metrics surfaces `sources` (a write-only column is never read)",
      '"sources"' in _metrics_src)

finish("test_traceability")
