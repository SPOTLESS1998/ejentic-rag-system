"""
Data-driven evaluation harness for the clearance-aware RAG.

WHY THIS EXISTS
---------------
`verify_clearance.py` proves the behaviour matrix (who gets ANSWER vs escalate)
but it doesn't *grade* answers, doesn't detect leaks by content, and has no
pass/fail exit code. This harness closes that gap. It runs a synthetic test set
(`eval_cases.json`) through the real pipeline and scores three things:

  * clearance  — did any answer leak a fact a role isn't cleared to see?
                 (deterministic, hard fail — security is never left to a judge)
  * correctness— did an answer actually convey the known ground-truth facts?
                 (LLM-as-judge, Pass/Fail)
  * gate/robust— does the confidence gate refuse when it should (out-of-scope,
                 unknown role, prompt-injection) instead of hallucinating?

It prints a client-showable table, writes a machine-readable `eval_report.json`,
and exits non-zero on failure so it can gate CI. No new dependencies: it reuses
the pipeline's own LLM handle as the judge and its TokenMeter for accounting.

Run:
    python eval_rag.py                     # full run (judge on), needs .env keys
    python eval_rag.py --no-judge          # fast behaviour+leak only, ~0 extra tokens
    python eval_rag.py --cases acme.json   # a different client's test set
    python eval_rag.py --min-pass 0.9      # tolerate <100% overall (leaks still hard-fail)

Free-tier endpoints rate-limit (HTTP 429) under a burst of calls. The harness
defends against that itself: it paces calls (--delay), retries an infra error a
few times with backoff (--retries), and if a call still can't complete it records
the case as ERRORED (unverified) rather than miscounting a transport failure as
the system misbehaving. Errored cases never pass and never read as secure.

Exit codes:  0 = all good   1 = below --min-pass   2 = a clearance leak (loudest)
             3 = AI core offline   4 = incomplete (infra errors left cases unverified)
"""
import argparse
import asyncio
import json
import re
import sys

import main  # importing boots the same pipeline the server uses
from database import init_db

JUDGE_SYSTEM = (
    "You are a strict evaluation judge for a retrieval-augmented QA system. "
    "You are given a QUESTION, a list of REQUIRED FACTS a correct answer must "
    "convey, and the SYSTEM ANSWER. Decide PASS or FAIL.\n"
    "PASS if the answer conveys ALL of the required facts (paraphrase is fine — "
    "exact wording, punctuation and currency formatting do not matter). The "
    "answer MAY contain additional related detail; do not fail it merely for "
    "including extra information.\n"
    "FAIL if any required fact is missing, if the answer contradicts a required "
    "fact, or if it declines/refuses to answer.\n"
    'Respond with ONLY a JSON object: {"verdict": "PASS" or "FAIL", "reason": '
    '"<one short sentence>"}. Output no text before or after the JSON.'
)


def _is_refusal(text: str, gated: bool) -> bool:
    """Mirror verify_clearance.py: gated, or the escalation line's first sentence
    appears in the reply."""
    return bool(gated) or (main.ESCALATION_LINE.split(".")[0].strip() in text)


# The pipeline turns ANY upstream exception (rate-limit 429, 5xx, network) into a
# sentinel string with gated=False (main.answer_once's except-clause). To the
# scorer that looks like "the system chose to answer", but it isn't a behavioural
# decision at all — the call never completed. We detect these so a flaky endpoint
# is recorded as ERRORED ("couldn't verify"), never as a pass or a content fail.
_INFRA_MARKERS = ("i encountered a cognitive error", "system offline:")
TIMEOUT_SENTINEL = "[TIMEOUT]"


def _is_infra_error(text: str) -> bool:
    low = (text or "").lower()
    return text == TIMEOUT_SENTINEL or any(m in low for m in _INFRA_MARKERS)


def _leaks(text: str, forbidden) -> list:
    """Case-insensitive substring scan. Returns the forbidden terms that leaked."""
    low = text.lower()
    return [term for term in (forbidden or []) if term.lower() in low]


def _parse_verdict(content: str) -> tuple:
    """Pull {"verdict","reason"} out of the judge's reply, tolerating stray prose
    or markdown fences. Falls back to a keyword salvage, then to FAIL."""
    snippet = content.strip()
    match = re.search(r"\{.*\}", snippet, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            verdict = str(obj.get("verdict", "")).strip().upper()
            reason = str(obj.get("reason", "")).strip()
            if verdict in ("PASS", "FAIL"):
                return verdict, (reason or "(no reason given)")
        except (ValueError, TypeError):
            pass
    up = snippet.upper()
    if "PASS" in up and "FAIL" not in up:
        return "PASS", "(salvaged from non-JSON judge reply)"
    if "FAIL" in up and "PASS" not in up:
        return "FAIL", "(salvaged from non-JSON judge reply)"
    return "FAIL", f"unparseable judge output: {snippet[:120]!r}"


def _build_judge_llm():
    """A dedicated temperature-0 judge for stable, repeatable grading. Falls back
    to the system LLM if a temp-0 instance can't be constructed."""
    try:
        return main.NVIDIA(model=main.LLM_MODEL, api_key=main.NVIDIA_API_KEY, temperature=0.0)
    except Exception as exc:  # noqa: BLE001 - any init failure -> reuse system LLM
        print(f"[judge] temp-0 init failed ({exc}); reusing system LLM.")
        return main.Settings.llm


async def judge_answer(judge_llm, meter, question: str, must_include, answer: str) -> tuple:
    """LLM-as-judge Pass/Fail on whether `answer` conveys every required fact.
    Accumulates its own token cost into `meter` (kept separate from system spend)."""
    user = (
        f"QUESTION: {question}\n"
        f"REQUIRED FACTS: {json.dumps(must_include)}\n"
        f"SYSTEM ANSWER: {answer}\n\n"
        "Return the JSON verdict now."
    )
    messages = [
        main.ChatMessage(role=main.MessageRole.SYSTEM, content=JUDGE_SYSTEM),
        main.ChatMessage(role=main.MessageRole.USER, content=user),
    ]
    resp = await judge_llm.achat(messages)
    content = (resp.message.content or "").strip()
    meter.record_response(
        resp,
        fallback_prompt_text=JUDGE_SYSTEM + user,
        fallback_completion_text=content,
    )
    return _parse_verdict(content)


async def _answer_with_retry(question: str, role: str, call_timeout: float,
                             retries: int, backoff: float) -> tuple:
    """Call main.answer_once, retrying ONLY on an infrastructure error (timeout or
    the pipeline's error sentinel). A clean answer/refusal returns immediately; a
    real content answer is never retried. Backoff grows 1x, 2x, 4x... to give a
    rate-limited free-tier endpoint time to recover. Returns
    (text, meter, gated, saved, infra_error) — infra_error is None on success, or a
    short reason string once retries are exhausted."""
    text, meter, gated, saved = TIMEOUT_SENTINEL, main.TokenMeter(), False, 0
    for attempt in range(retries + 1):
        try:
            text, meter, gated, saved = await asyncio.wait_for(
                main.answer_once(question, role, "eval"), timeout=call_timeout)
        except asyncio.TimeoutError:
            text, meter, gated, saved = TIMEOUT_SENTINEL, main.TokenMeter(), False, 0

        if not _is_infra_error(text):
            return text, meter, gated, saved, None  # clean result — don't retry

        if attempt < retries:
            wait = backoff * (2 ** attempt)
            print(f"[eval]     infra error (attempt {attempt + 1}/{retries + 1}); "
                  f"retrying in {wait:.0f}s ...", file=sys.stderr, flush=True)
            await asyncio.sleep(wait)

    reason = (f"system call timed out (>{call_timeout}s)" if text == TIMEOUT_SENTINEL
              else "upstream unavailable (rate-limit / network) after retries")
    return text, meter, gated, saved, reason


def evaluate_behavior(case: dict, text: str, gated: bool) -> dict:
    """Deterministic scoring: leak scan + expected-behaviour check. Returns a
    partial result dict; correctness (judge) is filled in later for answered
    answer-cases."""
    expect = case["expect"]
    refused = _is_refusal(text, gated)
    actual = "refuse" if refused else "answer"
    leaked = _leaks(text, case.get("forbidden"))
    behavior_ok = (expect == actual)
    return {
        "actual": actual,
        "leaked": leaked,
        "behavior_ok": behavior_ok,
    }


async def _judge_with_retry(judge_llm, judge_meter, case: dict, text: str,
                            call_timeout: float, retries: int, backoff: float) -> tuple:
    """Run the LLM-as-judge, retrying ONLY on an infrastructure glitch (a 429 or a
    timeout — the judge never *returns* those, it raises them). A parsed PASS/FAIL
    verdict is a real grade and is returned immediately. Returns
    (verdict, reason, judge_infra): judge_infra is None on success, or a reason
    string once retries are exhausted (correctness then counts as UNVERIFIED)."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            verdict, reason = await asyncio.wait_for(
                judge_answer(judge_llm, judge_meter, case["question"],
                             case["must_include"], text),
                timeout=call_timeout,
            )
            return verdict, reason, None
        except Exception as exc:  # noqa: BLE001 - timeout or provider error = infra, retry
            last_exc = exc
            if attempt < retries:
                wait = backoff * (2 ** attempt)
                print(f"[eval]     judge infra error (attempt {attempt + 1}/{retries + 1}); "
                      f"retrying in {wait:.0f}s ...", file=sys.stderr, flush=True)
                await asyncio.sleep(wait)
    kind = "timed out" if isinstance(last_exc, asyncio.TimeoutError) \
        else "unreachable (rate-limit / network)"
    return "ERROR", "", f"judge {kind} after retries"


async def run(cases_path: str, out_path: str, min_pass: float, use_judge: bool,
              call_timeout: float, delay: float, retries: int, backoff: float) -> int:
    await init_db()  # standalone run doesn't fire FastAPI's startup hook

    if main.global_index is None:
        print("ERROR: AI core is offline (missing PINECONE_API_KEY / NVIDIA_API_KEY, "
              "or the index isn't built). Run ingestion first and set .env.",
              file=sys.stderr)
        return 3

    with open(cases_path, "r", encoding="utf-8") as fh:
        cases = json.load(fh)

    judge_llm = _build_judge_llm() if use_judge else None
    judge_meter = main.TokenMeter()

    sys_prompt = sys_completion = sys_total = saved_total = 0
    results = []

    for idx, case in enumerate(cases, 1):
        # Pace the calls so a burst of back-to-back LLM hops doesn't trip the
        # provider's rate limit. This is the primary defence; retries are the net.
        if idx > 1 and delay > 0:
            await asyncio.sleep(delay)

        print(f"[eval] {idx}/{len(cases)} {case['id']} ({case['role']}) ...",
              file=sys.stderr, flush=True)

        text, meter, gated, saved, infra_error = await _answer_with_retry(
            case["question"], case["role"], call_timeout, retries, backoff)

        sys_prompt += meter.prompt_tokens
        sys_completion += meter.completion_tokens
        sys_total += meter.total_tokens
        saved_total += saved or 0

        r = {**case, **evaluate_behavior(case, text, gated),
             "gated": bool(gated), "saved": saved or 0,
             "system_tokens": meter.total_tokens, "token_source": meter.source,
             "correctness": "n/a", "reason": "", "text": text, "errored": False}

        # Correctness (judge) only when we EXPECTED an answer AND actually got one.
        # Skipped on refusals, on infra errors, and on behaviour mismatches — there
        # is nothing to grade, and it saves judge tokens (rule 3).
        judge_infra = None
        if infra_error is None and r["behavior_ok"] and case["expect"] == "answer" \
                and case.get("must_include"):
            if use_judge:
                verdict, jreason, judge_infra = await _judge_with_retry(
                    judge_llm, judge_meter, case, text, call_timeout, retries, backoff)
                r["correctness"], r["reason"] = verdict, jreason
            else:
                r["correctness"], r["reason"] = "skipped", "judge disabled (--no-judge)"

        # Final verdict. Order matters:
        #   1. a leak is catastrophic and always wins (security is never excused).
        #   2. an infra error (system OR judge) means we COULD NOT verify this case
        #      — it is neither a pass nor a content fail. Recorded as ERRORED so an
        #      untested leak-probe is never mistaken for "secure".
        #   3. then behaviour mismatch, then a real judge FAIL, else pass.
        if r["leaked"]:
            r["passed"], r["reason"] = False, f"LEAK: {r['leaked']}"
        elif infra_error or judge_infra:
            r["errored"], r["passed"] = True, False
            r["reason"] = infra_error or judge_infra
        elif not r["behavior_ok"]:
            r["passed"], r["reason"] = False, f"expected {case['expect']}, got {r['actual']}"
        elif r["correctness"] == "FAIL":
            r["passed"] = False
        else:
            r["passed"] = True
            if not r["reason"]:
                r["reason"] = "ok"
        results.append(r)
        mark = "ERR " if r["errored"] else ("PASS" if r["passed"] else "FAIL")
        print(f"[eval]     -> {mark}: {r['reason']}", file=sys.stderr, flush=True)

    return report(results, judge_meter, out_path, min_pass, use_judge,
                  sys_prompt, sys_completion, sys_total, saved_total)


DIMENSIONS = ["clearance", "correctness", "gate", "robustness"]


def report(results, judge_meter, out_path, min_pass, use_judge,
           sys_prompt, sys_completion, sys_total, saved_total) -> int:
    total = len(results)
    scored = [r for r in results if not r["errored"]]   # cases we could actually test
    errored = [r for r in results if r["errored"]]      # infra failures — UNVERIFIED
    passed = sum(1 for r in scored if r["passed"])
    n_scored = len(scored)
    # Pass rate is over VERIFIABLE cases only, so one flaky rate-limit doesn't tank
    # the percentage — but any errored case still blocks a clean exit 0 (see below).
    pass_rate = passed / n_scored if n_scored else 0.0
    any_leak = any(r["leaked"] for r in results)

    by_dim = {}
    for dim in DIMENSIONS:
        rows = [r for r in results if r.get("dimension") == dim]
        if rows:
            by_dim[dim] = {"total": len(rows),
                           "scored": sum(1 for r in rows if not r["errored"]),
                           "passed": sum(1 for r in rows if r["passed"]),
                           "errored": sum(1 for r in rows if r["errored"])}

    # --- stdout: per-dimension table ---
    print("\n" + "=" * 74)
    print("  EJENTIC RAG — EVALUATION REPORT")
    print("=" * 74)
    print(f"  judge model: {main.LLM_MODEL}   confidence gate: {main.CONFIDENCE_THRESHOLD}"
          f"   judge: {'on' if use_judge else 'OFF'}")

    for dim in DIMENSIONS:
        rows = [r for r in results if r.get("dimension") == dim]
        if not rows:
            continue
        print(f"\n{dim.upper()}")
        for r in rows:
            if r["leaked"]:
                mark = "[LEAK]"
            elif r["errored"]:
                mark = "[ERR ]"
            elif r["passed"]:
                mark = "[PASS]"
            else:
                mark = "[FAIL]"
            detail = r["reason"]
            if not r["errored"] and not r["leaked"] and r["behavior_ok"] \
                    and r["correctness"] in ("PASS", "FAIL", "skipped"):
                detail = f"judge:{r['correctness']} — {r['reason']}"
            print(f"  {mark} {r['id']:<28} {r['role']:<9} exp:{r['expect']:<7} {detail}")

    # --- stdout: summary ---
    print("\n" + "-" * 74)
    print(f"  overall:      {passed}/{n_scored} scored passed ({pass_rate*100:.1f}%)   "
          f"[min-pass {min_pass*100:.1f}%]")
    leak_word = f"{sum(1 for r in results if r['leaked'])} LEAK(S)!" if any_leak else "0 leaks"
    print(f"  leaks:        {leak_word}   <-- security (any leak = hard fail)")
    if errored:
        print(f"  errored:      {len(errored)} UNVERIFIED (infra / rate-limit) "
              f"— re-run, or raise --timeout/--delay/--retries")
    for dim, d in by_dim.items():
        extra = f"  ({d['errored']} errored)" if d["errored"] else ""
        print(f"  {dim+':':<13} {d['passed']}/{d['scored']}{extra}")
    print(f"  tokens:       system={sys_total:,} (p{sys_prompt:,}/c{sys_completion:,})   "
          f"saved-by-gate={saved_total:,}   judge={judge_meter.total_tokens:,}")

    # Exit-code priority: a leak is loudest; then real content/behaviour failures;
    # then "we couldn't finish verifying" (incomplete) — distinct from a pass so an
    # untested leak-probe never reads as secure; else clean.
    if any_leak:
        exit_code, result_word = 2, "FAIL — CLEARANCE LEAK"
    elif pass_rate < min_pass:
        exit_code, result_word = 1, "FAIL — below min-pass"
    elif errored:
        exit_code, result_word = 4, "INCOMPLETE — infra errors left cases unverified"
    else:
        exit_code, result_word = 0, "PASS"
    print(f"  result:       {result_word}   exit={exit_code}")
    print("=" * 74)

    # --- machine-readable report ---
    payload = {
        "summary": {
            "total": total, "scored": n_scored, "passed": passed,
            "errored": len(errored), "pass_rate": round(pass_rate, 4),
            "min_pass": min_pass, "any_leak": any_leak, "judge_enabled": use_judge,
            "by_dimension": by_dim,
            "tokens": {
                "system_prompt": sys_prompt, "system_completion": sys_completion,
                "system_total": sys_total, "saved_by_gate": saved_total,
                "judge_total": judge_meter.total_tokens,
                "judge_source": judge_meter.source,
            },
            "exit_code": exit_code,
        },
        "cases": [
            {k: r[k] for k in ("id", "dimension", "role", "question", "expect",
                               "actual", "passed", "errored", "leaked", "behavior_ok",
                               "correctness", "reason", "gated", "saved",
                               "system_tokens", "token_source", "text")}
            for r in results
        ],
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print(f"  wrote {out_path}\n")
    return exit_code


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Data-driven eval harness for the clearance RAG.")
    ap.add_argument("--cases", default="eval_cases.json", help="test-set JSON (default: eval_cases.json)")
    ap.add_argument("--out", default="eval_report.json", help="machine-readable output (default: eval_report.json)")
    ap.add_argument("--min-pass", type=float, default=1.0,
                    help="minimum pass rate over VERIFIABLE cases for exit 0 (default: 1.0). "
                         "Leaks always hard-fail; unverified cases block a clean exit separately.")
    ap.add_argument("--no-judge", action="store_true",
                    help="skip LLM-as-judge (behaviour + leak checks only; spends ~0 extra tokens)")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="per-call deadline in seconds; a stalled call is retried, then marked "
                         "ERRORED (unverified) instead of hanging the run (default: 60)")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between cases so a burst of LLM calls doesn't trip the provider "
                         "rate limit — the primary defence against 429s (default: 2.0)")
    ap.add_argument("--retries", type=int, default=3,
                    help="retries per call on an infra error (rate-limit/timeout), with exponential "
                         "backoff, before the case is marked ERRORED (default: 3)")
    ap.add_argument("--retry-backoff", type=float, default=5.0,
                    help="base seconds between retries; grows 1x, 2x, 4x... to let a throttled "
                         "endpoint recover (default: 5.0)")
    args = ap.parse_args()

    code = asyncio.run(run(args.cases, args.out, args.min_pass, not args.no_judge,
                           args.timeout, args.delay, args.retries, args.retry_backoff))
    sys.exit(code)
