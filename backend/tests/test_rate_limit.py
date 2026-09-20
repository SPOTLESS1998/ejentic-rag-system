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
          e.retry_after > 60 and e.window.endswith("per-day"),
          f"retry_after={e.retry_after}s window={e.window}")
    check("the window label names the TIER that refused, not just the window",
          e.window == "credential:per-day", f"window={e.window}")

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

# --- env overrides -------------------------------------------------------
# These are the numbers most likely to need changing in a hurry: a limit set too
# tight is indistinguishable from an outage to the people hitting it. They must
# be changeable from the deployment's env file, not only from a JSON file inside
# a deployed copy that would need a rebuild.
_cfgd = {"max_requests_per_minute": 30, "max_requests_per_day": 500}
with env(MAX_REQUESTS_PER_MINUTE="7"):
    check("MAX_REQUESTS_PER_MINUTE overrides the config value",
          ratelimit.limits_from_config(_cfgd).windows[0].limit == 7)
with env(MAX_REQUESTS_PER_DAY="4000"):
    _l = ratelimit.limits_from_config(_cfgd)
    check("MAX_REQUESTS_PER_DAY overrides the config value",
          _l.windows[1].limit == 4000, f"={_l.windows[1].limit}")
    check("overriding one window leaves the other on its configured value",
          _l.windows[0].limit == 30, f"={_l.windows[0].limit}")

# FAIL-CLOSED ON ABSENCE. An unset or blank variable must mean "use the config",
# never "no limit" — "absent = disabled" is how a cost control quietly stops
# existing. Turning it off takes an explicit word.
with env(MAX_REQUESTS_PER_MINUTE=None, MAX_REQUESTS_PER_DAY=None):
    check("an UNSET override falls back to config, it does not disable",
          ratelimit.limits_from_config(_cfgd).windows[0].limit == 30)
with env(MAX_REQUESTS_PER_MINUTE="   "):
    check("a BLANK override falls back to config, it does not disable",
          ratelimit.limits_from_config(_cfgd).windows[0].limit == 30)
with env(MAX_REQUESTS_PER_MINUTE="off"):
    _l = ratelimit.limits_from_config(_cfgd)
    check("disabling a window takes the explicit word 'off'",
          [w.label for w in _l.windows] == ["per-day"],
          f"windows={[w.label for w in _l.windows]}")
with env(MAX_REQUESTS_PER_MINUTE="lots"):
    try:
        ratelimit.limits_from_config(_cfgd)
        check("an unreadable override is an error, not a silent default", False,
              "accepted")
    except ValueError as e:
        check("an unreadable override is an error, not a silent default", True)
        check("...and the error says what to write instead",
              "off" in str(e) and "integer" in str(e), f"{e}")
with env(MAX_REQUESTS_PER_MINUTE="0"):
    check("0 via env still means CLOSED, not unlimited",
          not _admit(ratelimit.limits_from_config(_cfgd), "g", now=0.0))


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

# ---------------------------------------------------------------------------
section("6. the per-VISITOR share, nested inside the credential ceiling")
# ---------------------------------------------------------------------------
# The problem this solves: the public UI holds ONE shared guest key, so keying
# only by credential makes max_requests_per_day the whole site's daily budget
# rather than one visitor's.


def _tier(ceil_min=None, ceil_day=None, vis_min=None, vis_day=None):
    return ratelimit.TieredRateLimiter(
        ceiling=ratelimit.RateLimiter(ceil_min, ceil_day, tier="credential"),
        share=ratelimit.RateLimiter(vis_min, vis_day, fail_open=True, tier="visitor"))


def _admit_t(t, role, visitor=None, now=0.0):
    try:
        t.admit(role, visitor, now=now)
        return True
    except ratelimit.RateLimitError:
        return False


# One visitor exhausting its share must NOT lock out the others.
t = _tier(ceil_min=100, vis_min=3)
check("a visitor is served up to its own share",
      all(_admit_t(t, "guest", "alice", now=i * 0.1) for i in range(3)))
check("that visitor is then refused", not _admit_t(t, "guest", "alice", now=0.4))
check("A DIFFERENT visitor on the SAME key is unaffected",
      _admit_t(t, "guest", "bob", now=0.4))
check("...and a third, proving the key's budget was not consumed by alice",
      _admit_t(t, "guest", "carol", now=0.4))

# The ceiling still binds. This is what stops the forgeable visitor id becoming
# an escape hatch.
t = _tier(ceil_min=5, vis_min=2)
for i in range(5):
    _admit_t(t, "guest", f"visitor-{i}", now=0.0)   # 5 distinct visitors, 1 each
check("the CREDENTIAL ceiling still refuses once the key's total is reached",
      not _admit_t(t, "guest", "someone-new", now=0.0))
check("rotating visitor ids does NOT buy more than the ceiling allows",
      not _admit_t(t, "guest", "another-fresh-id", now=0.0))

# WHICH tier refused has to be distinguishable — they mean different things. A
# credential refusal affects everyone on that key; a visitor refusal affects one.
t = _tier(ceil_min=100, vis_min=1)
_admit_t(t, "guest", "alice", now=0.0)
try:
    t.admit("guest", "alice", now=0.0)
    check("a visitor-tier refusal is reported as such", False, "not refused")
except ratelimit.RateLimitError as e:
    check("a visitor-tier refusal is reported as such",
          e.window.startswith("visitor:"), f"window={e.window}")
    check("its message says the limit is per visitor, not per credential",
          "visitor" in e.detail, f"detail={e.detail!r}")

t = _tier(ceil_min=1, vis_min=100)
_admit_t(t, "guest", "alice", now=0.0)
try:
    t.admit("guest", "bob", now=0.0)
    check("a ceiling refusal is reported as credential-tier", False, "not refused")
except ratelimit.RateLimitError as e:
    check("a ceiling refusal is reported as credential-tier",
          e.window.startswith("credential:"), f"window={e.window}")

# The ceiling is the more serious condition, so it must win the report when both
# would refuse — its Retry-After is the one that is actually true for the caller.
t = _tier(ceil_min=1, vis_min=1)
_admit_t(t, "guest", "alice", now=0.0)
try:
    t.admit("guest", "alice", now=0.0)
    check("when BOTH tiers refuse, the ceiling is reported", False, "not refused")
except ratelimit.RateLimitError as e:
    check("when BOTH tiers refuse, the ceiling is reported",
          e.window.startswith("credential:"), f"window={e.window}")

# ATOMICITY ACROSS TIERS — same rule as across windows. Charging the ceiling for
# a request the visitor tier then refuses spends budget on a request never run.
t = _tier(ceil_min=100, vis_min=1)
_admit_t(t, "guest", "alice", now=0.0)
before = t.snapshot("guest", "alice", now=0.0)["credential"]["per-minute"]["used"]
_admit_t(t, "guest", "alice", now=0.0)          # refused by the visitor tier
after = t.snapshot("guest", "alice", now=0.0)["credential"]["per-minute"]["used"]
check("a visitor-tier refusal does NOT charge the credential ceiling",
      before == after == 1, f"before={before} after={after}")

# Buckets are namespaced by role, so the same visitor id under two roles is two
# buckets — otherwise a guest could drain an executive's visitor allowance.
t = _tier(ceil_min=100, vis_min=1)
_admit_t(t, "guest", "same-id", now=0.0)
check("the same visitor id under a DIFFERENT role is a different bucket",
      _admit_t(t, "executive", "same-id", now=0.0))

# No id presented => ceiling only. The honest fallback: we cannot fairly share
# what we cannot distinguish, and refusing would punish the caller for metadata
# we failed to supply.
t = _tier(ceil_min=3, vis_min=1)
check("with NO visitor id, only the ceiling applies",
      all(_admit_t(t, "guest", None, now=i * 0.1) for i in range(3)))
check("...and the ceiling still stops it", not _admit_t(t, "guest", None, now=0.4))


# ---------------------------------------------------------------------------
section("7. the visitor id is caller-supplied, so it is sanitised")
# ---------------------------------------------------------------------------
check("a normal id survives", ratelimit.clean_visitor_id("abc-123_x.y") == "abc-123_x.y")
check("surrounding whitespace is stripped",
      ratelimit.clean_visitor_id("  abc  ") == "abc")
for bad, why in [(None, "absent"), ("", "empty"), ("   ", "whitespace only"),
                 ("a" * 65, "too long"), ("has space", "space"),
                 ("semi;colon", "punctuation"), ("nul\x00byte", "NUL"),
                 ("new\nline", "CRLF"), ("../../etc", "traversal"),
                 ("<script>", "HTML"), ("emoji😀", "non-ASCII"),
                 ("a\x00b", "separator char we join on")]:
    check(f"rejected: {why}", ratelimit.clean_visitor_id(bad) is None,
          f"got={ratelimit.clean_visitor_id(bad)!r}")
check("exactly 64 chars is accepted (boundary)",
      ratelimit.clean_visitor_id("a" * 64) == "a" * 64)
check("65 is rejected, NOT truncated — a truncated id would merge two visitors",
      ratelimit.clean_visitor_id("a" * 65) is None)

# A rejected id degrades to ceiling-only rather than refusing the request.
t = _tier(ceil_min=5, vis_min=1)
check("an UNUSABLE visitor id falls back to ceiling-only, it does not refuse",
      _admit_t(t, "guest", "bad id!", now=0.0)
      and _admit_t(t, "guest", "also bad!", now=0.1))

# The \x00 join must be unambiguous: no sanitised id can contain it, so
# "role" + id can never be confused with a different (role, id) pair.
check("the namespace separator cannot appear in a sanitised id",
      ratelimit.clean_visitor_id("a\x00b") is None)


# ---------------------------------------------------------------------------
section("8. the two tiers fail in OPPOSITE directions at the bucket cap")
# ---------------------------------------------------------------------------
# The ceiling is the cost guarantee, so an untracked bucket there means we cannot
# bound spend => refuse. The share tier only refines fairness, and the ceiling is
# still enforced beneath it, so refusing legitimate visitors there would be a
# self-inflicted outage => allow, degrading to ceiling-only.
_saved = ratelimit.MAX_BUCKETS
try:
    ratelimit.MAX_BUCKETS = 3
    ceil = ratelimit.RateLimiter(100, None, fail_open=False, tier="credential")
    for i in range(3):
        ceil.admit(f"role-{i}", now=0.0)
    try:
        ceil.admit("role-overflow", now=0.0)
        check("CEILING tier fails CLOSED past the bucket cap", False, "admitted")
    except ratelimit.RateLimitError:
        check("CEILING tier fails CLOSED past the bucket cap", True)

    share = ratelimit.RateLimiter(100, None, fail_open=True, tier="visitor")
    for i in range(3):
        share.admit(f"v-{i}", now=0.0)
    try:
        share.admit("v-overflow", now=0.0)
        check("SHARE tier fails OPEN past the bucket cap "
              "(ceiling still applies, so fairness degrades, cost does not)", True)
    except ratelimit.RateLimitError:
        check("SHARE tier fails OPEN past the bucket cap", False, "refused")

    # And end-to-end: flooding distinct visitor ids must not be able to lock the
    # site out — it must only ever hit the ceiling.
    t = _tier(ceil_min=500, vis_min=1)
    outcomes = [_admit_t(t, "guest", f"flood-{i}", now=0.0) for i in range(40)]
    check("a visitor-id flood never produces a share-tier lockout",
          all(outcomes), f"refused={outcomes.count(False)}")
finally:
    ratelimit.MAX_BUCKETS = _saved

# Empty buckets are swept, so visitor memory tracks ACTIVE visitors rather than
# every visitor ever seen — the tier is keyed by something unbounded over time.
_saved = ratelimit.MAX_BUCKETS
try:
    ratelimit.MAX_BUCKETS = 20
    w = ratelimit._Window(2, 60.0, "per-minute", fail_open=True)
    for i in range(15):
        w.would_refuse(f"old-{i}", 0.0)
        w.record(f"old-{i}", 0.0)
    grew = len(w._log)
    w.would_refuse("later", 10_000.0)      # every old window has aged out
    check("the bucket table is swept once it grows, not left to accumulate",
          len(w._log) < grew, f"{grew} -> {len(w._log)}")
    # The sweep must key on staleness, not emptiness. A stale bucket is NOT
    # empty — it is full of timestamps nobody has examined. Getting this wrong
    # meant the table filled up and, because the share tier fails open, the
    # per-visitor limit silently stopped working for good.
    check("...and it is the STALE ones that went, leaving the live one",
          len(w._log) == 1 and "later" in w._log, f"keys={sorted(w._log)[:4]}")
    # A bucket still inside its window must survive a sweep, or the limiter
    # would forget an active visitor and hand them a fresh allowance.
    w2 = ratelimit._Window(2, 60.0, "per-minute", fail_open=True)
    for i in range(15):
        w2.would_refuse(f"v-{i}", 0.0)
        w2.record(f"v-{i}", 0.0)
    w2.would_refuse("newcomer", 1.0)       # 1s later: everyone is still active
    check("an ACTIVE bucket is never swept", len(w2._log) == 16,
          f"len={len(w2._log)}")
finally:
    ratelimit.MAX_BUCKETS = _saved


# ---------------------------------------------------------------------------
section("9. the visitor tier is config-driven and can be turned off")
# ---------------------------------------------------------------------------
for k in ("max_requests_per_visitor_per_minute", "max_requests_per_visitor_per_day"):
    check(f"{k} is in DEFAULT_CONFIG", k in registry.DEFAULT_CONFIG)
_c = base_cfg()
check("a tenant config that never mentions them still inherits them",
      bool(_c.get("max_requests_per_visitor_per_minute"))
      and bool(_c.get("max_requests_per_visitor_per_day")))
check("the per-visitor share is TIGHTER than the credential ceiling "
      "(otherwise it can never bind)",
      _c["max_requests_per_visitor_per_day"] < _c["max_requests_per_day"]
      and _c["max_requests_per_visitor_per_minute"] <= _c["max_requests_per_minute"],
      f"visitor {_c['max_requests_per_visitor_per_minute']}/min "
      f"{_c['max_requests_per_visitor_per_day']}/day vs role "
      f"{_c['max_requests_per_minute']}/min {_c['max_requests_per_day']}/day")

_live = ratelimit.tiered_from_config(_c)
check("tiered_from_config builds both tiers",
      _live.ceiling.enabled and _live.share.enabled)
check("the share tier is fail-open, the ceiling is not",
      all(w.fail_open for w in _live.share.windows)
      and not any(w.fail_open for w in _live.ceiling.windows))
check("how many heavy visitors it takes to exhaust the day is a real number",
      (_c["max_requests_per_day"] // _c["max_requests_per_visitor_per_day"]) >= 1,
      f"{_c['max_requests_per_day'] // _c['max_requests_per_visitor_per_day']} "
      f"visitors at their full daily share")

with env(MAX_REQUESTS_PER_VISITOR_PER_MINUTE="4"):
    check("MAX_REQUESTS_PER_VISITOR_PER_MINUTE overrides config",
          ratelimit.visitor_limits_from_config(_c).windows[0].limit == 4)
with env(MAX_REQUESTS_PER_VISITOR_PER_MINUTE="off",
         MAX_REQUESTS_PER_VISITOR_PER_DAY="off"):
    _off = ratelimit.tiered_from_config(_c)
    check("the visitor tier can be disabled entirely", not _off.share.enabled)
    check("disabling it leaves the credential ceiling intact", _off.ceiling.enabled)
    check("...and requests still flow, limited only by the ceiling",
          _admit_t(_off, "guest", "alice", now=0.0))


# ---------------------------------------------------------------------------
section("10. WIRING — the real endpoint honours X-Visitor-Id")
# ---------------------------------------------------------------------------
# Everything in sections 6-9 passes if main.py never reads the header. This is
# the only part that would notice.
_calls3 = {"answer": 0}


async def _spy_answer3(*a, **kw):
    _calls3["answer"] += 1
    return "an answer", m.TokenMeter(), False, 0


_tiny3 = ratelimit.TieredRateLimiter(
    ceiling=ratelimit.RateLimiter(100, None, tier="credential"),
    share=ratelimit.RateLimiter(2, None, fail_open=True, tier="visitor"))
client, _mod, undo = test_client(answer_once=_spy_answer3,
                                 retrieve_and_rerank=_spy_retrieve,
                                 LIMITER=_tiny3)
try:
    with env(RAG_KEY_GUEST=_GUEST_KEY):
        def q(vid=None):
            h = {"X-API-Key": _GUEST_KEY}
            if vid:
                h["X-Visitor-Id"] = vid
            return client.post("/api/rag", json={"query": "hi"}, headers=h).status_code

        check("alice is served her share", [q("alice"), q("alice")] == [200, 200])
        check("alice is then 429'd by the VISITOR tier", q("alice") == 429)
        check("WIRING: bob is still served — the header really is read",
              q("bob") == 200, "if main.py ignored the header, bob would be 429")
        check("bob gets his own full share", q("bob") == 200)
        check("...then bob is limited too", q("bob") == 429)
        # A caller with no header falls back to ceiling-only, so it must not be
        # caught by another visitor's exhausted bucket.
        check("a request with NO X-Visitor-Id is not caught by alice's bucket",
              q(None) == 200)
        # The streaming endpoint is a separate handler — a guard wired into one
        # path and not the other is the classic near-miss.
        r = client.post("/chat", json={"query": "hi"},
                        headers={"X-API-Key": _GUEST_KEY, "X-Visitor-Id": "alice"})
        check("/chat honours the visitor tier too", r.status_code == 429,
              f"status={r.status_code}")
finally:
    undo()

finish("test_rate_limit")