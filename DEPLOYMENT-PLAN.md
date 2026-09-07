# Deploying the RAG — the plan, and what has to be decided first

Written 2026-09-06, after the auth work landed and was live-verified on localhost.
**Updated 2026-09-07: the artifacts in "What I would change before deploying anything" are now
built** — see the status box below. The three decisions are still open, and nothing has been
deployed.

> ## Status — 2026-09-07
>
> **Built and verified locally (steps 2 of the sequence). Nothing live, nothing deployed.**
>
> | File | What it is |
> |---|---|
> | `backend/.dockerignore`, `frontend/.dockerignore` | **The important one** — see finding 4 below |
> | `docker-compose.prod.yml` | backend internal-only, loopback-published UI, no source mounts |
> | `backend/Dockerfile.prod` | non-root, no build toolchain at runtime, `libgomp1` for torch |
> | `frontend/Dockerfile.prod` | real production build (`output: standalone`), not `next dev` |
> | `frontend/next.config.ts` | `output: "standalone"` added |
> | `deploy/Caddyfile.rag` | site block, password-gated by default, SSE-safe |
> | `deploy/ejentic-rag.service` | systemd unit, rebuilds on restart, survives reboot |
> | `deploy/server.env.example` | template for the server's secret file (names only) |
> | `deploy/GO_LIVE.md` | the runbook — decisions, key generation, the verification matrix |
>
> Verified: `docker compose -f docker-compose.prod.yml config` resolves clean, and the resolved
> config confirms the backend publishes no port, the UI binds `127.0.0.1` only, there are no bind
> mounts, and n8n is absent. The missing-variable guards fail loudly (`exit 1`) rather than starting
> an unauthenticated UI. `npm run build` succeeds with `output: standalone`, and the key is absent
> from **every** file under `.next/` — leak check proven able to fail first, by planting a decoy key
> of the same shape and confirming it gets caught.
>
> **Not verified:** the Docker images do not build on this Mac — the Docker daemon is not running,
> only the CLI. So the `.dockerignore` files are verified by pattern coverage against the real file
> list, not against a real build context. `GO_LIVE.md` step 4 carries a one-command check that
> proves it against the actual image at deploy time; run it, do not skip it.
>
> **Still yours to decide:** Decisions 1, 2 and 3 below. Decision 2 is the one that matters.

---

## Where this stands today

The system is secure and proven, **on this Mac only**:

| | State |
|---|---|
| Auth | Enforced. Role comes from an API key, never the request body. |
| Verified | Full live matrix passed: 401 / 403 / 409, guest refused + executive answered. |
| Tests | 5 offline suites (no keys, no network) + `run_e2e_test.sh` (36 assertions, live). |
| Frontend | Works, holds the **guest** key server-side; key proven absent from all 16 browser bundles. |
| Reachable from the internet | **No.** `localhost:8002` and `localhost:3002`. |

So "deploy" here means one specific thing: **make it reachable by someone who is not sitting at this
laptop, without weakening any of the above.**

---

## Five things that are wrong for a deployment right now

Findings 1–3 were written on 2026-09-06. **Findings 4 and 5 were found on 2026-09-07 while building
the artifacts, and finding 4 is worse than anything else on this list.**

### 1. `docker-compose.yml` predates the auth work

It was written before any of this existed and is now actively wrong in three ways:

```yaml
ports:
  - "8002:8002"          # publishes the backend to the world, bypassing the proxy
environment:
  - NEXT_PUBLIC_BACKEND_URL=http://localhost:8002
```

- **`ports: 8002:8002` publishes the backend directly.** In a deployment the backend should be
  reachable *only* by the frontend and the proxy, on an internal network. Publishing it means the
  API is exposed alongside the UI. Auth still holds — this is not a hole — but it widens the attack
  surface for no benefit, and it means a misconfigured `CORS_ORIGINS` is suddenly load-bearing.
- **`NEXT_PUBLIC_BACKEND_URL` is the old variable name.** The proxy reads `RAG_BACKEND_URL` first
  and only falls back to this one. Worse, the `NEXT_PUBLIC_` prefix is exactly the thing the auth
  work exists to avoid: anything so prefixed is **inlined into the JavaScript sent to the browser.**
  It is only a URL today, so it is harmless — but leaving that variable in the file is an invitation
  for someone to add the *key* next to it the same way.
- **No `CORS_ORIGINS`, no `RAG_API_KEY`.** Neither service is told about the real domain or the
  frontend's key, so a deploy from this file gets localhost defaults.

### 2. `volumes: ./backend:/app` mounts the source into the container

That is a development convenience (edit locally, container sees it). In a deployment it means the
running code is whatever is on the host disk rather than what was built and tested — the exact
"is the running process the code I fixed?" trap that already bit us once today, when an old
checkout served :8002 for 25 hours.

### 3. `auth.required` is committed as `true` — good, but it makes the boot fail loudly

This is correct behaviour, not a bug, and it is worth knowing in advance: **the container will refuse
to start** if the key env vars are not present. That is the fail-closed design working. It will look
like a broken deploy the first time it happens.

### 4. ⚠️ There was no `.dockerignore`, so `docker build` baked every key into the image

**This is the finding that would have undone the entire auth project, and it was not in the original
plan.** Both Dockerfiles end with `COPY . .`. `backend/.env` exists on disk and holds
`PINECONE_API_KEY`, `NVIDIA_API_KEY` and all three `RAG_KEY_*` values. With no `.dockerignore`, every
one of those lands in an image layer.

Why that is worse than it sounds: an image layer is a **distributable artifact**. Anyone who can pull
the image — a registry, a teammate, a client you hand a build to — can read the values straight back
out with `docker history` or by extracting the layer. Passing `--env-file` at `docker run` does not
help, because the secret is already *in* the image. And the file would look completely fine: the
deployment works, auth works, every check in README §3a passes. The keys are simply also sitting in
the build output.

`RAG_KEY_EXECUTIVE` *is* the security boundary this whole system was rebuilt around. A boundary that
ships inside the artifact is not one.

Fixed: `backend/.dockerignore` and `frontend/.dockerignore`, which exclude `.env*` (keeping
`.env.example`), plus `data/`, `*.db`, `venv/`, `node_modules/` and `.next/`. `GO_LIVE.md` step 4
carries a one-command check against the built image, because pattern coverage on a laptop is not the
same as proof against a real build context.

### 5. The frontend Dockerfile runs `npm run dev`

```dockerfile
CMD ["npm", "run", "dev", "--", "-p", "3002"]
```

That is the Next.js development server: it compiles routes on demand, ships unminified code, and is
explicitly not built to face real traffic. Correct for a laptop, wrong for anything with a public
hostname.

Fixed in `frontend/Dockerfile.prod`: a multi-stage build using `output: "standalone"` (the documented
Docker path for this Next version), running `node server.js` as a non-root user. The dev Dockerfile
is left exactly as it was — the two want opposite things, and sharing one file means one of them is
always slightly wrong.

---

## The decisions that have to be made first

I am not making these unilaterally: two spend money and one is about who can read what.

### Decision 1 — where it runs

Recommendation: **the box that already serves `audit.ejentic.xyz`.**

The reasoning is that the pattern is already proven in this project. `lead-generation-system/deploy/`
holds a working Caddy + systemd deployment (`Caddyfile.audit`, `ejentic-approval.service`,
`GO_LIVE_RUNBOOK.md`) that is live today. Reusing it means: no new hosting bill, no new provider to
learn, TLS certificates handled by Caddy automatically, and one server to patch instead of two.

The alternative — a managed platform (Railway, Render, Fly) — is faster to a first URL but adds a
monthly cost, and the RAG's local cross-encoder reranker wants real RAM, which is where the cheap
tiers get unhappy.

**What this needs from you:** confirmation that the box has room. The reranker (`sentence-transformers`
cross-encoder) plus torch is the heavy part.

### Decision 2 — who gets which key

This is the one that actually matters, and it is a policy question, not a technical one.

The UI currently holds the **guest** key, so anyone who can open the page sees public material only.
That was the right default for localhost. For a deployment there are three separate audiences and
they are easy to conflate:

| Audience | Reaches it how | Key |
|---|---|---|
| Public / prospects | the website | **guest** |
| Staff | ? | employee |
| You | ? | executive |
| n8n Telegram flow | server-to-server | whichever tier that flow is meant to answer at |

**The trap:** a single deployed UI holds exactly one key, so it answers at exactly one clearance for
everyone who opens it. There is no "log in and see more" unless per-user login is built. If staff
need the internal tier, the honest options are (a) a second deployment on a private hostname holding
the employee key, or (b) real user login — a bigger piece of work.

**What this needs from you:** who should be able to reach it, and at what tier. Until that is
answered, the safe deployment is guest-only and public.

### Decision 3 — is it public at all?

Caddy can put HTTP basic auth in front of the whole thing (that is what `Caddyfile.audit` does for
the approval UI). A password-gated deployment is a reasonable first step: it proves the hosting works
without publishing a demo you have not decided to publish.

---

## What I would change before deploying anything

Small, contained, and all verifiable locally:

1. **A separate `docker-compose.prod.yml`** rather than editing the dev one — keep the convenient
   dev setup, add a deployment one. It would: drop `ports` on the backend (internal network only),
   drop the source-mount volumes, replace `NEXT_PUBLIC_BACKEND_URL` with `RAG_BACKEND_URL`, pass
   `CORS_ORIGINS` as the real domain, and pass `RAG_API_KEY` to the frontend from the server's env.
2. **A `deploy/` directory mirroring lead-gen's**: a `Caddyfile` for the RAG's hostname, a systemd
   unit, and a `GO_LIVE.md` written so someone else could follow it.
3. **A key-rotation note in the RUNBOOK.** Right now, rotating a key means editing `.env` and
   restarting. That is fine, but it should be written down, because the moment a key is shared with
   anyone it becomes a thing that may need revoking.
4. **Re-run `eval_rag.py`** — see below.

## Two open items unrelated to hosting

- **`backend/eval_report.json` is dated 2026-08-12.** It claims 19/19 passed and `any_leak: false`,
  and it predates every change made since — including all of the auth work. It should not be quoted
  as current, and it should be regenerated before anyone points at it as evidence. **It costs real
  tokens to regenerate, so it needs an explicit go-ahead.**
- **The n8n Telegram template** already carries `X-API-Key` and a `RAG_KEY` reference, but has not
  been run against the authenticated server. Worth a live check once a deployment exists.

---

## Sequence, once the decisions are made

1. Confirm the target box has RAM for the reranker.
2. Write `docker-compose.prod.yml` + `deploy/` (Caddyfile, systemd unit, GO_LIVE.md).
3. Generate a **separate set of keys for the server** — never reuse the local development keys.
4. Stand it up behind basic auth first. Walk the full README §3a matrix against the real hostname.
5. Remove basic auth only when the answer to Decision 3 is deliberate.
6. Point the n8n flow at it, verify the Telegram path end to end.
7. Regenerate `eval_report.json` (needs sign-off — real tokens).

Nothing in steps 2–3 touches a live system, so that part can proceed as soon as Decision 1 is
settled.
