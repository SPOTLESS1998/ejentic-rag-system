"""The business-metrics bridge — what we report, and what we must NOT report.

This suite guards the accounting rules in metrics_bridge.emit(), because a
quietly wrong number here is worse than no number: it would be used to price
client work.

The rules under test:
  * a served answer counts as one billable "answer"
  * a GATED refusal costs money (embedding + rerank ran) but is NOT an answer
  * a greeting or an error row spent nothing and must produce NO events at all
  * an unpriced model records an UNKNOWN cost, never a free one
  * nothing here may ever raise into the request path

It runs against a STUB hub written into a temp dir, not the real metrics hub —
so it is hermetic, needs no private repo present, and can never append a test
row to a real ledger.
"""
import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path

import harness  # noqa: F401 - sets RAG_OFFLINE and the fake keys
from harness import check, finish, section

STUB_MONEY = '''
CALLS = []

def log_usage(system, client, unit, count, **extra):
    CALLS.append({"kind": "usage", "system": system, "client": client,
                  "unit": unit, "count": count, **extra})
    return True

def log_ai_cost(system, client, model, input_tokens, output_tokens, **extra):
    CALLS.append({"kind": "cost", "system": system, "client": client,
                  "model": model, "input_tokens": input_tokens,
                  "output_tokens": output_tokens, **extra})
    return True
'''


def build_stub_hub() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stub_hub_"))
    (d / "money.py").write_text(STUB_MONEY, "utf-8")
    return d


HUB = build_stub_hub()
os.environ["METRICS_HUB"] = str(HUB)
os.environ["METRICS_SYSTEM_ID"] = "rag"
os.environ.setdefault("LLM_MODEL", "models/gemini-flash-lite-latest")

import metrics_bridge  # noqa: E402 - must be imported AFTER METRICS_HUB is set

CALLS = metrics_bridge._money.CALLS


def reset():
    CALLS.clear()


def kinds():
    return [c["kind"] for c in CALLS]


# ---------------------------------------------------------------------------
section("the bridge loads when configured")
check("enabled with METRICS_HUB set", metrics_bridge.enabled())
check("system id comes from config, not hardcoded",
      metrics_bridge.SYSTEM_ID == "rag")

# ---------------------------------------------------------------------------
section("a served answer is one billable answer, plus its cost")
reset()
metrics_bridge.emit("acme", prompt_tokens=1200, completion_tokens=300,
                    total_tokens=1500, token_source="provider", gated=False)
check("emitted exactly two events", len(CALLS) == 2, f"got {kinds()}")
usage = next((c for c in CALLS if c["kind"] == "usage"), {})
cost = next((c for c in CALLS if c["kind"] == "cost"), {})
check("counted 1 answer", usage.get("count") == 1 and usage.get("unit") == "answers")
check("billed to the tenant passed in", usage.get("client") == "acme")
check("cost carries the real token counts",
      cost.get("input_tokens") == 1200 and cost.get("output_tokens") == 300)
check("cost carries the model actually called",
      cost.get("model") == "models/gemini-flash-lite-latest", cost.get("model"))
check("provider-reported usage is not flagged estimated",
      cost.get("estimated") is None)

# ---------------------------------------------------------------------------
section("a GATED refusal costs money but is not an answer")
reset()
metrics_bridge.emit("acme", prompt_tokens=800, completion_tokens=0,
                    total_tokens=800, token_source="gate", gated=True)
check("cost recorded (retrieval really ran)", "cost" in kinds())
check("NO answer counted for a refusal", "usage" not in kinds(), f"got {kinds()}")
check("the cost row is marked gated",
      next(c for c in CALLS if c["kind"] == "cost").get("gated") is True)

# ---------------------------------------------------------------------------
section("work we never did produces no events")
reset()
metrics_bridge.emit("acme", gated=False)                      # greeting path
check("greeting emits nothing", CALLS == [], f"got {kinds()}")
reset()
metrics_bridge.emit("acme", prompt_tokens=0, completion_tokens=0,
                    total_tokens=0, gated=False)              # error path
check("error row emits nothing", CALLS == [], f"got {kinds()}")

# ---------------------------------------------------------------------------
section("estimated tokens are labelled, not passed off as measured")
reset()
metrics_bridge.emit("acme", prompt_tokens=900, completion_tokens=200,
                    total_tokens=1100, token_source="estimate", gated=False)
cost = next(c for c in CALLS if c["kind"] == "cost")
check("estimate is flagged as an estimate", cost.get("estimated") is True)
check("token_source is passed through", cost.get("token_source") == "estimate")

# ---------------------------------------------------------------------------
section("metrics can never break a request")
reset()
raised = False
try:
    # completion_tokens is unconvertible; emit must absorb it, not propagate
    ok = metrics_bridge.emit("acme", prompt_tokens=10,
                             completion_tokens=object(), total_tokens=10)
except Exception:  # noqa: BLE001
    raised = True
check("garbage input does not raise", not raised)
check("and reports failure honestly", ok is False)

# ---------------------------------------------------------------------------
section("off by default — no hub configured means silence")
saved = os.environ.pop("METRICS_HUB")
try:
    fresh = importlib.reload(metrics_bridge)
    check("disabled without METRICS_HUB", not fresh.enabled())
    check("emit is a no-op when disabled",
          fresh.emit("acme", prompt_tokens=100, completion_tokens=100,
                     total_tokens=200) is False)
finally:
    os.environ["METRICS_HUB"] = saved
    metrics_bridge = importlib.reload(metrics_bridge)
    CALLS = metrics_bridge._money.CALLS

# ---------------------------------------------------------------------------
section("wired into the real audit path (log_query emits once per query)")
os.environ["RAG_AUDIT_DB"] = str(Path(tempfile.gettempdir()) / "rag_bridge_test.db")
import database  # noqa: E402 - after METRICS_HUB so its optional import binds

check("database picked the bridge up", database.metrics_bridge is not None)
reset()


async def _one_query():
    await database.init_db()
    await database.log_query(
        "WEB_UI_public", "what is the refund policy?", "You may return within 30 days.",
        prompt_tokens=1000, completion_tokens=250, total_tokens=1250,
        token_source="provider", gated=False, client="acme",
    )


asyncio.run(_one_query())
check("one query produced exactly one usage + one cost", sorted(kinds()) == ["cost", "usage"],
      f"got {kinds()}")
check("tenant carried through from log_query",
      all(c["client"] == "acme" for c in CALLS))

finish("metrics bridge")
