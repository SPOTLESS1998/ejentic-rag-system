"""Offline tests for eval_rag.py — the harness that grades everything else.

WHY THIS FILE EXISTS
--------------------
`eval_rag.py` produces the only automated verdict this system makes about its own
safety, and until now it had NO test of any kind. The suite that proves the
clearance boundary was itself unproven.

It also shipped a fail-OPEN bug. `_parse_verdict` salvaged a PASS out of any reply
containing "PASS" and not "FAIL", so a judge replying in prose —

    "The answer conveys none of the required facts and must not PASS."

— was scored as a PASS. Two properties made that worse than a stray branch:

  1. The judge runs on a FLOATING model alias (`...-latest`), so the reply FORMAT
     can change with no commit on our side. A permissive parser turns a
     provider-side change into a quietly greener report.
  2. run()'s scoring ladder tests `correctness == "FAIL"` and treats EVERYTHING
     else as a pass. So a verdict that merely fails to say FAIL is a pass by
     default — returning a third state from _parse_verdict is not enough on its
     own, it has to be routed through `judge_infra` to reach the errored branch.

Section 3 is the one that matters: it drives the real run() with a stubbed answer
and a stubbed judge, and asserts an unreadable grade lands as UNVERIFIED rather
than as a silent pass. Sections 1 and 2 pin the two halves separately.

Run:  venv/bin/python tests/test_eval_harness.py
"""
import asyncio
import json
import tempfile
from pathlib import Path

from harness import check, section, finish, import_main

main = import_main()          # sets RAG_OFFLINE, fake keys, temp audit DB
import eval_rag               # noqa: E402 - must follow harness's env setup


# --- stubs ------------------------------------------------------------------
class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeResp:
    """Shaped like an LLM response: `.message.content`, no usage block (so the
    TokenMeter falls back to its estimator, which is what we want offline)."""

    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeJudge:
    """Returns a canned reply. `calls` proves whether a retry actually happened."""

    def __init__(self, content):
        self.content = content
        self.calls = 0

    async def achat(self, messages):
        self.calls += 1
        return _FakeResp(self.content)


def _verdict(text):
    return eval_rag._parse_verdict(text)[0]


# --- 1. _parse_verdict fails CLOSED ----------------------------------------
section("1. _parse_verdict — reads a grade, or admits it cannot")

check("valid JSON PASS is honoured",
      _verdict('{"verdict":"PASS","reason":"all facts present"}') == "PASS")
check("valid JSON FAIL is honoured",
      _verdict('{"verdict":"FAIL","reason":"missing revenue"}') == "FAIL")
check("JSON inside a markdown fence still parses",
      _verdict('```json\n{"verdict":"PASS","reason":"ok"}\n```') == "PASS")
check("reason is carried through",
      eval_rag._parse_verdict('{"verdict":"FAIL","reason":"missing X"}')[1] == "missing X")
check("JSON with an empty reason gets a placeholder, not a crash",
      eval_rag._parse_verdict('{"verdict":"PASS","reason":""}')[1] == "(no reason given)")

# THE REGRESSION. This exact sentence was scored PASS before the fix.
_REFUSAL_PROSE = "The answer conveys none of the required facts and must not PASS."
check("REGRESSION: 'must not PASS' prose is NOT a pass",
      _verdict(_REFUSAL_PROSE) != "PASS",
      f"got {_verdict(_REFUSAL_PROSE)}")
check("REGRESSION: 'must not PASS' prose is reported UNREADABLE",
      _verdict(_REFUSAL_PROSE) == "ERROR")
check("REGRESSION: 'should PASS' prose is also not silently accepted",
      _verdict("I think this one should PASS.") == "ERROR")

# The FAIL salvage is deliberately KEPT — it only ever errs toward failing.
check("prose FAIL is still salvaged (errs toward failing, so it is safe)",
      _verdict("FAIL - the answer omits the acquisition price.") == "FAIL")
check("prose with BOTH words is unreadable, not a coin-flip",
      _verdict("Is this a PASS or a FAIL? Hard to say.") == "ERROR")
check("empty reply is unreadable",
      _verdict("") == "ERROR")
check("whitespace-only reply is unreadable",
      _verdict("   \n\t ") == "ERROR")
check("unrelated prose is unreadable",
      _verdict("The weather today is mild.") == "ERROR")
check("JSON with an out-of-vocabulary verdict is unreadable",
      _verdict('{"verdict":"MAYBE","reason":"unsure"}') == "ERROR")
check("malformed JSON is unreadable",
      _verdict('{"verdict":"PASS", oops}') == "ERROR")
check("unreadable reply quotes the offending text so a human can debug it",
      "weather" in eval_rag._parse_verdict("The weather today is mild.")[1])
check("PASS is never returned for any unreadable input",
      all(_verdict(t) != "PASS" for t in
          [_REFUSAL_PROSE, "", "   ", "should PASS", "PASS or FAIL?",
           '{"verdict":"MAYBE"}', '{"verdict":"PASS", oops}']))


# --- 2. _judge_with_retry routes an unreadable grade to UNVERIFIED ----------
section("2. _judge_with_retry — an unreadable grade is not a grade")

_CASE = {"question": "q", "must_include": ["a"], "id": "t", "expect": "answer"}


def _judge(content):
    judge = _FakeJudge(content)
    verdict, reason, infra = asyncio.run(
        eval_rag._judge_with_retry(judge, main.TokenMeter(), _CASE, "some answer",
                                   call_timeout=5, retries=2, backoff=0))
    return verdict, reason, infra, judge.calls


v, r, infra, calls = _judge('{"verdict":"PASS","reason":"ok"}')
check("a readable PASS returns no infra marker", v == "PASS" and infra is None)
check("a readable verdict is not retried", calls == 1, f"calls={calls}")

v, r, infra, calls = _judge('{"verdict":"FAIL","reason":"nope"}')
check("a readable FAIL returns no infra marker", v == "FAIL" and infra is None)

v, r, infra, calls = _judge(_REFUSAL_PROSE)
check("an unreadable reply sets the infra marker (-> errored branch)", bool(infra))
check("an unreadable reply does NOT return PASS", v != "PASS", f"got {v}")
check("the infra marker explains itself", "unreadable" in (infra or "").lower())
check("an unreadable reply is NOT retried (temp-0: same question, same answer)",
      calls == 1, f"calls={calls}")


# --- 3. END TO END: an unreadable grade cannot become a silent pass ---------
section("3. run() — the fail-closed property, end to end")

_PASSING_CASE = [{
    "id": "fixture-answerable", "dimension": "correctness", "role": "guest",
    "question": "What services are offered?", "expect": "answer",
    "must_include": ["consulting"], "forbidden": [],
}]
_ANSWER = "We offer consulting services. [Source 1]"


def _run_with_judge_reply(reply):
    """Drive the real run() with a stubbed answer and a stubbed judge."""
    tmp = Path(tempfile.mkdtemp())
    cases_path, out_path = tmp / "cases.json", tmp / "report.json"
    cases_path.write_text(json.dumps(_PASSING_CASE), encoding="utf-8")

    async def fake_answer_once(query, role, platform, *a, **kw):
        return _ANSWER, main.TokenMeter(), False, 0

    saved_answer, saved_index = main.answer_once, main.global_index
    saved_build = eval_rag._build_judge_llm
    main.answer_once = fake_answer_once
    main.global_index = object()                       # "index is up"
    eval_rag._build_judge_llm = lambda: _FakeJudge(reply)
    try:
        code = asyncio.run(eval_rag.run(
            str(cases_path), str(out_path), min_pass=1.0, use_judge=True,
            call_timeout=5, delay=0, retries=0, backoff=0))
        return code, json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        main.answer_once, main.global_index = saved_answer, saved_index
        eval_rag._build_judge_llm = saved_build


code, rep = _run_with_judge_reply('{"verdict":"PASS","reason":"ok"}')
case = rep["cases"][0]
check("control: a readable PASS over a good answer exits 0", code == 0, f"exit={code}")
check("control: that case is recorded as passed", case["passed"] is True)
check("control: that case is NOT errored", case["errored"] is False)

code, rep = _run_with_judge_reply(_REFUSAL_PROSE)
case = rep["cases"][0]
check("THE FIX: an unreadable grade does NOT pass the case",
      case["passed"] is False, f"passed={case['passed']}")
check("THE FIX: it is recorded as ERRORED (unverified), not failed-on-content",
      case["errored"] is True)
check("THE FIX: the run does not exit 0",
      code != 0, f"exit={code}")
check("THE FIX: it exits 4 = INCOMPLETE (distinct from a content failure)",
      code == 4, f"exit={code}")
check("THE FIX: summary counts it as errored",
      rep["summary"]["errored"] == 1)
check("an unverified case never reads as secure (pass_rate excludes it)",
      rep["summary"]["scored"] == 0)


# --- 4. the report records WHAT graded it ----------------------------------
section("4. report provenance — a grade whose grader is unrecorded is not evidence")

check("report records the resolved judge model",
      rep["summary"].get("judge_model") == eval_rag.JUDGE_MODEL)
check("report records the system LLM separately",
      rep["summary"].get("system_llm_model") == main.LLM_MODEL)
check("report records the confidence gate the run used",
      rep["summary"].get("confidence_threshold") == main.CONFIDENCE_THRESHOLD)
check("report flags a floating-alias judge as non-reproducible",
      rep["summary"].get("judge_model_is_alias") == eval_rag.JUDGE_MODEL.endswith("-latest"))
check("JUDGE_MODEL defaults to the system LLM (no behaviour change unless pinned)",
      eval_rag.JUDGE_MODEL == main.LLM_MODEL)

finish("test_eval_harness")
