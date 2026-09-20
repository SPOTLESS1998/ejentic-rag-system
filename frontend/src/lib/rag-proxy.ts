/**
 * Server-only helpers for talking to the RAG backend.
 *
 * WHY A PROXY EXISTS AT ALL
 * -------------------------
 * The backend derives a caller's clearance from an API key. A key shipped to the
 * browser is a PUBLIC key — anyone can open devtools, read it, and hold whatever
 * clearance it grants. So the browser never sees a key: it calls our own
 * `/api/rag/*` routes, and this module (which only ever runs on the server) adds
 * the key on the way out.
 *
 * `RAG_API_KEY` must NOT be prefixed `NEXT_PUBLIC_` — that prefix is exactly the
 * mechanism that inlines a value into the client bundle.
 *
 * TWO MODES, AND THE HINGE BETWEEN THEM
 * -------------------------------------
 * A deployed UI holds one key, so it answers at one clearance for everyone who opens
 * it. That is fine for the public deployment and unacceptable for staff, so there are
 * two modes and `RAG_STAFF` is the only thing that selects between them:
 *
 *   SINGLE-KEY (public)   RAG_STAFF unset. Every visitor is served with `RAG_API_KEY`
 *                         — normally the guest key. No login, no session, nothing in
 *                         staff.ts or session.ts is consulted. Behaviour is exactly
 *                         what it was before per-person login existed.
 *
 *   PER-PERSON (internal) RAG_STAFF configured. Each request must carry a valid
 *                         session cookie; the person's role selects the backend key
 *                         (`RAG_KEY_<ROLE>`), and their id is forwarded for audit.
 *
 * Keeping the public path untouched matters: it is the internet-facing surface, and
 * it should hold the least-privileged credential in the system and nothing else. The
 * two deployments run the same image with different environments — see
 * docker-compose.prod.yml.
 */
// Relative imports with explicit ".ts" rather than the "@/lib/..." alias used
// elsewhere, so this module can be loaded by the plain-script test runner
// (`node tests/test_proxy.ts`), which has no knowledge of Next's path aliases. This is
// the file that decides which backend key a request acts with, so being directly
// testable is worth more here than matching the alias convention.
import { loginEnabled, backendKeyForRole, keyEnvForRole, staffConfig } from "./staff.ts";
import { sessionFromRequest, cookieAttributes, cookieFromRequest } from "./session.ts";
import { createHash, randomBytes } from "node:crypto";

/** Base URL of the FastAPI backend. Server-side, so no NEXT_PUBLIC_ needed. */
export const RAG_BASE_URL = (
  process.env.RAG_BACKEND_URL ??
  process.env.NEXT_PUBLIC_BACKEND_URL ??
  "http://localhost:8002"
).replace(/\/+$/, "");

/**
 * Header carrying WHO asked, for the backend's audit trail.
 *
 * ⚠️ Attribution only — never authorization. The backend still derives clearance
 * from the API key alone. Anyone holding the key could forge this header, so it is
 * exactly as trustworthy as the proxy that sets it, and the backend treats it that
 * way: it records the value and grants nothing on the strength of it.
 */
export const ACTOR_HEADER = "X-Actor";

/**
 * Header carrying WHICH BUCKET to meter this request against.
 *
 * The backend's rate limiter is two-tier: a ceiling per API key, and nested inside it
 * a share per visitor. The public UI holds ONE shared guest key, so without this
 * header every visitor on the internet shares a single bucket and the first heavy one
 * exhausts the whole site's daily budget for everybody else.
 *
 * ⚠️ NOT identity, and never attribution. This is deliberately a different header from
 * `X-Actor`: an actor is a person we authenticated, a visitor id is a bucket label.
 * The public deployment's value is a random number attached to a browser — it names
 * nobody, and it must never be written to the audit trail as though it did.
 */
export const VISITOR_HEADER = "X-Visitor-Id";

/**
 * Cookie holding the public deployment's visitor bucket id.
 *
 * ⚠️ NOT a session and NOT an auth token. It grants nothing, proves nothing, and is
 * never checked against anything — deleting it costs a visitor nothing but a fresh
 * rate-limit bucket. It carries NO personal data: the value is random bytes, minted
 * without reading anything about the person, their request, or their address.
 *
 * It is HttpOnly for the same reason the session cookie is — page JavaScript (or an
 * XSS) should not be able to read or rewrite it — but do not read that flag as a sign
 * this is a credential. If you are here looking for "who is this user", this is the
 * wrong cookie; on the public deployment the honest answer is that nobody knows.
 */
export const VISITOR_COOKIE = "rag_visitor";

/**
 * The contract with the backend: opaque, at most 64 chars, strictly `[A-Za-z0-9._-]`.
 *
 * Enforced on EVERY id before it goes out, including one read back from our own
 * cookie. A cookie is client-controlled input — HttpOnly stops page scripts touching
 * it, not a scripted client sending whatever it likes — so an unvalidated value would
 * flow straight into an outbound request header, where a CR/LF would be header
 * injection and a megabyte of junk would be an easy way to break the backend.
 */
const VISITOR_ID_PATTERN = /^[A-Za-z0-9._-]{1,64}$/;

/**
 * How long a visitor keeps the same bucket. Long, because a bucket that rotates often
 * is a rate limit that resets often — the whole point is that the same browser lands
 * in the same bucket tomorrow.
 *
 * Honest limit: a visitor who clears cookies, or a script that simply does not send
 * one, gets a fresh bucket. This tier cannot stop that and is not trying to. It stops
 * ONE ordinary heavy visitor from starving everyone else; the per-API-key ceiling it
 * nests inside is what bounds the deliberate abuser.
 */
const VISITOR_COOKIE_MAX_AGE = 180 * 24 * 60 * 60;

/** Who this request acts as, once resolved. */
export interface Caller {
  /** Backend key to authenticate with. Empty string when this deployment has none. */
  key: string;
  /** Clearance tier, when known (per-person mode). Null in single-key mode — there
   *  the backend's own /whoami is the authority on what the key grants. */
  role: string | null;
  /** Person id for audit attribution. Null when nobody is individually identified. */
  actor: string | null;
}

export type CallerResult =
  | { ok: true; caller: Caller }
  | { ok: false; status: number; detail: string };

/** The single key used in single-key (public) mode. */
function singleKey(): string {
  return (process.env.RAG_API_KEY ?? "").trim();
}

/**
 * Has this deployment DECLARED that it authenticates individual people?
 *
 * Set `RAG_REQUIRE_LOGIN=1` in internal.env (the template ships with it on). It is a
 * statement of intent, and it exists because `RAG_STAFF` is load-bearing in a direction
 * that fails silently: if it is emptied, commented out, or typo'd, `loginEnabled()`
 * simply goes false and the code drops into single-key mode. On the PUBLIC site that is
 * the correct behaviour. On the INTERNAL site it is the whole boundary disappearing —
 * and that site deliberately has no basic-auth password in front of it (see
 * deploy/Caddyfile.rag), precisely because per-person sign-in was supposed to BE the
 * fence. So the one host where this mistake is unmitigated is the one host where it
 * matters most.
 *
 * With this set, a deployment that cannot authenticate people refuses to serve instead
 * of quietly serving everyone at whatever tier a stray `RAG_API_KEY` grants.
 */
function requireLogin(): boolean {
  return (process.env.RAG_REQUIRE_LOGIN ?? "").trim() === "1";
}

/**
 * Resolve who this request acts as.
 *
 * In per-person mode a missing or invalid session is a 401 — and note what is NOT
 * done here: there is no fallback to `RAG_API_KEY`. A fallback would mean an expired
 * cookie quietly serves whatever that key grants instead of asking the person to
 * sign in, which is the silent-downgrade failure this whole layer exists to avoid.
 *
 * THE COOKIE SAYS WHO, THE CONFIG SAYS WHAT — RE-CHECKED EVERY REQUEST
 * -------------------------------------------------------------------
 * The signed cookie is trusted for the caller's *identity* only. Their *tier* is
 * looked up in `staffConfig()` on every request rather than read from the cookie,
 * because the cookie's role is a snapshot from login time and can go stale:
 *
 *   • Someone removed from `RAG_STAFF` (fired, or access revoked) still holds a
 *     validly-signed cookie until it expires — up to the session TTL, 12h default.
 *   • Someone demoted (executive → employee) holds a cookie asserting the OLD, higher
 *     tier for just as long.
 *
 * Trusting the cookie's role would mean either of those takes up to a whole TTL to
 * take effect. Re-reading the config here makes both take effect on the very next
 * request: the config is the authority, the cookie is only a claim about identity.
 * `staffConfig().entries` already excludes anyone whose role has no backend key, so
 * being in it is proof this deployment can actually serve them.
 */
export function resolveCaller(request: Request): CallerResult {
  if (!loginEnabled()) {
    // A deployment that declared itself an authenticating one must not become a
    // single-key one because a variable went missing. This is the only path by which
    // per-person sign-in can silently switch off, so it is the only place the
    // declaration is enforced.
    if (requireLogin()) {
      return {
        ok: false,
        status: 503,
        detail:
          "This deployment requires per-person sign-in (RAG_REQUIRE_LOGIN=1) but no staff " +
          "are configured (RAG_STAFF is empty or unparseable). Refusing to serve rather " +
          "than falling back to one shared key for everyone.",
      };
    }
    // Single-key mode. An absent key is not rejected here: /whoami reports
    // `key_configured: false` so an operator can tell "no key set in this
    // deployment" from "key rejected", which otherwise look identical.
    return { ok: true, caller: { key: singleKey(), role: null, actor: null } };
  }

  const { session } = sessionFromRequest(request);
  if (!session) {
    return { ok: false, status: 401, detail: "Not signed in." };
  }

  // Re-resolve against the CURRENT config, not the cookie's snapshot.
  const current = staffConfig().entries.find((e) => e.id === session.id);
  if (!current) {
    // The cookie is validly signed, but this person is no longer a servable entry:
    // removed from RAG_STAFF, their access code cleared, or the backend key for their
    // tier pulled — any of which drops them out of `entries`. Force a fresh sign-in,
    // which will then either succeed at their new tier or be refused outright. 401,
    // not 403: from the browser's side the session is simply no longer valid.
    return { ok: false, status: 401, detail: "Your access has changed. Please sign in again." };
  }
  if (current.role !== session.role) {
    // Demoted or promoted since this cookie was issued. Never serve the cookie's
    // (possibly higher) tier — re-login mints a cookie carrying the correct one.
    return {
      ok: false,
      status: 401,
      detail: "Your access level has changed. Please sign in again.",
    };
  }

  const key = backendKeyForRole(current.role);
  if (!key) {
    // Defensive backstop: staffConfig() only returns entries whose role HAS a backend
    // key, so a member reaching here without one should be impossible. Kept as a
    // fail-closed guard rather than an assumption — if that invariant ever changes,
    // this refuses to serve rather than dereferencing a null key.
    //
    // 503, not 403: it is our configuration that is broken, not their permission, and
    // the distinction is what tells an operator where to look.
    return {
      ok: false,
      status: 503,
      detail:
        `This deployment has no backend key for the "${current.role}" tier ` +
        `(expected ${keyEnvForRole(current.role)}). Signed in, but unable to serve you.`,
    };
  }
  return { ok: true, caller: { key, role: current.role, actor: current.id } };
}

/** The rate-limit bucket this request is metered against, once resolved. */
export interface Visitor {
  /** Value for `X-Visitor-Id`. Always satisfies VISITOR_ID_PATTERN. */
  id: string;
  /**
   * `Set-Cookie` the route must attach to its response, or null when there is
   * nothing to persist (staff mode, or a public visitor who already has a usable
   * cookie). Returned rather than written here because this module never sees a
   * Response — the route owns that, exactly as it does for the session cookie.
   */
  setCookie: string | null;
}

/** A fresh bucket id: 24 random bytes as base64url — 32 chars, alphabet `[A-Za-z0-9_-]`,
 *  so it is inside the contract's charset and well under the 64-char cap by
 *  construction. 192 bits is far past any chance of two browsers colliding. */
function mintVisitorId(): string {
  return randomBytes(24).toString("base64url");
}

/**
 * Turn a staff id into a contract-valid bucket id.
 *
 * `staffConfig()` already restricts ids to `[a-z0-9_-]`, which is inside the allowed
 * charset, so the overwhelmingly common case is the id passed through untouched —
 * and that is what makes the limit genuinely per-person.
 *
 * A pathological id (longer than 64 chars today, or some charset a future change to
 * staff.ts permits) is hashed rather than truncated. Truncating would silently merge
 * two people who share a 64-char prefix into one bucket, so each would see a limit
 * they did not spend; a digest keeps one person to one bucket and stays stable across
 * requests, which is the only property this value actually needs.
 */
function visitorIdForActor(actor: string): string {
  if (VISITOR_ID_PATTERN.test(actor)) return actor;
  return createHash("sha256").update(actor, "utf8").digest("hex").slice(0, 32);
}

/**
 * Decide which bucket to meter this request against.
 *
 * PER-PERSON (staff): the signed-in person's own id — the same value that goes out as
 * `actor` — so the limit is genuinely per person rather than per browser. No cookie is
 * involved or needed; the session already says who this is, and a stale visitor cookie
 * left in the browser by the public site is ignored outright.
 *
 * SINGLE-KEY (public): nobody is identified, so a random id is minted and persisted in
 * a cookie to give the same browser the same bucket next time.
 *
 * WHAT IS NOT TRUSTED
 * -------------------
 * A client-sent `X-Visitor-Id` header is never consulted — see `backendHeaders`, which
 * strips it. And the cookie, though it is ours, is validated on the way back in like
 * any other client input: a value that does not match the contract is discarded and
 * replaced rather than forwarded.
 */
export function resolveVisitor(request: Request, caller: Caller): Visitor {
  // Staff mode. `caller.actor` is only ever set on the per-person branch, so this is
  // the same condition that decides whether an actor is reported at all.
  if (caller.actor) {
    return { id: visitorIdForActor(caller.actor), setCookie: null };
  }

  const existing = cookieFromRequest(request, VISITOR_COOKIE);
  if (existing !== null && VISITOR_ID_PATTERN.test(existing)) {
    // Reuse, and do NOT re-issue the cookie: re-sending it on every response would
    // slide the expiry forward and add a Set-Cookie to every streamed answer for no
    // gain. The browser already holds it.
    return { id: existing, setCookie: null };
  }

  const id = mintVisitorId();
  return { id, setCookie: `${VISITOR_COOKIE}=${id}; ${cookieAttributes(VISITOR_COOKIE_MAX_AGE)}` };
}

/**
 * Headers for an outbound backend call: the caller's key, their id for audit, the
 * rate-limit bucket, plus a caller-supplied upload token when one is present.
 *
 * Note what is NOT forwarded: any client-sent `X-API-Key`, `X-Actor` or
 * `X-Visitor-Id`. If we passed the browser's headers through, a caller could supply
 * their own key (or claim to be someone else in the audit log) and the proxy would
 * become the open door it exists to close. A caller choosing their own visitor id is
 * the same failure in a smaller key: they would rotate it per request and never hit
 * the per-visitor share at all.
 */
export function backendHeaders(
  caller: Caller,
  extra: Record<string, string> = {},
  uploadToken?: string | null,
  visitorId?: string | null,
): Record<string, string> {
  const out: Record<string, string> = { ...extra };
  if (caller.key) out["X-API-Key"] = caller.key;
  if (caller.actor) out[ACTOR_HEADER] = caller.actor;
  if (uploadToken) out["X-Upload-Token"] = uploadToken;

  // Strip before setting, and compare case-insensitively: HTTP header names are
  // case-insensitive but the keys of a plain object are not, so a `x-visitor-id`
  // slipped in via `extra` would sail straight past a single `delete out[VISITOR_HEADER]`
  // and reach the backend. Deleting unconditionally also covers the path where we have
  // no id of our own to send — "we chose nothing" must not mean "the caller chose".
  const wanted = VISITOR_HEADER.toLowerCase();
  for (const k of Object.keys(out)) {
    if (k.toLowerCase() === wanted) delete out[k];
  }
  if (visitorId) out[VISITOR_HEADER] = visitorId;

  return out;
}

/** True when this deployment can authenticate to the backend at all. */
export function hasApiKey(): boolean {
  if (!loginEnabled()) return singleKey().length > 0;
  // Per-person mode has no single key, so the question becomes "can this deployment
  // serve anybody at all?". staffConfig() already drops anyone whose tier has no
  // backend key, so a non-empty entries list IS that check.
  //
  // This used to `return true` unconditionally, which reported `key_configured: true`
  // on a deployment where every RAG_KEY_* was missing and nobody could sign in — the
  // exact moment the diagnostic needed to be right.
  return staffConfig().entries.length > 0;
}

/** Turn a failed caller resolution into the response the browser sees. */
export function callerError(result: { status: number; detail: string }): Response {
  return Response.json({ error: result.detail }, { status: result.status });
}

/**
 * Forward a JSON error from the backend as-is.
 *
 * Deliberately passes the backend's status through: a 401/403/409 must stay
 * itself so the UI can say what actually happened. Collapsing everything to 500
 * turns "your key can't do that" into "the server is broken".
 */
export async function passThroughError(res: Response): Promise<Response> {
  let detail = res.statusText;
  try {
    const body = (await res.json()) as { detail?: string };
    if (body?.detail) detail = body.detail;
  } catch {
    /* non-JSON body */
  }
  return Response.json({ error: detail }, { status: res.status });
}
