"""Live post-deploy verification — run INSIDE the backend container.

    docker exec ejentic-rag-backend-1 python /tmp/verify_deploy.py

Runs in-container because that is where PINECONE / NVIDIA / RAG_KEY_* live. It
reads keys from the environment and never prints one.

WHAT THIS IS CHECKING, AND THE ORDER IT HAS TO RUN IN
-----------------------------------------------------
Three commits land here: a retrieval-layer clearance guard (touches the security
path), per-request bounds, and a rate limiter (behaviour that has never run
against a real server). So:

  * THE CONTROL COMES FIRST AND IT IS THE WHOLE POINT. A refusal proves nothing
    on its own — broken retrieval also refuses everything, and every leak
    assertion still reads green. So the same executive question is asked twice,
    minutes apart, same index, same server, only the key differing. Guest must
    refuse it AND executive must answer it with the real figures. Only the pair
    proves the refusal is the filter working rather than retrieval being dead.

  * The rate-limit burst runs LAST, and on the EXECUTIVE key, because it
    exhausts a bucket. Running it first would poison every later check, and
    running it on guest would 429 the bucket that real visitors share.

  * The burst uses GREETINGS. `_is_greeting` short-circuits before retrieval, so
    a greeting costs zero tokens while still being charged to the limiter. That
    is how we prove the 429 fires without spending 31 LLM generations against a
    free-tier quota.
"""
import json
import os
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8002"
GUEST = os.environ.get("RAG_KEY_GUEST", "")
EXEC = os.environ.get("RAG_KEY_EXECUTIVE", "")
CANARIES = ["2.4", "12M", "$12", "310k", "OmniScrape"]

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    ok = bool(cond)
    if ok:
        PASS += 1
        print(f"  PASS  {name}" + (f"  ({extra})" if extra else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  ({extra})" if extra else ""))
    return ok


def post(path, body, key=None, timeout=180):
    """Returns (status, body, headers).

    ⚠️ headers is the raw email.Message, NOT dict(...). HTTP header names are
    case-insensitive and uvicorn emits them lowercase, so `dict(e.headers)`
    gives {'retry-after': '19'} and a lookup for 'Retry-After' returns None.
    That exact mistake made this script report a missing Retry-After header on
    2026-09-20 while the server was sending it correctly. A check that cries
    wolf is worse than no check — you learn to ignore its output.
    """
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"X-API-Key": key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers
    except Exception as e:  # noqa: BLE001
        return 0, f"TRANSPORT: {e}", {}


print("\n[0] health + the new boot state")
try:
    with urllib.request.urlopen(BASE + "/", timeout=30) as r:
        h = json.loads(r.read().decode())
    check("backend answers /", r.status == 200)
    check("auth is still REQUIRED", h["auth"]["required"] is True)
    check("all three roles still have keys",
          not h["auth"]["roles_missing_keys"], f"missing={h['auth']['roles_missing_keys']}")
except Exception as e:  # noqa: BLE001
    check("backend answers /", False, str(e))

print("\n[1] retrieval still WORKS (the clearance guard did not break it)")
s, b, _ = post("/api/rag", {"query": "What services does Ejentic AI offer?"}, GUEST)
ans = (json.loads(b).get("response", "") if s == 200 else b)
check("guest gets a real answer", s == 200 and len(ans) > 200, f"status={s} len={len(ans)}")
check("it is grounded (cites a source)", "[Source" in ans or "Source" in ans,
      ans[:90].replace("\n", " "))

print("\n[2] the boundary still HOLDS (guest is refused)")
s, b, _ = post("/api/rag", {"query": "What was Q2 revenue and the acquisition offer?"}, GUEST)
g_ans = json.loads(b).get("response", "") if s == 200 else b
leaked = [c for c in CANARIES if c in g_ans]
check("guest query returns 200 (a refusal, not an error)", s == 200, f"status={s}")
check("NO executive figure leaked to guest", not leaked, f"leaked={leaked}")

print("\n[3] THE CONTROL — executive gets the same question ANSWERED")
print("     (without this, [2] is satisfied just as well by broken retrieval)")
s, b, _ = post("/api/rag", {"query": "What was Q2 revenue and the acquisition offer?"}, EXEC)
e_ans = json.loads(b).get("response", "") if s == 200 else b
found = [c for c in CANARIES if c in e_ans]
check("executive query returns 200", s == 200, f"status={s}")
check("executive DOES see the restricted figures", bool(found), f"found={found}")
check("=> so guest's refusal is the FILTER, not dead retrieval",
      bool(found) and not leaked)

print("\n[4] widening is still refused")
s, b, _ = post("/api/rag", {"query": "hi", "clearance_level": "executive"}, GUEST)
check("guest asking for executive is 403", s == 403, f"status={s}")
check("the 403 body leaks no figures", not any(c in b for c in CANARIES))

print("\n[5] NEW — the query length cap, refused before anything is spent")
s, b, _ = post("/api/rag", {"query": "x" * 5000}, GUEST)
check("an over-long query is 422", s == 422, f"status={s}")
s, b, _ = post("/api/rag", {"query": "x" * 5000})
check("...and 422 arrives even with NO key (refused before auth, costs nothing)",
      s == 422, f"status={s}")

print("\n[6] NEW — the rate limiter (executive bucket; greetings = zero tokens)")
codes, retry_after, n = [], None, 0
for i in range(45):
    s, b, hdrs = post("/api/rag", {"query": "hello"}, EXEC, timeout=60)
    codes.append(s)
    n += 1
    if s == 429:
        retry_after = hdrs.get("Retry-After")
        break
check("the limiter eventually returns 429", 429 in codes,
      f"after {n} requests; codes seen={sorted(set(codes))}")
check("the 429 carries Retry-After", (retry_after or "").isdigit(),
      f"Retry-After={retry_after!r} (case-insensitive lookup — see post())")
check("it did NOT fire on the first request (limit is not absurdly low)",
      codes[0] != 429, f"first={codes[0]}")

print("\n[7] per-role buckets — exhausting executive must NOT affect guest")
s, b, _ = post("/api/rag", {"query": "hello"}, GUEST, timeout=60)
check("guest still served after executive is rate-limited", s == 200, f"status={s}")

print(f"\n{'=' * 62}\n  LIVE VERIFY: {PASS} passed, {FAIL} failed\n{'=' * 62}")
raise SystemExit(1 if FAIL else 0)
