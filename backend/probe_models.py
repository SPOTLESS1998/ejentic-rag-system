"""Probe which chat models this deployment can ACTUALLY serve.

`GET /models` lists models that then 404 on a chat call — this project already
lost time to that (2026-09-09, gemini-2.5-flash-lite). Listed != servable, so
this makes the real call.

Used to pick a JUDGE model distinct from the answering model: a model grading
its own output invites self-preference bias, and on Gemini the free-tier quota
is per project AND PER MODEL, so a different judge also gets its own
requests-per-minute allowance instead of competing with the answers.
"""
import json
import os
import time
import urllib.request

BASE = os.environ["LLM_BASE_URL"]
KEY = os.environ["LLM_API_KEY"]
SYSTEM = os.environ.get("LLM_MODEL") or "(unset)"

CANDIDATES = [
    "models/gemini-flash-latest",
    "models/gemini-2.5-flash",
    "models/gemini-2.0-flash",
    "models/gemini-flash-lite-latest",
]

# The judge's real job, in miniature: return a strict JSON verdict. A model that
# chats fine but cannot hold the output format is useless as a judge — and would
# fail CLOSED as ERROR/unverified, which is safe but blocks every run.
PROMPT = ('Reply with ONLY this JSON and nothing else: '
          '{"verdict":"PASS","reason":"probe"}')

print(f"answering model = {SYSTEM}\n")
for mdl in CANDIDATES:
    body = json.dumps({"model": mdl,
                       "messages": [{"role": "user", "content": PROMPT}],
                       "max_tokens": 64}).encode()
    req = urllib.request.Request(BASE + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + KEY})
    t0 = time.time()
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=60).read())
        txt = (r["choices"][0]["message"]["content"] or "").strip()
        dt = time.time() - t0
        parses = False
        try:
            cleaned = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
            parses = json.loads(cleaned.strip()).get("verdict") == "PASS"
        except Exception:
            parses = False
        same = " <-- SAME AS ANSWERING MODEL (self-grading)" if mdl == SYSTEM else ""
        print(f"  SERVES  {mdl}  ({dt:.1f}s)  json_ok={parses}  {txt[:48]!r}{same}")
    except Exception as e:
        print(f"  FAILS   {mdl}  -> {str(e)[:100]}")
    time.sleep(4.5)   # respect the 15/min per-model free-tier ceiling
