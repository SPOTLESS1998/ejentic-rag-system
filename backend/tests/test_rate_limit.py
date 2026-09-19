"""Offline tests for the per-caller request ceilings.

WHY THIS FILE EXISTS
--------------------
Bounding the SIZE of one request (test_resource_bounds.py) does not bound the
COUNT of them. Before ratelimit.py there was no 429 path anywhere in this
backend: a valid key pointed at a loop could issue requests as fast as the
network delivered them, every one of them individually "within limits" while
the total was unbounded.

The two assertions that matter most are not "the limit works":

  * Section 5 — ORDERING. An UNAUTHENTICATED flood must consume none of the
    authenticated caller's allowance. If the limiter ran before auth, a keyless
    attacker could exhaust everyone else's budget: a denial of service delivered
    through the rate limiter itself. This is the mirror of the query-length
    check, which correctly runs BEFORE auth because it costs nothing to decide.

  * Section 4 — WIRING. Every assertion in sections 1-3 would still pass if the
    call to `_rate_limit` were deleted from the endpoints. Section 4 drives the
    real HTTP path and is the only thing here that would notice.

Run:  venv/bin/python tests/test_rate_limit.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (base_cfg, check, env, finish, import_main, section,  # noqa: E402
                     test_client)

import client_registry as registry  # noqa: E402
import ratelimit  # noqa: E402

m = import_main()

_GUEST_KEY = "a" * 64        # fake, correct shape; never a real credential
_EXEC_KEY = "b" * 64


def _admit(lim, bucket, now):
    """Admit one, reporting success as a bool instead of an exception."""
    try:
        lim.admit(bucket, now=now)
        return True
    except ratelimit.RateLimitError:
        return False


# ---------------------------------------------------------------------------
section("1. the window admits up to the limit, then refuses, then recovers")
# ---------------------------------------------------------------------------
lim = ratelimit.RateLimiter(per_minute=3, per_day=None)
check("requests under the limit are admitted",
      all(_admit(lim, "guest", now=100.0 + i) for i in range(3)))
check("the request that would breach the limit is refused",
      not _admit(lim, "guest", now=103.0))

try:
    lim.admit("guest", now=103.0)
    check("refusal raises RateLimitError", False, "nothing raised")
except ratelimit.RateLimitError as e:
    check("refusal raises RateLimitError", True)
    check("it carries HTTP 429", e.status == 429, f"status={e.status}")
    check("Retry-After is a whole number of seconds >= 1",
          isinstance(e.retry_after, int) and e.retry_after >= 1, f"={e.retry_after}")
    check("Retry-After never says 0 (which would invite a guaranteed-refused retry)",
          e.retry_after > 0)
    check("the message names the limit and the window, not our internals",
          "3" in e.detail and "minute" in e.detail and "Traceback" not in e.detail,
          f"detail={e.detail!r}")

# The window SLIDES: the oldest event ages out and room appears, without any
# calendar boundary to game.
check("still refused one second before the oldest event ages out",
      not _admit(lim, "guest", now=159.5))
check("admitted again once the oldest event has aged out",
      _admit(lim, "guest", now=160.5))

# A rolling window means there is no instant at which everyone's allowance
# resets at once — which is exactly what a calendar window would hand an abuser.
lim2 = ratelimit.RateLimiter(per_minute=2, per_day=None)
_admit(lim2, "g", now=0.0)
_admit(lim2, "g", now=59.0)
check("a burst straddling a would-be calendar boundary is still refused",
      not _admit(lim2, "g", now=59.5))

# Buckets are independent: one credential cannot spend another's allowance.
lim3 = ratelimit.RateLimiter(per_minute=2, per_day=None)
_admit(lim3, "guest", now=0.0)
_admit(lim3, "guest", now=0.1)
check("an exhausted bucket is refused", not _admit(lim3, "guest", now=0.2))
check("a DIFFERENT bucket is unaffected by it", _admit(lim3, "executive", now=0.2))


# ---------------------------------------------------------------------------
section("2. the two rules that are easy to get wrong")
# ---------------------------------------------------------------------------
# RULE 1: a refused request is NOT recorded. If refusals counted, a client stuck
# in a retry loop would extend its own lockout forever and never recover.
lim = ratelimit.RateLimiter(per_minute=2, per_day=None)
_admit(lim, "g", now=0.0)
_admit(lim, "g", now=0.0)
for i in range(20):
    _admit(lim, "g", now=1.0 + i)      # all refused
check("a hammering client does not extend its own lockout",
      _admit(lim, "g", now=60.5), "admitted as soon as the original two aged out")
check("refused attempts left no trace in the window",
      lim.snapshot("g", now=60.5)["per-minute"]["used"] == 1,
      f"snapshot={lim.snapshot('g', now=60.5)}")

# RULE 2: BOTH windows are checked before EITHER records. Recording window by
# window would let the minute window charge for a request the day window then
# refuses — budget spent on a request that never ran.
lim = ratelimit.RateLimiter(per_minute=10, per_day=2)
_admit(lim, "g", now=0.0)
_admit(lim, "g", now=0.0)
refused = not _admit(lim, "g", now=0.0)
snap = lim.snapshot("g", now=0.0)
check("the day window refuses once its (tighter) limit is reached", refused)
check("the minute window was NOT charged for the refused request",
      snap["per-minute"]["used"] == 2, f"snapshot={snap}")
check("both windows agree on what was actually spent",
      snap["per-minute"]["used"] == snap["per-day"]["used"], f"snapshot={snap}")

# When both windows would refuse, the Retry-After we hand back must be the TRUE
# one — the longer wait. Promising 1s when the real wait is 24h is a lie the
# client will act on.
lim = ratelimit.RateLimiter(per_minute=1, per_day=1)
_admit(lim, "g", now=0.0)
try:
    lim.admit("g", now=0.0)
    check("Retry-After reflects the LONGER of the two windows", False, "not refused")
except ratelimit.RateLimitError as e:
    check("Retry-After reflects the LONGER of the two windows",
          e.retry_after > 60 and e.window == "per-day",
          f"retry_after={e.retry_after}s window={e.window}")

# Memory is bounded by construction — the thing this module exists to guarantee
# must be true OF this module too.
lim = ratelimit.RateLimiter(per_minute=5, per_day=None)
for i in range(500):
    _admit(lim, "g", now=float(i) * 0.001)
check("the window log never grows past the limit, however many are refused",
      len(lim.windows[0]._log["g"]) <= 5, f"len={len(lim.windows[0]._log['g'])}")

# The bucket cap fails CLOSED. Evicting instead would reset the evicted bucket's
# limit, so an attacker cycling fake keys would get a fresh allowance each time.
_saved_cap = ratelimit.MAX_BUCKETS
try:
    ratelimit.MAX_BUCKETS = 3
    lim = ratelimit.RateLimiter(per_minute=100, per_day=None)
    admitted = [_admit(lim, f"bucket-{i}", now=0.0) for i in range(6)]
    check("new buckets are admitted up to the cap", all(admitted[:3]), f"{admitted}")
    check("past the cap we REFUSE rather than evict (no free allowance reset)",
          not any(admitted[3:]), f"{admitted}")
    check("an already-tracked bucket keeps working past the cap",
          _admit(lim, "bucket-0", now=0.0))
finally:
    ratelimit.MAX_BUCKETS = _saved_cap


# ---------------------------------------------------------------------------
section("3. the limits come from config (no business fact in code)")
# ---------------------------------------------------------------------------
check("max_requests_per_minute is in DEFAULT_CONFIG (every tenant inherits it)",
      "max_requests_per_minute" in registry.DEFAULT_CONFIG)
check("max_requests_per_day is in DEFAULT_CONFIG",
      "max_requests_per_day" in registry.DEFAULT_CONFIG)
_cfg = base_cfg()
check("a tenant config that never mentions the limits still gets them",
      bool(_cfg.get("max_requests_per_minute")) and bool(_cfg.get("max_requests_per_day")))

_live = ratelimit.limits_from_config(_cfg)
check("the active tenant's limiter is enabled", _live.enabled)
check("both windows are configured", len(_live.windows) == 2,
      f"windows={[w.label for w in _live.windows]}")

# The composition that turns a request cap into a SPEND cap. If either half is
# missing the arithmetic is meaningless, so pin that both exist together.
_worst = _cfg["max_requests_per_day"] * _cfg["max_output_tokens"]
check("worst-case daily completion spend is a finite, computable number",
      isinstance(_worst, int) and _worst > 0, f"{_cfg['max_requests_per_day']} req x "
      f"{_cfg['max_output_tokens']} tok = {_worst:,} tokens/day/role")

# null disables a window; 0 CLOSES it. A typo must not read as "unlimited".
check("null disables a window",
      len(ratelimit.limits_from_config(
          {"max_requests_per_minute": None, "max_requests_per_day": 10}).windows) == 1)
check("a tenant can disable rate limiting entirely with nulls",
      not ratelimit.limits_from_config(
          {"max_requests_per_minute": None, "max_requests_per_day": None}).enabled)
_zero = ratelimit.limits_from_config({"max_requests_per_minute": 0})
check("0 means CLOSED, not unlimited (a typo must not open the door)",
      not _admit(_zero, "g", now=0.0))

for bad in ("30", 3.5, True, []):
    try:
        ratelimit.limits_from_config({"max_requests_per_minute": bad})
        check(f"a non-integer limit ({bad!r}) is rejected at load", False, "accepted")
    except ValueError:
        check(f"a non-integer limit ({bad!r}) is rejected at load", True)


# ---------------------------------------------------------------------------
section("4. WIRING — the real endpoint actually returns 429")
# ---------------------------------------------------------------------------
# Everything above passes if the call to _rate_limit is deleted from the
# handlers. This section is the only thing that would notice.
_calls = {"answer": 0}


async def _spy_answer(*a, **kw):
    _calls["answer"] += 1
    return "an answer", m.TokenMeter(), False, 0


async def _spy_retrieve(*a, **kw):
    return [], 0.0, "q"


_tiny = ratelimit.RateLimiter(per_minute=2, per_day=None)
client, _mod, undo = test_client(answer_once=_spy_answer,
                                 retrieve_and_rerank=_spy_retrieve,
                                 LIMITER=_tiny)
try:
    with env(RAG_KEY_GUEST=_GUEST_KEY):
        h = {"X-API-Key": _GUEST_KEY}
        r1 = client.post("/api/rag", json={"query": "one"}, headers=h)
        r2 = client.post("/api/rag", json={"query": "two"}, headers=h)
        r3 = client.post("/api/rag", json={"query": "three"}, headers=h)

        check("requests within the limit are served", r1.status_code == 200
              and r2.status_code == 200, f"{r1.status_code},{r2.status_code}")
        check("the request over the limit is refused with 429",
              r3.status_code == 429, f"status={r3.status_code}")
        check("the 429 carries a Retry-After header the client can honour",
              r3.headers.get("Retry-After", "").isdigit(),
              f"Retry-After={r3.headers.get('Retry-After')!r}")
        check("the refused request cost NOTHING — no answer was generated",
              _calls["answer"] == 2, f"answer calls={_calls['answer']}")
        check("the 429 body explains the limit without leaking internals",
              "rate limit" in r3.text.lower() and "Traceback" not in r3.text)

        # The streaming endpoint is a separate handler and needs its own proof —
        # a guard wired into one path and not the other is the classic near-miss.
        r4 = client.post("/chat", json={"query": "four"}, headers=h)
        check("/chat is limited too, and fails as a readable error not a stream",
              r4.status_code == 429, f"status={r4.status_code}")

    # Roles get their own allowance, so a compromised guest key cannot starve the
    # executive tier it shares an instance with.
    with env(RAG_KEY_GUEST=_GUEST_KEY, RAG_KEY_EXECUTIVE=_EXEC_KEY):
        r5 = client.post("/api/rag", json={"query": "five"},
                         headers={"X-API-Key": _EXEC_KEY})
        check("an exhausted guest allowance does not starve the executive role",
              r5.status_code == 200, f"status={r5.status_code}")
finally:
    undo()


# ---------------------------------------------------------------------------
section("5. ORDERING — an unauthenticated flood consumes NO allowance")
# ---------------------------------------------------------------------------
# The subtle failure this rules out: if the limiter ran BEFORE auth, it would
# have to key on something the caller controls, and a keyless attacker could
# then exhaust the legitimate users' budget — a denial of service delivered
# through the rate limiter. Auth first means a bad key is rejected for free and
# charged to nobody.
_calls2 = {"answer": 0}


async def _spy_answer2(*a, **kw):
    _calls2["answer"] += 1
    return "an answer", m.TokenMeter(), False, 0


_tiny2 = ratelimit.RateLimiter(per_minute=2, per_day=None)
client, _mod, undo = test_client(answer_once=_spy_answer2,
                                 retrieve_and_rerank=_spy_retrieve,
                                 LIMITER=_tiny2)
try:
    with env(RAG_KEY_GUEST=_GUEST_KEY):
        bad = [client.post("/api/rag", json={"query": "flood"},
                           headers={"X-API-Key": "c" * 64}).status_code
               for _ in range(25)]
        none = [client.post("/api/rag", json={"query": "flood"}).status_code
                for _ in range(25)]
        check("a wrong key is rejected 401, never 429", set(bad) == {401}, f"{set(bad)}")
        check("a missing key is rejected 401, never 429",
              set(none) == {401}, f"{set(none)}")
        check("the flood generated no answers", _calls2["answer"] == 0,
              f"answer calls={_calls2['answer']}")

        # 50 rejected requests later, the legitimate caller still has its FULL
        # allowance. This is the assertion the whole section exists for.
        h = {"X-API-Key": _GUEST_KEY}
        after = [client.post("/api/rag", json={"query": "mine"},
                             headers=h).status_code for _ in range(2)]
        check("ORDERING: the real caller's full allowance survived the flood",
              after == [200, 200], f"{after}")
        check("...and the limit still applies to it afterwards",
              client.post("/api/rag", json={"query": "mine"},
                          headers=h).status_code == 429)
finally:
    undo()

finish("test_rate_limit")
