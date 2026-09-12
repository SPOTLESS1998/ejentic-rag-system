"""Optional bridge: report real usage + AI cost to the agency metrics hub.

WHY THIS EXISTS
---------------
This system already counts tokens per query (token_meter.py) and stores them in
its own audit DB (database.py). That answers "how is this deployment behaving".
It does not answer the business question: **what does serving this client cost
us, and does their retainer clear it?** The metrics hub aggregates that across
every deployed system; this module is the one-way pipe into it.

DESIGN RULES (deliberate — do not "simplify" these away)
-------------------------------------------------------
1. **Off unless configured.** No `METRICS_HUB` env var, no hub on disk, no
   emitting. This repo is public and runs on other people's machines; it must
   never assume a path that only exists on ours.
2. **Never break a request.** Every failure is swallowed with one `[metrics]`
   warning. A missing business metric is an annoyance; a 500 on a client's
   query is not. Same contract as `database.log_query`.
3. **Never invent a number.** If the model has no price in the hub, the hub
   records the cost as *unknown* rather than zero — an unpriced call is a thing
   we must go fix, not a free one.
4. **No client facts in code.** The tenant id is passed in per call and the
   system id comes from config, so this file stays identical for every client.

ENABLING IT
-----------
    METRICS_HUB=/path/to/metrics        # directory holding money.py
    METRICS_SYSTEM_ID=rag               # optional; must match the hub's catalog
"""
import os
import sys
from pathlib import Path

# Which product in the hub's catalog this deployment reports as.
SYSTEM_ID = (os.environ.get("METRICS_SYSTEM_ID") or "rag").strip()

_money = None
_disabled_reason = None


def _load_hub():
    """Import the hub's money module, or leave metrics off. Import failure here
    is never fatal — this is optional instrumentation, not a dependency."""
    global _money, _disabled_reason
    hub = (os.environ.get("METRICS_HUB") or "").strip()
    if not hub:
        _disabled_reason = "METRICS_HUB not set"
        return
    try:
        path = Path(hub).expanduser().resolve()
        if not (path / "money.py").exists():
            _disabled_reason = f"no money.py under {path}"
            return
        # Appended, not prepended: the hub must never shadow this app's own
        # modules if a name ever collides.
        if str(path) not in sys.path:
            sys.path.append(str(path))
        import money as _m  # noqa: PLC0415 - deliberately lazy/optional
        _money = _m
        print(f"[metrics] reporting usage + cost to {path}")
    except Exception as exc:  # noqa: BLE001 - optional instrumentation
        _disabled_reason = str(exc)
        print(f"[metrics] disabled ({exc})")


_load_hub()

_model_cache = None


def _model_name():
    """The model this process actually calls, resolved the same way main.py does:
    env override first, then the active client's config."""
    global _model_cache
    if _model_cache is not None:
        return _model_cache
    name = (os.environ.get("LLM_MODEL") or "").strip()
    if not name:
        try:
            import client_registry as registry  # noqa: PLC0415
            active = (os.environ.get("RAG_CLIENT", "").strip()
                      or registry.active_client_id())
            name = (registry.get_client(active) or {}).get("llm_model") or ""
        except Exception:  # noqa: BLE001
            name = ""
    _model_cache = name.strip() or "unknown"
    return _model_cache


def enabled() -> bool:
    return _money is not None


def emit(client, prompt_tokens=None, completion_tokens=None, total_tokens=None,
         token_source=None, gated=False, model=None) -> bool:
    """Report one served query to the hub. Returns True if anything was written.

    Called once per query from `database.log_query`, which every answer path
    funnels through exactly once.

    What counts as what:
      * **an answer** (the unit we bill on) — only when the query was not gated
        AND tokens were actually spent. That excludes greetings and error rows,
        which do no retrieval and call no model, and would otherwise inflate the
        headline number with work we never did.
      * **a cost** — whenever tokens were spent, INCLUDING a gated refusal.
        The confidence gate still paid for embedding and reranking before it
        decided not to answer, and that money is real.
    """
    if _money is None:
        return False
    try:
        spent = int(total_tokens or 0) or int(prompt_tokens or 0) or int(completion_tokens or 0)
        if not spent:
            return False  # greeting or error path — no work done, nothing to bill

        wrote = False
        if not gated:
            wrote |= _money.log_usage(SYSTEM_ID, client, "answers", 1,
                                      token_source=token_source)
        # token_source "estimate" means the provider gave us no usage numbers and
        # token_meter approximated them — so the cost is an estimate too. Pass it
        # through rather than presenting a guess as a measurement.
        wrote |= _money.log_ai_cost(
            SYSTEM_ID, client, model or _model_name(),
            int(prompt_tokens or 0), int(completion_tokens or 0),
            token_source=token_source,
            gated=(True if gated else None),
            estimated=(True if token_source and token_source != "provider" else None),
        )
        return wrote
    except Exception as exc:  # noqa: BLE001 - metrics must never break a query
        print(f"[metrics] WARNING: failed to report usage/cost: {exc}")
        return False
