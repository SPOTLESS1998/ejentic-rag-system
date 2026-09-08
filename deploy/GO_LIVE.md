# Going live with the RAG — the runbook

Written 2026-09-07. **Nothing in here has been executed.** It is written so that someone other than
the author could follow it, and so the two decisions that are not mine to make are made on purpose
rather than by default.

Companion to [`../DEPLOYMENT-PLAN.md`](../DEPLOYMENT-PLAN.md), which explains *why* each of these
steps exists. This file is the *how*.

---

## What this deploys

| | |
|---|---|
| Public entry point | **one** — Caddy on 443, terminating TLS, proxying to the Next.js UI |
| Next.js UI — public | container, published on **127.0.0.1:3002 only** |
| Next.js UI — internal *(optional)* | container, **127.0.0.1:3003 only**, per-person sign-in. Opt in with `--profile internal` |
| FastAPI backend | container, **publishes no port at all** — reachable only by the UI(s), over the internal Docker network |
| Secrets | `/etc/ejentic-rag/server.env`, plus `internal.env` for the staff tier. Root-owned `0600`, **outside the git checkout** |
| Survives reboot | yes — `systemd` unit, `Restart=always` |
| Password gate | **on by default** on the public site (see Decision 3) |

The **public** deployment has no login: it holds one key and answers at one clearance for everyone
who opens it. That is Decision 2, and it is the thing to understand before deploying anything.
Per-person sign-in does exist — but only on the optional **internal** deployment (step 9), which is
a separate container on a separate hostname.

---

## Before you start: three decisions

Two of these cost money or expose data, so they are yours. The defaults below are the safe,
reversible ones; you can deploy on the defaults today and revisit.

### Decision 1 — which box

**Recommended: the one already serving `audit.ejentic.xyz`.** Caddy is installed and proven there,
TLS is automatic, there is no new hosting bill, and it is one server to patch instead of two.

**What to check first — this is the only hard prerequisite:**

```bash
free -h          # RAM: budget ~4 GB free for the backend container (compose caps it at 4G)
df -h /          # disk: ~6 GB for images, more if you skip the CPU-only torch build below
docker --version # Compose v2 (`docker compose`, no hyphen)
caddy version    # decides `basicauth` vs `basic_auth` — see the Caddyfile
```

The RAM figure is a budget, not a measurement: the reranker is
`cross-encoder/ms-marco-MiniLM-L-6-v2` (~90 MB) plus torch, which is modest, but the FastAPI app,
llama-index and the embedding client sit alongside it. **Measure after the first boot**
(`docker stats`) rather than trusting this number. If the box is tight, the backend degrades
gracefully — it falls back to a dependency-free reranker — but you want to know that happened, not
discover it.

### Decision 2 — which clearance the UI answers at ⚠️

**This is the one that actually matters, and it is a policy question, not a technical one.**

One deployed UI holds one key, so it answers at **exactly one clearance for everyone who opens it**.
There is no "log in and see more" on the public site. The password gate in Decision 3 controls *who
reaches the page*; it does not change *what the page can see*.

| Audience | Reaches it how | Key |
|---|---|---|
| Public / prospects | the public hostname | **guest** ← the default |
| Staff | the internal hostname, signing in as themselves | whichever tier each person is configured for |
| You | the internal hostname, as yourself | executive |
| n8n Telegram flow | server-to-server, its own key | whichever tier that flow should answer at |

Set `RAG_UI_KEY` in `server.env` to the **guest** key unless you have deliberately decided
otherwise.

If staff genuinely need the internal tier, **that is now built** — it is the internal deployment in
step 9. A second container from the same image on its own hostname, where each person signs in with
their own generated code and the UI then acts at *that person's* tier. Turn it on with
`--profile internal`; leave it off and nothing about the public site changes at all.

Do **not** solve it by giving everyone the employee key and relying on the password to keep the
public out. That collapses two independent controls into one, and the day the password leaks you
lose both. It also destroys attribution: the audit trail would record "someone holding the employee
key", where the internal deployment records *which person asked*.

### Decision 3 — is it public at all

**Default: password-gated.** `deploy/Caddyfile.rag` puts basic auth in front of the whole site.
Gated is the default because it is the reversible direction: a password proves the hosting works
without publishing a demo you have not decided to publish, and removing it later is one line.
Publishing first and finding out you did not mean to is not reversible in the same way.

---

## Step by step

Steps 1–5 touch nothing live and can be redone freely. The site is not reachable until step 6.

### 1. Get the code on the box

```bash
sudo mkdir -p /opt/ejentic-rag
sudo chown "$USER":"$USER" /opt/ejentic-rag
git clone <repo-url> /opt/ejentic-rag
cd /opt/ejentic-rag
```

### 2. Generate a fresh set of keys — do NOT reuse the local ones

```bash
for role in GUEST EMPLOYEE EXECUTIVE; do
  printf 'RAG_KEY_%s=%s\n' "$role" "$(openssl rand -hex 32)"
done
```

⚠️ **Fresh, on the server.** A key that has sat in a laptop dotfile, been echoed into a terminal and
landed in shell history is not a production credential. `RAG_KEY_EXECUTIVE` is the entire security
boundary — it is the difference between "what is our refund policy" and "what was Q2 revenue".

Keep the three values somewhere you can find them again: you need the guest one for the UI and the
executive one for step 7's verification, and there is no way to recover them from the server later
except by reading the file.

### 3. Create the secret file

```bash
sudo mkdir -p /etc/ejentic-rag
sudo cp deploy/server.env.example /etc/ejentic-rag/server.env
sudo chmod 600 /etc/ejentic-rag/server.env
sudo chown root:root /etc/ejentic-rag/server.env
sudo -e /etc/ejentic-rag/server.env
```

Fill in: `RAG_CLIENT`, `PINECONE_API_KEY`, `NVIDIA_API_KEY`, the three `RAG_KEY_*` values,
`RAG_UI_KEY` (Decision 2 — the **value** of one of them, normally guest), and `RAG_CORS_ORIGINS`
(your real `https://` origin).

It lives in `/etc`, not in the checkout, on purpose: `git clean` cannot delete it and `git add -A`
cannot commit it.

### 4. Build

```bash
set -a; . /etc/ejentic-rag/server.env; set +a   # compose needs RAG_UI_KEY + RAG_CORS_ORIGINS
docker compose -f docker-compose.prod.yml build

# If disk or image-pull time is tight, cut ~2-3 GB of unused CUDA libraries:
#   docker compose -f docker-compose.prod.yml build \
#     --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
```

Sanity-check that no secret went into the image — this should print **nothing**:

```bash
docker run --rm --entrypoint sh "$(docker compose -f docker-compose.prod.yml images -q backend)" \
  -c 'ls -a /app | grep -x ".env" || echo "OK: no .env in the image"'
```

That check exists because both Dockerfiles end in `COPY . .`, and `backend/.env` sits right there.
Without `backend/.dockerignore` the build bakes every key into an image layer, where `docker history`
can read it back out — and no amount of runtime `--env-file` undoes it, because the secret is already
in the artifact. The `.dockerignore` files are load-bearing, not housekeeping.

### 5. Start it (still not reachable from outside)

```bash
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f backend
```

Wait for the backend to report healthy — first boot downloads the cross-encoder, so allow a few
minutes. Then, from the box itself:

```bash
curl -s http://127.0.0.1:3002/ -o /dev/null -w 'UI: %{http_code}\n'
docker compose -f docker-compose.prod.yml exec backend \
  python -c "import urllib.request,json; print(json.load(urllib.request.urlopen('http://127.0.0.1:8002/'))['auth'])"
```

That last line must show auth **enabled**. `GET /` deliberately reports the auth posture so an
unprotected instance is visible at a glance rather than assumed safe. If it says auth is off, stop
and fix it before going any further.

**If the backend refuses to boot:** that is very likely the fail-closed design working, not a broken
deploy. `auth.required` is `true` in the client config, and with the `RAG_KEY_*` variables missing the
server declines to start rather than serve an instance that cannot authenticate anyone. Check
`docker compose logs backend` before looking anywhere else.

### 6. Put Caddy in front — the point of no return

DNS first, or Caddy cannot get a certificate:

```
rag.<yourdomain>    A    <server-ip>
```

Then:

```bash
caddy version   # < 2.8 -> `basicauth` (one word);  >= 2.8 -> `basic_auth` (two words)
caddy hash-password   # run ON THE SERVER; never commit the hash
```

Paste the block from `deploy/Caddyfile.rag` into `/etc/caddy/Caddyfile`, replacing
`rag.example.com` and `REPLACE_WITH_BCRYPT_HASH`. **That server file is the single source of truth
for every hostname on the box** — one bad edit takes the other sites down with it, so always:

```bash
sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
sudo systemctl reload caddy      # reload, never restart
```

### 7. Verify the boundary through the real hostname

This is the part that matters. Run it against `https://rag.<yourdomain>`, not localhost — the whole
point is to prove the deployment did not weaken anything. Basic auth is in front, so pass `-u`.

```bash
H=https://rag.<yourdomain>
U='-u ejentic:<the-basic-auth-password>'
# The UI's proxy adds the key server-side; these hit the proxy the way the browser does.

# 1. Guest UI cannot reach executive material -> the gate REFUSES
curl -s $U -X POST "$H/api/rag/chat" -H 'Content-Type: application/json' \
  -d '{"query":"What was Q2 revenue?"}'          # escalation line, and NO $2.4M anywhere

# 2. Asking for MORE than the key grants -> 403, loud, never a silent downgrade
curl -s $U -o /dev/null -w '%{http_code}\n' -X POST "$H/api/rag/chat" \
  -H 'Content-Type: application/json' \
  -d '{"query":"hi","clearance_level":"executive"}'                        # 403

# 3. The browser bundle still holds no key — this must print nothing
curl -s $U "$H/" | grep -o 'RAG_KEY_[A-Z]*\|[0-9a-f]\{64\}' || echo "OK: no key in the HTML"

# 4. Streaming still streams (SSE not buffered by the proxy)
curl -sN $U -X POST "$H/api/rag/chat" -H 'Content-Type: application/json' \
  -d '{"query":"What services do you offer?"}' | head -5     # data: {"chunk"...} arriving early
```

Then the full matrix from **README §3a**, which needs direct backend access. The backend publishes no
port, so run it from inside the network rather than opening one:

```bash
docker compose -f docker-compose.prod.yml exec backend sh -c '
  code() { curl -s -o /dev/null -w "%{http_code}\n" "$@"; }
  echo -n "no key            -> "; code -X POST localhost:8002/api/rag -H "Content-Type: application/json" -d "{\"query\":\"hi\"}"
  echo -n "guest->executive  -> "; code -X POST localhost:8002/api/rag -H "X-API-Key: $RAG_KEY_GUEST" -H "Content-Type: application/json" -d "{\"query\":\"hi\",\"clearance_level\":\"executive\"}"
  echo -n "wrong tenant      -> "; code -X POST localhost:8002/api/rag -H "X-API-Key: $RAG_KEY_GUEST" -H "Content-Type: application/json" -d "{\"query\":\"hi\",\"client\":\"someone-else\"}"
  echo -n "metrics, no key   -> "; code localhost:8002/metrics
'
```

Expected: `401`, `403`, `409`, `401`. Anything else — especially a `200` on the first — means stop and
investigate before this goes any further.

Finally, prove the executive tier still *works*, so you know you tightened the boundary rather than
just breaking retrieval:

```bash
docker compose -f docker-compose.prod.yml exec backend sh -c '
  curl -s -X POST localhost:8002/api/rag -H "X-API-Key: $RAG_KEY_EXECUTIVE" \
    -H "Content-Type: application/json" -d "{\"query\":\"What was Q2 revenue?\"}"'
# a grounded answer WITH [Source N] citations
```

### 8. Survive a reboot

Only after step 7 passes:

```bash
sudo cp deploy/ejentic-rag.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ejentic-rag
sudo systemctl status ejentic-rag
sudo reboot          # then re-run step 7's first two checks
```

The reboot test is not optional theatre. `Requires=docker.service` exists precisely because this
races on boot and *only* on boot — it works every time you test it by hand.

### 9. The internal (staff) deployment — optional

Skip this entirely if the answer to Decision 2 was "guest only". Nothing above depends on it, and
the public site is unaffected either way.

Do this **after** step 7 passes. It reuses the same backend container, so the boundary has to be
proven working before another door is added to it.

**What you get:** a second UI on its own hostname where each person signs in with their own generated
code, and the UI then acts at *that person's* clearance tier. Same image as the public site — the
only difference is environment. `RAG_STAFF` is the hinge: unset, the code runs in single-key mode
exactly as the public deployment does.

**9a. Generate the session secret and one code per person.** All on the server:

```bash
printf 'RAG_SESSION_SECRET=%s\n' "$(openssl rand -hex 32)"
for who in ADA PETER; do printf 'RAG_CODE_%s=%s\n' "$who" "$(openssl rand -hex 32)"; done
```

Each person gets their **own** code — not one shared staff password. The reason is revocation:
removing one person becomes deleting one line, where a shared password can only be rotated for
everyone at once, which in practice means never. The audit trail also records *who* asked rather
than "someone with the staff password".

Give each person their code over something private. You cannot recover it later — only replace it.

**9b. Create the internal secret file:**

```bash
sudo cp deploy/internal.env.example /etc/ejentic-rag/internal.env
sudo chmod 600 /etc/ejentic-rag/internal.env
sudo chown root:root /etc/ejentic-rag/internal.env
sudo -e /etc/ejentic-rag/internal.env
```

Fill in `RAG_STAFF` (e.g. `ada=employee,peter=executive`), one `RAG_CODE_<ID>` per person, and
`RAG_SESSION_SECRET`. The role keys are **not** repeated here — compose passes them through from
`server.env`, so each key has exactly one home.

**9c. Start it:**

```bash
set -a; . /etc/ejentic-rag/server.env; set +a
docker compose -f docker-compose.prod.yml --profile internal up -d --build
```

The `--profile internal` is what makes this opt-in. Without it, `up -d` brings up exactly what it
did before — public UI plus backend.

**9d. DNS and Caddy.** Point the internal hostname at the box, then paste the **second** block from
`deploy/Caddyfile.rag` (the `rag-internal.example.com` one) into `/etc/caddy/Caddyfile`:

```bash
sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

That block has no `basicauth` on purpose — per-person sign-in *is* the boundary here, and a shared
password in front of it would both add a second credential to hold and quietly collapse the
per-person accountability back into "someone who had the staff password". The reasoning, and the
address-restriction alternative if you want the form hidden anyway, are in the Caddyfile itself.

**9e. Verify it — the part that matters.** Against the real hostname:

```bash
HI=https://rag-internal.<yourdomain>

# 1. Not signed in: the API refuses outright. Never a quiet public-tier answer.
curl -s -o /dev/null -w 'chat, no session:  %{http_code}\n' -X POST "$HI/api/rag/chat" \
  -H 'Content-Type: application/json' -d '{"query":"hi"}'                       # expect 401

# 2. A page request bounces to sign-in, rather than loading a chat box that 401s.
curl -s -o /dev/null -w 'page, no session:  %{http_code} -> %{redirect_url}\n' "$HI/"
                                                                               # expect 307 -> /login

# 3. A wrong code is refused, and reveals nothing about why.
curl -s -X POST "$HI/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"code":"definitely-not-a-real-access-code-at-all"}'                      # 401, generic message

# 4. A real code mints a session. Check the cookie flags on the way past.
curl -s -D - -o /dev/null -c /tmp/rag.jar -X POST "$HI/api/auth/login" \
  -H 'Content-Type: application/json' -d '{"code":"<ada-code>"}' | grep -i set-cookie
# must show ALL of: HttpOnly   SameSite=Lax   Secure
```

`HttpOnly` means an XSS cannot read the session; `Secure` means it never travels unencrypted;
`SameSite=Lax` is what blocks cross-site POSTs. If any of the three is missing, stop.

```bash
# 5. Signed in: WHO and WHAT tier are reported separately, from two different authorities.
curl -s -b /tmp/rag.jar "$HI/api/rag/whoami"
# signed_in:true, actor:"ada" (from the cookie this server signed),
# role:"employee"           (from the backend, derived from the key)

# 6. staff_problems MUST be empty. Anything listed there is a configured person who
#    cannot sign in — usually a tier whose RAG_KEY_* is missing.
curl -s -b /tmp/rag.jar "$HI/api/rag/whoami" | grep -o '"staff_problems":\[[^]]*\]'

# 7. The tier is real, not cosmetic: an employee still cannot read executive material.
curl -s -b /tmp/rag.jar -X POST "$HI/api/rag/chat" -H 'Content-Type: application/json' \
  -d '{"query":"What was Q2 revenue?"}'                        # a refusal, and NO $2.4M anywhere

# 8. A tampered session is rejected. Flip the last character of the cookie value.
S=$(sed -n 's/.*rag_session\t\(.*\)$/\1/p' /tmp/rag.jar)
curl -s -o /dev/null -w 'tampered cookie:   %{http_code}\n' \
  -b "rag_session=${S%?}X" -X POST "$HI/api/rag/chat" \
  -H 'Content-Type: application/json' -d '{"query":"hi"}'                        # expect 401

# 9. Still no key in anything the browser receives.
curl -s "$HI/login" | grep -o 'RAG_KEY_[A-Z]*\|[0-9a-f]\{64\}' || echo "OK: no key in the HTML"
```

Step 8 is the one that proves the whole design: the session payload is signed, not encrypted, so
anyone can read `{"id":"ada","role":"employee"}` out of their own cookie — and changing `employee`
to `executive` invalidates the signature. If a tampered cookie ever returns `200`, the tier boundary
is gone.

Then sign in as a second person at a different tier and confirm they get a *different* answer to
the same question. Two people, two tiers, one URL — that is the thing the public deployment cannot
do, and the only reason this second container exists.

---

## Routine operations

### Rotating a key

Any key that has been shared with anyone is a thing that may need revoking. There is no revocation
list — rotation *is* revocation.

```bash
sudo -e /etc/ejentic-rag/server.env        # replace the value(s)
sudo systemctl restart ejentic-rag         # picks up EnvironmentFile + restarts containers
```

Then re-run step 7. **If you rotate the key the UI holds, update `RAG_UI_KEY` in the same edit** —
they are two variables holding one value, and changing only one leaves the UI authenticating with a
key the server no longer knows. The symptom is a UI that loads fine and 401s on every question.

### Removing or changing someone's access (internal tier)

Editing who may sign in is editing one file and restarting one container:

```bash
sudo -e /etc/ejentic-rag/internal.env      # RAG_STAFF: delete a line to remove someone,
                                            # change their role to move their tier
docker compose -f docker-compose.prod.yml --profile internal up -d frontend-internal
```

**This takes effect on that person's *next request*, not whenever their cookie expires.** Their
session cookie stays validly signed for up to the session TTL (12h by default), but the proxy
re-reads `RAG_STAFF` on every request and serves the tier the *config* now says — never the tier
baked into the cookie at login. A removed person is signed out on their next click; a demoted one
drops to their lower tier immediately, and their cookie is never honoured at the higher one.

Because of that, you do **not** rotate `RAG_SESSION_SECRET` to remove one person — that invalidates
*everyone's* cookie and signs the whole team out at once. Rotate the secret only if you believe the
secret itself leaked. To also retire that person's *code* so it can never sign in again, clear their
`RAG_CODE_<ID>` in the same edit — removing them from `RAG_STAFF` already stops the code working, but
clearing it leaves nothing behind to leak.

Confirm it after the restart, using that person's old cookie against any `/api/rag/*` route:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/rag.jar -X POST "$HI/api/rag/chat" \
  -H 'Content-Type: application/json' -d '{"query":"hi"}'        # expect 401
```

The JSON body says which: `Your access has changed` for a removal, `Your access level has changed`
for a tier move. Either way the answer is a refusal, never a quiet downgrade to a narrower tier.

### Deploying a change

```bash
cd /opt/ejentic-rag && git pull
sudo systemctl restart ejentic-rag     # the unit's ExecStartPre rebuilds first
```

The rebuild is in the unit deliberately. A `restart` that skips it serves the old image from a new
checkout — which is how this project once had a stale build answering on `:8002` for 25 hours while
the fixed code sat unused on disk.

### Rolling back

```bash
cd /opt/ejentic-rag && git log --oneline -10
git checkout <known-good-sha>
sudo systemctl restart ejentic-rag
```

The audit DB is on a named volume (`rag_data`), so it is untouched by rollbacks and by
`docker compose down`. Only `docker compose down -v` destroys it — which is also the only way to
delete the audit trail, so treat that flag accordingly.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Backend exits immediately at boot | Almost always the fail-closed check: `auth.required` is true and a `RAG_KEY_*` is missing or empty. `docker compose logs backend` says so. |
| Caddy won't load the config | `basicauth` vs `basic_auth` — the directive was renamed in Caddy 2.8. `caddy version`, then match. |
| Compose refuses to start, complains about a variable | Working as designed. `RAG_UI_KEY` and `RAG_CORS_ORIGINS` use the `:?` form so an unset value stops the deploy instead of quietly bringing up an unauthenticated UI. |
| UI loads, every question 401s | `RAG_UI_KEY` does not match any current `RAG_KEY_*` — typically a rotation that updated only one of the two. |
| Answers arrive all at once after a long pause | SSE is being buffered. Check `flush_interval -1` survived the paste into `/etc/caddy/Caddyfile`. |
| Answers time out around 30s | A proxy timeout below the backend's `LLM_TIMEOUT` (default 300s). The supplied Caddy block allows 330s. |
| `libgomp.so.1: cannot open shared object file` | Torch's OpenMP runtime missing. `Dockerfile.prod` installs `libgomp1`; the dev Dockerfile does not. |
| Reranking looks worse than local | Check the logs for `[rerank] Local sentence-transformers cross-encoder active`. If torch failed to load, the pipeline degrades to the dependency-free reranker **silently** rather than erroring. |

---

## Still open after this

Deliberately not done here, because each needs something I should not decide alone:

- **`backend/eval_report.json` is dated 2026-08-12.** It claims 19/19 and `any_leak: false`, and it
  predates every change since — including all of the auth work. **Do not quote it as current.**
  Regenerating it spends real tokens, so it needs an explicit go-ahead.
- **The n8n Telegram flow** already carries `X-API-Key` and a `RAG_KEY` reference but has never run
  against an authenticated server. Point it at the deployed hostname with its own key (Decision 2
  applies again: whichever tier that flow should answer at) and walk the Telegram path end to end.
- **Whether to run the staff tier at all.** The internal deployment is *built* (step 9) and its
  auth boundary is tested, but running it is still a deliberate choice: it stands up a second
  internet-facing surface and means handing out per-person codes. Skip it entirely if guest-only is
  the answer to Decision 2 — nothing else depends on it.
