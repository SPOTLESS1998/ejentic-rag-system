"""Per-caller request ceilings — the OTHER half of bounding what this costs.

WHY THIS EXISTS
---------------
`main.py` bounds a SINGLE request: the query has a max length and the LLM has a
max_tokens. Nothing bounded HOW MANY requests. A valid key pointed at a loop —
a buggy client, a crawler, a leaked credential — could issue them as fast as the
network delivered them, and every one of them would be individually "within
limits" while the total was unbounded.

Those two halves only become a spend ceiling TOGETHER:

    worst-case daily completion spend  =  max_requests_per_day x max_output_tokens

That arithmetic is the whole point, and it is why the request cap is a cost
control rather than a politeness feature. Bounding the count of unbounded things
bounds nothing; bounding the count of BOUNDED things bounds the total. Whoever
tunes these numbers should do that multiplication first — the docstring on
`limits_from_config` spells it out with the shipped defaults.

DESIGN — AND WHAT IT DELIBERATELY IS NOT
----------------------------------------
Two rolling windows over an in-memory log of timestamps. No database, no Redis,
no I/O of any kind. That is a deliberate trade, not laziness:

  * A limiter that performs I/O can FAIL, and then you must choose between
    failing open (unbounded spend — the exact thing being prevented) and failing
    closed (the service goes down because a counter was unreachable). A limiter
    with no I/O cannot present that choice.
  * The cost is that a process restart clears the windows. Accepted knowingly:
    no attacker can restart our process, deploys are human-paced, and a
    crash-loop that resets the day window means the service is already down. The
    honest summary is "this bounds spend per uptime period, not per calendar
    day", and that is written here rather than discovered later.

⚠️ SINGLE PROCESS ONLY. The counters live in this process's memory, so running
uvicorn with `--workers N` silently multiplies every limit by N. We serve with a
single worker everywhere (Dockerfile, RUNBOOK, run_e2e_test.sh). If that ever
changes, these limits must move to shared storage or be divided by the worker
count — there is no way for this module to detect it.

ROLLING, NOT CALENDAR. "Per day" means any 24-hour span, not midnight-to-
midnight. A calendar window hands an abuser a free reset at a known instant and
lets a burst straddle the boundary at double the limit. Time comes from
`time.monotonic()`, so an NTP correction or a DST change cannot widen a window
or, worse, lock everyone out until the clock catches up.

TWO RULES THAT ARE EASY TO GET WRONG
------------------------------------
1. A REFUSED request is not recorded. If refusals counted against the window, a
   client stuck in a retry loop would extend its own lockout forever and could
   never recover on its own — the limit would stop being predictable.
2. Both windows are checked BEFORE either is recorded. Checking and recording
   window-by-window would let the minute window record a request that the day
   window then refuses, silently charging budget for a request that never ran.
"""
from __future__ import annotations

import os
import time
from collections import deque
from typing import Optional

# Ceiling on how many distinct buckets we will track. With role-keyed buckets
# (the only thing main.py passes) this can never fire: roles come from the tenant
# config and there are a handful. It exists so that a LATER change to a
# caller-controlled key — an IP, an actor id — degrades safely instead of turning
# the limiter itself into the unbounded-memory bug it was written to prevent.
#
# Over the cap we REFUSE rather than evict. Evicting the oldest bucket would
# discard its history, and discarding history resets its limit: an attacker
# cycling through fake keys would get a fresh allowance every time. Refusing is
# the fail-closed reading.
MAX_BUCKETS = 2048


class RateLimitError(Exception):
    """A caller exceeded one of its windows.

    Plain exception, not HTTPException, matching auth.py: the logic here stays
    framework-free so it is directly testable without a request object, and
    main.py converts it at the boundary.

    `retry_after` is whole seconds, rounded UP and floored at 1 — it goes into
    the HTTP `Retry-After` header, and a header saying 0 invites an immediate
    retry that is guaranteed to be refused again.
    """

    def __init__(self, detail: str, retry_after: int, window: str):
        super().__init__(detail)
        self.status = 429
        self.detail = detail
        self.retry_after = retry_after
        self.window = window


class _Window:
    """At most `limit` events in any `seconds`-long span, per bucket.

    A sliding log (the timestamps themselves) rather than a counter, because a
    counter can only do calendar windows. Memory is bounded by construction: a
    bucket never holds more than `limit` timestamps, since one more than that is
    exactly the condition for refusing.
    """

    def __init__(self, limit: int, seconds: float, label: str):
        self.limit = int(limit)
        self.seconds = float(seconds)
        self.label = label
        self._log: dict[str, deque] = {}

    def _prune(self, bucket: str, now: float) -> Optional[deque]:
        """Drop timestamps that have aged out. Returns the live log, or None if
        this bucket does not exist and cannot be created."""
        dq = self._log.get(bucket)
        if dq is None:
            if len(self._log) >= MAX_BUCKETS:
                return None
            dq = self._log[bucket] = deque()
        cutoff = now - self.seconds
        while dq and dq[0] <= cutoff:
            dq.popleft()
        return dq

    def would_refuse(self, bucket: str, now: float) -> Optional[float]:
        """Seconds to wait if admitting one more event would breach this window,
        or None if there is room. Records NOTHING — see rule 2 in the module
        docstring."""
        dq = self._prune(bucket, now)
        if dq is None:
            # Bucket cap reached. Fail closed, loudly: this is a misconfiguration
            # or an attack, and either way silence is the wrong response.
            print(f"[ratelimit] REFUSING: bucket cap ({MAX_BUCKETS}) reached on "
                  f"{self.label}; '{bucket}' not tracked. Are limiter keys "
                  f"caller-controlled? See MAX_BUCKETS.")
            return self.seconds
        if self.limit <= 0:
            # A limit of zero means "closed", not "unlimited". An operator who
            # types 0 gets a closed door; unlimited is expressed by omitting the
            # window entirely (see limits_from_config).
            return self.seconds
        if len(dq) < self.limit:
            return None
        # The oldest event in the window is what has to expire before there is
        # room for one more.
        return max(0.0, (dq[0] + self.seconds) - now)

    def record(self, bucket: str, now: float) -> None:
        dq = self._log.get(bucket)
        if dq is not None:
            dq.append(now)

    def used(self, bucket: str, now: float) -> int:
        """How many events are currently inside the window. Diagnostics only."""
        dq = self._prune(bucket, now)
        return len(dq) if dq is not None else 0


class RateLimiter:
    """The two windows, checked and recorded as one atomic decision.

    THREAD / TASK SAFETY: `admit` contains no `await`, so under a single-threaded
    asyncio event loop it runs to completion without interleaving and needs no
    lock. Anyone adding an await inside it breaks that guarantee and must add
    one — this is the reason `admit` is a plain `def` and not `async def`.
    """

    def __init__(self, per_minute: Optional[int], per_day: Optional[int]):
        self.windows = []
        if per_minute is not None:
            self.windows.append(_Window(per_minute, 60.0, "per-minute"))
        if per_day is not None:
            self.windows.append(_Window(per_day, 86_400.0, "per-day"))

    @property
    def enabled(self) -> bool:
        return bool(self.windows)

    def admit(self, bucket: str, now: Optional[float] = None) -> None:
        """Admit one request for `bucket`, or raise RateLimitError.

        `now` is injectable so the tests can advance time without sleeping — a
        rate limiter tested with real sleeps is a rate limiter that is only
        tested at one speed.
        """
        if now is None:
            now = time.monotonic()

        # Check every window BEFORE recording in any of them (rule 2). The
        # tightest refusal wins, so the Retry-After we hand back is the one that
        # is actually true.
        worst: Optional[tuple] = None
        for w in self.windows:
            wait = w.would_refuse(bucket, now)
            if wait is not None and (worst is None or wait > worst[0]):
                worst = (wait, w)

        if worst is not None:
            wait, w = worst
            retry_after = max(1, int(wait) + (1 if wait % 1 else 0))
            raise RateLimitError(
                f"rate limit exceeded: at most {w.limit} requests per "
                f"{'minute' if w.seconds <= 60 else '24h'} for this credential. "
                f"Retry in {retry_after}s.",
                retry_after=retry_after,
                window=w.label,
            )

        for w in self.windows:
            w.record(bucket, now)

    def snapshot(self, bucket: str, now: Optional[float] = None) -> dict:
        """Current usage per window. For diagnostics and tests, never for a
        decision — reading it must not change anything."""
        if now is None:
            now = time.monotonic()
        return {w.label: {"used": w.used(bucket, now), "limit": w.limit}
                for w in self.windows}


def _window_limit(value, key: str, env_name: str) -> Optional[int]:
    """Resolve one window's limit from the environment, else the tenant config.

    WHY ENV OVERRIDES EXIST HERE and not only in the JSON: these are the numbers
    most likely to need changing in a hurry — a limit set too tight is
    indistinguishable from an outage to the people hitting it. Every other
    tunable in main.py is `env or config`, and the knob you reach for during an
    incident must live in the deployment's env file, not inside a file copy that
    needs a rebuild.

    FAIL-CLOSED ON ABSENCE. An unset variable means "use the configured limit",
    never "no limit". Turning the limiter off takes an EXPLICIT `off`, because
    "absent = disabled" is how a cost control quietly stops existing — the same
    shape as the RAG_STAFF off-switch that failed open (see the audit notes).

    `null`/missing in config disables a window; 0 CLOSES it. A typo must not read
    as unlimited, so a non-integer is an error at load rather than a default.
    """
    raw = (os.environ.get(env_name) or "").strip()
    if raw:
        if raw.lower() in ("off", "none", "null", "disabled"):
            print(f"[ratelimit] WARNING: {env_name}={raw!r} — the {key} ceiling is "
                  f"DISABLED for this process. Spend on this window is unbounded.")
            return None
        try:
            return int(raw)
        except ValueError:
            raise ValueError(
                f"{env_name}={raw!r} must be an integer, or 'off' to disable "
                f"the window. Refusing to guess."
            )

    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer or null, got {value!r}")
    return value


def limits_from_config(cfg: dict) -> RateLimiter:
    """Build the limiter for a tenant.

    THE ARITHMETIC THAT MATTERS, with the shipped defaults (30/min, 500/day) and
    the shipped `max_output_tokens` of 2048:

        30 x 2048   =    61,440 completion tokens in any one minute
        500 x 2048  = 1,024,000 completion tokens in any 24h

    ...per ROLE, so multiply by the number of roles that hold a key for the true
    instance-wide ceiling. Those are WORST CASE: every request would have to run
    the generation to its cap, which real answers (~300 tokens) do not approach.

    The defaults are sized to be invisible to a human — a person asks a handful
    of questions a minute, so 30 is roughly 10x normal use — while stopping a
    runaway loop within seconds. They are NOT sized against any particular
    provider's quota: a deployment on a metered or free-tier provider should set
    its own numbers BELOW that provider's limit, in its own `clients/<id>.json`
    or via the env overrides below, so we refuse gracefully with a Retry-After
    instead of the provider returning a raw 429 in the middle of a demo. That
    number is a fact about a specific deployment, so it belongs in that tenant's
    config and never here.

    ⚠️ ONE BUCKET PER ROLE, NOT PER PERSON. A deployment whose public UI holds a
    single shared key puts every visitor in the same bucket, so the daily limit
    is the whole site's budget rather than one visitor's. Size it against
    expected total traffic, not per-user behaviour.

    Env overrides (for the deployment's env file, changeable without a rebuild):
    MAX_REQUESTS_PER_MINUTE, MAX_REQUESTS_PER_DAY. Unset = use config.
    """
    return RateLimiter(
        per_minute=_window_limit(cfg.get("max_requests_per_minute"),
                                 "max_requests_per_minute",
                                 "MAX_REQUESTS_PER_MINUTE"),
        per_day=_window_limit(cfg.get("max_requests_per_day"),
                              "max_requests_per_day",
                              "MAX_REQUESTS_PER_DAY"),
    )
