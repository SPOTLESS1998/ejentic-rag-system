"""Offline tests for the per-request resource bounds.

WHY THIS FILE EXISTS
--------------------
Nothing bounded a single request. Two holes, both on the live public path:

  * The LLM was constructed with NO `max_tokens`, so the model decided how long to
    talk for and `LLM_TIMEOUT` (300s by default) was the only ceiling. One caller
    could hold a five-minute unbounded generation open and bill it to us.
  * `QueryRequest.query` was a bare `str`. A caller could post a megabyte and have
    it embedded, rewritten through an LLM hop, and fed to a metered model — and
    nothing about the request looked like abuse.

Neither is a tuning knob. They are the difference between "expensive" and
"unbounded", and an unbounded cost on a public endpoint is the failure mode you
cannot recover from after the fact.

The important assertion here is ORDERING: an over-long query must be refused
BEFORE any embedding, retrieval or LLM call happens. A cap that is checked after
the spend it exists to prevent is decoration. Section 3 proves it by counting
calls into stubbed seams that must never fire.

Run:  venv/bin/python tests/test_resource_bounds.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (base_cfg, check, env, finish, import_main, section,  # noqa: E402
                     test_client)

import client_registry as registry  # noqa: E402

m = import_main()


# ---------------------------------------------------------------------------
section("1. the bounds exist, are inheritable, and are sane")
# ---------------------------------------------------------------------------
check("max_output_tokens is in DEFAULT_CONFIG (so every tenant inherits it)",
      "max_output_tokens" in registry.DEFAULT_CONFIG)
check("max_query_chars is in DEFAULT_CONFIG",
      "max_query_chars" in registry.DEFAULT_CONFIG)
check("a tenant config that never mentions the bounds still gets them",
      bool(base_cfg().get("max_output_tokens")) and bool(base_cfg().get("max_query_chars")))

check("MAX_OUTPUT_TOKENS resolved to a positive int",
      isinstance(m.MAX_OUTPUT_TOKENS, int) and m.MAX_OUTPUT_TOKENS > 0,
      f"={m.MAX_OUTPUT_TOKENS}")
check("MAX_QUERY_CHARS resolved to a positive int",
      isinstance(m.MAX_QUERY_CHARS, int) and m.MAX_QUERY_CHARS > 0,
      f"={m.MAX_QUERY_CHARS}")

# Generous on purpose. A cap tight enough to truncate a real answer trades a cost
# problem for a correctness problem — and the longest answer this system has
# actually produced in production was ~1,100 chars (~300 tokens).
check("output cap leaves real headroom over the longest observed answer (~300 tok)",
      m.MAX_OUTPUT_TOKENS >= 1024, f"={m.MAX_OUTPUT_TOKENS}")
check("query cap is well above any real question",
      m.MAX_QUERY_CHARS >= 500, f"={m.MAX_QUERY_CHARS}")
check("the query cap still bounds the prompt (not effectively infinite)",
      m.MAX_QUERY_CHARS <= 100_000, f"={m.MAX_QUERY_CHARS}")
check("env can override the output cap",
      int(os.environ.get("MAX_OUTPUT_TOKENS") or m.MAX_OUTPUT_TOKENS) == m.MAX_OUTPUT_TOKENS)


# ---------------------------------------------------------------------------
section("2. the query cap is declared ON THE MODEL (so FastAPI enforces it early)")
# ---------------------------------------------------------------------------
_field = m.QueryRequest.model_fields["query"]
_constraints = [getattr(md, "max_length", None) for md in getattr(_field, "metadata", [])]
check("QueryRequest.query carries a max_length constraint",
      m.MAX_QUERY_CHARS in _constraints, f"constraints={_constraints}")
check("query is still REQUIRED (the cap did not make it optional)",
      _field.is_required())

_ok = m.QueryRequest(query="x" * (m.MAX_QUERY_CHARS - 1))
check("a normal-length query validates", len(_ok.query) == m.MAX_QUERY_CHARS - 1)
check("a query exactly at the cap validates (boundary is inclusive)",
      len(m.QueryRequest(query="x" * m.MAX_QUERY_CHARS).query) == m.MAX_QUERY_CHARS)

try:
    m.QueryRequest(query="x" * (m.MAX_QUERY_CHARS + 1))
    check("one char over the cap is REJECTED", False, "it validated")
except Exception:
    check("one char over the cap is REJECTED", True)

try:
    m.QueryRequest(query="x" * 1_000_000)
    check("a megabyte query is REJECTED", False, "it validated")
except Exception:
    check("a megabyte query is REJECTED", True)

# The other fields are untouched — this change must not alter the frozen contract.
check("clearance_level still defaults to empty (contract unchanged)",
      m.QueryRequest(query="q").clearance_level == "")
check("platform still defaults to WEB_UI (contract unchanged)",
      m.QueryRequest(query="q").platform == "WEB_UI")


# ---------------------------------------------------------------------------
section("3. ORDERING — an over-long query costs NOTHING")
# ---------------------------------------------------------------------------
# The whole point of putting the cap on the model rather than in the handler. If
# the request is refused before the handler runs, none of these seams can fire.
_calls = {"answer": 0, "retrieve": 0, "rewrite": 0}


async def _spy_answer(*a, **kw):
    _calls["answer"] += 1
    return "should never happen", m.TokenMeter(), False, 0


async def _spy_retrieve(*a, **kw):
    _calls["retrieve"] += 1
    return [], 0.0, "q"


async def _spy_rewrite(q, meter=None):
    _calls["rewrite"] += 1
    return q


client, _mod, undo = test_client(answer_once=_spy_answer,
                                 retrieve_and_rerank=_spy_retrieve,
                                 rewrite_query=_spy_rewrite)
_GUEST_KEY = "a" * 64          # fake, correct shape; never a real credential
try:
    # No credential at all. The refusal must STILL be free — an unauthenticated
    # caller must not be able to make us embed or generate. Note the 422 arrives
    # ahead of the 401: body validation runs before the auth dependency. For a
    # cheap length check that ordering is correct (refuse without spending);
    # it would be the WRONG order for anything that costs something to decide.
    r = client.post("/api/rag", json={"query": "x" * (m.MAX_QUERY_CHARS + 50)})
    check("an over-long query is refused with a 4xx", 400 <= r.status_code < 500,
          f"status={r.status_code}")
    check("it is 422 — body validation, ahead of both the handler and auth",
          r.status_code == 422, f"status={r.status_code}")
    check("ORDERING: no answer/synthesis call was made", _calls["answer"] == 0,
          f"answer calls={_calls['answer']}")
    check("ORDERING: no retrieval (so no embedding spend)", _calls["retrieve"] == 0,
          f"retrieve calls={_calls['retrieve']}")
    check("ORDERING: no query-rewrite LLM hop", _calls["rewrite"] == 0,
          f"rewrite calls={_calls['rewrite']}")
    check("the rejection body names the offending field, not our internals",
          "query" in r.text and "Traceback" not in r.text)

    # An over-long query WITH a valid key is still refused, and still for free —
    # so a legitimate key holder cannot spend unboundedly either.
    with env(RAG_KEY_GUEST=_GUEST_KEY):
        r_auth = client.post("/api/rag",
                             json={"query": "x" * (m.MAX_QUERY_CHARS + 50)},
                             headers={"X-API-Key": _GUEST_KEY})
        check("an over-long query from an AUTHENTICATED caller is refused too",
              r_auth.status_code == 422, f"status={r_auth.status_code}")
        check("ORDERING: still no spend for the authenticated case",
              _calls["answer"] == 0 and _calls["retrieve"] == 0,
              f"calls={_calls}")

        # CONTROL: the same seams DO fire for an acceptable query, so every zero
        # above means "refused early" and not "the stubs were never wired up".
        r_ok = client.post("/api/rag", json={"query": "what services do you offer?"},
                           headers={"X-API-Key": _GUEST_KEY})
        check("CONTROL: an acceptable query reaches the handler",
              r_ok.status_code == 200, f"status={r_ok.status_code}")
        check("CONTROL: the answer seam fired for it (stubs really were wired)",
              _calls["answer"] == 1, f"answer calls={_calls['answer']}")
finally:
    undo()


# ---------------------------------------------------------------------------
section("4. the output cap is passed to the LLM, not just computed")
# ---------------------------------------------------------------------------
# Same lesson as the clearance guard: a constant that nothing reads is decoration.
# main.py builds Settings.llm at import, which RAG_OFFLINE skips, so assert on the
# source of the constructor call rather than a live client.
import inspect  # noqa: E402
import re  # noqa: E402

_src = inspect.getsource(m)
_ctor = re.search(r"Settings\.llm = NVIDIA\((.*?)\n            \)", _src, re.S)
check("the LLM constructor call was found in source", _ctor is not None)
if _ctor:
    _args = _ctor.group(1)
    check("max_tokens is passed to the LLM constructor",
          "max_tokens=" in _args, f"args={_args[:200]!r}")
    check("it is wired to MAX_OUTPUT_TOKENS, not a literal",
          "max_tokens=MAX_OUTPUT_TOKENS" in _args.replace(" ", ""))
    check("timeout is still passed (the cap did not displace it)",
          "timeout=LLM_TIMEOUT" in _args.replace(" ", ""))

finish("test_resource_bounds")
