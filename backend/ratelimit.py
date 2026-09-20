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
import re
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

    def __init__(self, limit: int, seconds: float, label: str,
                 fail_open: bool = False):
        self.limit = int(limit)
        self.seconds = float(seconds)
        self.label = label
        # What to do when MAX_BUCKETS is reached and this bucket is untracked.
        # The CEILING tier fails closed (it is the cost guarantee). The per-visitor
        # SHARE tier fails OPEN — see TieredRateLimiter for why that asymmetry is
        # correct rather than sloppy.
        self.fail_open = fail_open
        self._capped_logged = False
        self._log: dict[str, deque] = {}

    def _sweep(self, now: float) -> None:
        """Drop buckets that are entirely outside the window.

        Needed because the SHARE tier is keyed by visitor, and visitors are
        unbounded over time while the ceiling tier's roles are not. Without this,
        `_log` grows with every visitor ever seen rather than with visitors
        currently inside the window — and since the share tier fails OPEN at the
        cap, that means the per-visitor limit would silently stop working for
        good once enough distinct visitors had been served. (Found by the test
        that asserts this, not by reading the code.)

        Checking `dq[-1]` is the whole trick: a bucket is stale when its NEWEST
        entry has aged out, and that is O(1). An earlier version only removed
        buckets that were already empty, which never fired — a stale bucket is
        not empty, it is full of timestamps nobody has looked at yet.

        Runs only when the table is getting large, so the common path stays O(1).
        """
        if len(self._log) <= MAX_BUCKETS // 2:
            return
        cutoff = now - self.seconds
        stale = [k for k, dq in self._log.items() if not dq or dq[-1] <= cutoff]
        for key in stale:
            del self._log[key]

    def _prune(self, bucket: str, now: float) -> Optional[deque]:
        """Drop timestamps that have aged out. Returns the live log, or None if
        this bucket does not exist and cannot be created."""
        dq = self._log.get(bucket)
        if dq is None:
            self._sweep(now)
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
            # Bucket table is full and this bucket is not in it. Log ONCE, not per
            # request: at this point every request takes this path, and a line per
            # request would bury the incident in its own noise.
            if not self._capped_logged:
                self._capped_logged = True
                print(f"[ratelimit] bucket cap ({MAX_BUCKETS}) reached on "
                      f"{self.label}; '{bucket}' untracked — "
                      + ("ALLOWING (share tier; the per-credential ceiling still "
                         "applies)" if self.fail_open else
                         "REFUSING (ceiling tier, fail-closed)"))
            return None if self.fail_open else self.seconds
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

    def __init__(self, per_minute: Optional[int], per_day: Optional[int],
                 fail_open: bool = False, tier: str = "credential"):
        self.tier = tier
        self.windows = []
        if per_minute is not None:
            self.windows.append(_Window(per_minute, 60.0, "per-minute", fail_open))
        if per_day is not None:
            self.windows.append(_Window(per_day, 86_400.0, "per-day", fail_open))

    @property
    def enabled(self) -> bool:
        return bool(self.windows)

    def worst_refusal(self, bucket: str, now: float) -> Optional[tuple]:
        """The tightest window refusing this request, as (wait_seconds, window),
        or None if every window has room. RECORDS NOTHING.

        Split out from `admit` so a caller holding SEVERAL limiters can check all
        of them before recording in any — the same rule-2 atomicity that applies
        between windows applies between tiers. See TieredRateLimiter.
        """
        worst: Optional[tuple] = None
        for w in self.windows:
            wait = w.would_refuse(bucket, now)
            if wait is not None and (worst is None or wait > worst[0]):
                worst = (wait, w)
        return worst

    def record(self, bucket: str, now: float) -> None:
        for w in self.windows:
            w.record(bucket, now)

    def _refuse(self, worst: tuple) -> "RateLimitError":
        wait, w = worst
        retry_after = max(1, int(wait) + (1 if wait % 1 else 0))
        scope = ("this credential" if self.tier == "credential"
                 else "one visitor")
        return RateLimitError(
            f"rate limit exceeded: at most {w.limit} requests per "
            f"{'minute' if w.seconds <= 60 else '24h'} for {scope}. "
            f"Retry in {retry_after}s.",
            retry_after=retry_after,
            window=f"{self.tier}:{w.label}",
        )

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
        worst = self.worst_refusal(bucket, now)
        if worst is not None:
            raise self._refuse(worst)
        self.record(bucket, now)

    def snapshot(self, bucket: str, now: Optional[float] = None) -> dict:
        """Current usage per window. For diagnostics and tests, never for a
        decision — reading it must not change anything."""
        if now is None:
            now = time.monotonic()
        return {w.label: {"used": w.used(bucket, now), "limit": w.limit}
                for w in self.windows}


VISITOR_ID_MAX = 64
_VISITOR_OK = re.compile(r"[A-Za-z0-9._-]+")


def clean_visitor_id(value: Optional[str]) -> Optional[str]:
    """Sanitise a caller-supplied visitor id before it becomes a dict key.

    Returns None for anything absent or implausible, which degrades that request
    to ceiling-only limiting rather than refusing it.

    This value arrives in a header, so it is caller-controlled and lands in an
    in-memory table — exactly the shape that turns a limiter into the
    unbounded-memory bug it was written to prevent. So: fixed charset, hard
    length cap, and no normalisation games. It is compared only for equality, so
    there is nothing to be clever about.
    """
    if not value:
        return None
    v = value.strip()
    if not v or len(v) > VISITOR_ID_MAX:
        return None
    if not _VISITOR_OK.fullmatch(v):
        return None
    return v


class TieredRateLimiter:
    """A per-credential CEILING with a per-visitor SHARE nested inside it.

    WHY TWO TIERS
    -------------
    Buckets keyed by API key alone are the wrong shape for a public site. The
    public UI holds ONE shared guest key, so every visitor lands in one bucket
    and `max_requests_per_day` becomes the whole site's daily budget rather than
    one visitor's. Roughly fifty visitors asking ten questions each exhausts it,
    and every visitor after that sees a 429 that reads as an outage.

    So: the ceiling (keyed by the authenticated role) still bounds total spend,
    and inside it a share (keyed by visitor) stops any single visitor taking a
    disproportionate slice. BOTH must admit. Neither replaces the other.

    🧠 THE POINT OF THE NESTING — AND WHY A FORGEABLE ID IS STILL USEFUL.
    The visitor id comes from a header, so anyone holding the API key can forge
    or rotate it. That would be fatal if the visitor bucket were the only limit:
    rotate the id, get a fresh allowance. It is fine here because the ceiling is
    keyed by the API KEY, which the caller cannot forge — rotating visitor ids
    buys unlimited *visitor* buckets and still runs into the *credential*
    ceiling. The unforgeable identity enforces the ceiling; the forgeable one
    only refines fairness beneath it. Read in that order, the design is sound;
    read the other way round it looks broken.

    THE FAILURE ASYMMETRY, WHICH IS DELIBERATE
    ------------------------------------------
    At MAX_BUCKETS the two tiers behave oppositely:

      * the CEILING fails CLOSED — it is the cost guarantee, and an untracked
        bucket there means we cannot bound spend, so we refuse;
      * the SHARE fails OPEN — it degrades to ceiling-only limiting.

    Failing the share closed would refuse legitimate visitors once enough
    distinct visitors were seen, which is a self-inflicted outage. Failing it
    open costs only fairness, never the cost bound, *because the ceiling is
    still there*. This is the same reasoning that makes a forgeable id
    acceptable: everything the share tier can lose is recoverable; nothing the
    ceiling protects is.
    """

    def __init__(self, ceiling: RateLimiter, share: RateLimiter):
        self.ceiling = ceiling
        self.share = share

    @property
    def enabled(self) -> bool:
        return self.ceiling.enabled or self.share.enabled

    @property
    def windows(self):
        """Every active window, for the boot banner and diagnostics."""
        return list(self.ceiling.windows) + list(self.share.windows)

    def admit(self, role: str, visitor: Optional[str] = None,
              now: Optional[float] = None) -> None:
        """Charge one request against both tiers, or raise RateLimitError.

        `visitor` may be None (no id presented, or it failed sanitisation), in
        which case only the ceiling applies. That is the honest fallback: we
        cannot fairly share what we cannot distinguish, and refusing would punish
        a caller for our own missing metadata.
        """
        if now is None:
            now = time.monotonic()

        vid = clean_visitor_id(visitor)
        # Namespaced by role so a visitor id can never collide across roles — and
        # so a guest cannot consume an executive visitor bucket by presenting the
        # same id. \x00 cannot appear in a sanitised id, so the join is unambiguous.
        share_bucket = f"{role}\x00{vid}" if vid else None

        # BOTH tiers are checked before EITHER records — the same atomicity rule
        # that holds between windows holds between tiers. Charging the ceiling for
        # a request the share tier then refuses would spend budget on a request
        # that never ran.
        worst_ceiling = self.ceiling.worst_refusal(role, now)
        worst_share = (self.share.worst_refusal(share_bucket, now)
                       if share_bucket and self.share.enabled else None)

        # The ceiling's refusal is reported in preference to the share's: it is
        # the more serious condition (everyone on this credential is affected,
        # not just this visitor) and its Retry-After is the one that matters.
        if worst_ceiling is not None:
            raise self.ceiling._refuse(worst_ceiling)
        if worst_share is not None:
            raise self.share._refuse(worst_share)

        self.ceiling.record(role, now)
        if share_bucket and self.share.enabled:
            self.share.record(share_bucket, now)

    def snapshot(self, role: str, visitor: Optional[str] = None,
                 now: Optional[float] = None) -> dict:
        """Usage in both tiers. Diagnostics only — must not change anything."""
        if now is None:
            now = time.monotonic()
        out = {"credential": self.ceiling.snapshot(role, now)}
        vid = clean_visitor_id(visitor)
        if vid and self.share.enabled:
            out["visitor"] = self.share.snapshot(f"{role}\x00{vid}", now)
        return out


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
        tier="credential",
    )


def visitor_limits_from_config(cfg: dict) -> RateLimiter:
    """The per-visitor SHARE tier, nested inside the credential ceiling.

    Sized for one human, not for the site: a person asking more than ~10
    questions a minute is not reading the answers. Its job is not to bound spend
    — the ceiling does that — but to stop one visitor consuming a
    disproportionate slice of a shared key's allowance.

    FAIL-OPEN at the bucket cap, deliberately. See TieredRateLimiter for why the
    two tiers fail in opposite directions.

    Env overrides: MAX_REQUESTS_PER_VISITOR_PER_MINUTE / ..._PER_DAY.
    `null` in config (or `off` in env) disables the tier, which returns the
    system to ceiling-only behaviour.
    """
    return RateLimiter(
        per_minute=_window_limit(cfg.get("max_requests_per_visitor_per_minute"),
                                 "max_requests_per_visitor_per_minute",
                                 "MAX_REQUESTS_PER_VISITOR_PER_MINUTE"),
        per_day=_window_limit(cfg.get("max_requests_per_visitor_per_day"),
                              "max_requests_per_visitor_per_day",
                              "MAX_REQUESTS_PER_VISITOR_PER_DAY"),
        fail_open=True,
        tier="visitor",
    )


def tiered_from_config(cfg: dict) -> TieredRateLimiter:
    """The limiter the app actually uses: ceiling + nested per-visitor share."""
    return TieredRateLimiter(ceiling=limits_from_config(cfg),
                             share=visitor_limits_from_config(cfg))
