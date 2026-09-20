/**
 * Offline tests for src/lib/rag-proxy.ts — the module that decides which backend key
 * a request acts with, and forbids the browser from choosing one for itself.
 *
 * Run:  npm run test:proxy
 *
 * No network, no keys, no test framework (Node 24 strips types natively). Every
 * scenario builds a real `Request`, mints a real signed cookie, and calls the real
 * `resolveCaller` — nothing is stubbed, because the thing under test IS the wiring.
 *
 * WHAT IS ACTUALLY BEING PROVEN
 * -----------------------------
 *   • MODE SELECTION — with RAG_STAFF unset the proxy is in single-key mode and never
 *     consults a session; setting RAG_STAFF is the only thing that flips it to
 *     per-person. A regression here either breaks the public site or, worse, leaves
 *     the staff site trusting the single key.
 *
 *   • THE COOKIE SAYS WHO, THE CONFIG SAYS WHAT — this is the reason this file exists.
 *     A validly-signed cookie asserting a tier must NOT be served at that tier if the
 *     current config no longer agrees. Removal and demotion have to take effect on the
 *     NEXT request, not whenever the cookie happens to expire (up to 12h later). These
 *     tests mint a genuinely valid cookie (secret unchanged, so it still verifies) and
 *     then mutate only the config — so a 401 here can ONLY come from the config
 *     re-check, never from a signature failure. That is why each one asserts the exact
 *     reason string, not merely the 401.
 *
 *   • THE PROXY, NOT THE CALLER, CHOOSES THE KEY — backendHeaders must let the caller's
 *     resolved key and actor OVERWRITE anything a route accidentally passed through, so
 *     a client-supplied X-API-Key can never reach the backend.
 *
 *   • THE PROXY, NOT THE CALLER, CHOOSES THE RATE-LIMIT BUCKET — same property, smaller
 *     key. A caller who can name their own X-Visitor-Id rotates it per request and the
 *     per-visitor share stops existing. So the header is stripped on the way out, the
 *     cookie it comes from is re-validated on the way in, and staff are bucketed by the
 *     person id rather than by a browser.
 */
import {
  resolveCaller,
  resolveVisitor,
  backendHeaders,
  hasApiKey,
  callerError,
  passThroughError,
  ACTOR_HEADER,
  VISITOR_HEADER,
  VISITOR_COOKIE,
  RAG_BASE_URL,
  type CallerResult,
  type Caller,
} from "../src/lib/rag-proxy.ts";
import { createSession, SESSION_COOKIE } from "../src/lib/session.ts";
import { keyEnvForRole, staffConfig } from "../src/lib/staff.ts";

let pass = 0;
let fail = 0;

function check(name: string, cond: boolean): void {
  if (cond) {
    pass += 1;
    console.log(`  ok   ${name}`);
  } else {
    fail += 1;
    console.log(`  FAIL ${name}`);
  }
}

function section(title: string): void {
  console.log(`\n${title}`);
}

/** Wipe every RAG_* var, then apply the given ones. Keeps one test's environment from
 *  leaking into the next — the whole suite hinges on RAG_STAFF being exactly what each
 *  scenario sets. */
function setEnv(vars: Record<string, string> = {}): void {
  for (const k of Object.keys(process.env)) {
    if (k.startsWith("RAG_")) delete process.env[k];
  }
  for (const [k, v] of Object.entries(vars)) process.env[k] = v;
}

const SECRET = "0123456789abcdef0123456789abcdef"; // exactly 32 chars — session.ts floor
const CODE = "abcdefabcdefabcdefabcdefabcdef01"; // 32 chars, past the 24-char minimum
const EMP_KEY = "backend-employee-key";
const EXEC_KEY = "backend-executive-key";

/** A complete two-person staff deployment: ada (employee), peter (executive). */
function staffEnv(extra: Record<string, string> = {}): void {
  setEnv({
    RAG_SESSION_SECRET: SECRET,
    RAG_STAFF: "ada=employee,peter=executive",
    RAG_CODE_ADA: CODE,
    RAG_CODE_PETER: CODE,
    RAG_KEY_EMPLOYEE: EMP_KEY,
    RAG_KEY_EXECUTIVE: EXEC_KEY,
    ...extra,
  });
}

/** Build a POST the way the browser would, optionally carrying a session cookie. */
function requestWith(cookieValue: string | null): Request {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (cookieValue !== null) headers["cookie"] = `${SESSION_COOKIE}=${cookieValue}`;
  return new Request("http://internal.local/api/rag/chat", { method: "POST", headers });
}

/** Denial view of a CallerResult, or null when it was allowed. Lets a test assert the
 *  status AND the reason without fighting the discriminated union at every call site. */
function denied(r: CallerResult): { status: number; detail: string } | null {
  return r.ok ? null : { status: r.status, detail: r.detail };
}

/** How many configured people this deployment cannot serve. Pairs with hasApiKey():
 *  "configured but unservable" must show up as a problem, not as silence. */
function staffProblemCount(): number {
  return staffConfig().problems.length;
}

// ── SINGLE-KEY (PUBLIC) MODE ────────────────────────────────────────────────────
// RAG_STAFF unset. The deployment serves everyone with one key and never looks at a
// session — this is the internet-facing surface, and it must behave exactly as it did
// before per-person login existed.
section("single-key mode ignores sessions entirely");
{
  setEnv({ RAG_API_KEY: "the-one-key" });
  const r = resolveCaller(requestWith(null));
  check("no login configured -> allowed", r.ok === true);
  if (r.ok) {
    check("serves the single RAG_API_KEY", r.caller.key === "the-one-key");
    check("role is null (backend /whoami is the authority)", r.caller.role === null);
    check("actor is null (nobody is individually identified)", r.caller.actor === null);
  }
}
{
  // A stray session cookie must not flip a public deployment into per-person mode.
  // Only RAG_STAFF does that — a cookie from some other origin sharing the browser
  // (or a leftover from when this deployment was internal) must be completely inert.
  // The secret is present so the cookie genuinely verifies; RAG_STAFF is absent, so it
  // is ignored anyway. That is the point: signature-valid but mode says don't look.
  setEnv({ RAG_API_KEY: "the-one-key", RAG_SESSION_SECRET: SECRET });
  const { value } = createSession("ada", "executive");
  const r = resolveCaller(requestWith(value));
  check("a valid session cookie is ignored when RAG_STAFF is unset",
    r.ok === true && r.caller.role === null && r.caller.key === "the-one-key");
}
{
  // No key set at all is NOT rejected here: /whoami reports key_configured:false so an
  // operator can tell "no key in this deployment" from "key rejected".
  setEnv({});
  const r = resolveCaller(requestWith(null));
  check("single-key mode with no key -> allowed with empty key", r.ok === true && r.ok && r.caller.key === "");
}

// ── PER-PERSON MODE: A COOKIE IS REQUIRED AND MUST VERIFY ───────────────────────
section("per-person mode requires a valid session");
{
  staffEnv();
  const r = resolveCaller(requestWith(null));
  const d = denied(r);
  check("no cookie -> 401", d?.status === 401);
  check("no cookie -> reason is 'Not signed in.'", d?.detail === "Not signed in.");
}
{
  staffEnv();
  const r = resolveCaller(requestWith("this.is-not-a-valid-cookie"));
  const d = denied(r);
  // A garbage cookie fails signature verification -> no session -> "Not signed in."
  // This is a DIFFERENT reason from the revocation cases below, and keeping them
  // distinct is what lets those cases prove the re-check (not a signature failure) is
  // what rejected them.
  check("garbage cookie -> 401", d?.status === 401);
  check("garbage cookie -> 'Not signed in.' (signature failure, not revocation)", d?.detail === "Not signed in.");
}
{
  staffEnv();
  const { value } = createSession("ada", "employee");
  const r = resolveCaller(requestWith(value));
  check("valid employee session -> allowed", r.ok === true);
  if (r.ok) {
    check("employee served the employee key", r.caller.key === EMP_KEY);
    check("employee role reported", r.caller.role === "employee");
    check("actor is the person id, for the audit trail", r.caller.actor === "ada");
  }
}
{
  staffEnv();
  const { value } = createSession("peter", "executive");
  const r = resolveCaller(requestWith(value));
  check("valid executive session -> served the executive key", r.ok === true && r.ok && r.caller.key === EXEC_KEY);
}

// ── THE REVOCATION RE-CHECK (why this file exists) ──────────────────────────────
// Each of these mints a genuinely valid cookie, then changes ONLY the config, leaving
// RAG_SESSION_SECRET untouched so the cookie still verifies. Any 401 therefore comes
// from the config re-check, which is exactly the property under test.
section("removal and demotion take effect on the next request, not at cookie expiry");
{
  // FIRED: ada is removed from RAG_STAFF while her cookie is still valid.
  staffEnv();
  const { value } = createSession("ada", "employee");
  process.env.RAG_STAFF = "peter=executive"; // ada gone; her cookie is untouched and still signs true
  const r = resolveCaller(requestWith(value));
  const d = denied(r);
  check("removed person with a still-valid cookie -> 401", d?.status === 401);
  check("removed -> 'Your access has changed.' (config re-check fired, not signature)",
    d?.detail === "Your access has changed. Please sign in again.");
  check("removed person is NOT served any key", r.ok === false);
}
{
  // DEMOTED: peter's cookie says executive, but the config now says employee. The
  // single most important assertion in the file — the stale higher tier must never win.
  staffEnv();
  const { value } = createSession("peter", "executive");
  process.env.RAG_STAFF = "ada=employee,peter=employee"; // peter demoted exec -> employee
  const r = resolveCaller(requestWith(value));
  const d = denied(r);
  check("demoted person -> 401", d?.status === 401);
  check("demoted -> 'Your access level has changed.'",
    d?.detail === "Your access level has changed. Please sign in again.");
  check("demoted person is NEVER served the executive key their cookie claims", r.ok === false);
}
{
  // PROMOTED: even a promotion must re-login. The cookie's role is never trusted as
  // authoritative in either direction — a mismatch is a mismatch.
  staffEnv();
  const { value } = createSession("ada", "employee");
  process.env.RAG_STAFF = "ada=executive,peter=executive"; // ada promoted; RAG_KEY_EXECUTIVE present
  const r = resolveCaller(requestWith(value));
  const d = denied(r);
  check("promoted person -> 401 (cookie role is stale in both directions)", d?.status === 401);
  check("promoted -> 'Your access level has changed.'",
    d?.detail === "Your access level has changed. Please sign in again.");
}
{
  // CODE CLEARED: ada's access code is removed, dropping her from entries (a "problem",
  // not an entry). She is treated as removed, not served.
  staffEnv();
  const { value } = createSession("ada", "employee");
  delete process.env.RAG_CODE_ADA;
  const r = resolveCaller(requestWith(value));
  const d = denied(r);
  check("access code cleared mid-session -> 401", d?.status === 401);
  check("code cleared -> 'Your access has changed.'",
    d?.detail === "Your access has changed. Please sign in again.");
}
{
  // TIER KEY PULLED: the backend key for ada's tier is removed. staffConfig() then drops
  // her from entries (fail-closed), so she is logged out — NOT served, and NOT a 503.
  // This is the case that proves pulling a key mid-session fails to re-login, not to a
  // silent downgrade or a broken-config error the browser would sit on.
  staffEnv();
  const { value } = createSession("ada", "employee");
  delete process.env.RAG_KEY_EMPLOYEE;
  const r = resolveCaller(requestWith(value));
  const d = denied(r);
  check("tier key pulled mid-session -> 401 (not 503, not served)", d?.status === 401);
  check("tier key pulled -> treated as removed", d?.detail === "Your access has changed. Please sign in again.");
  check("tier key pulled -> NOT served", r.ok === false);
}
// NOTE ON THE 503 BRANCH: resolveCaller has a defensive 503 for "in entries but no
// backend key". It is deliberately unreachable — staffConfig().entries EXCLUDES any
// role without a key, and both reads happen in one synchronous call, so a member who
// is in entries always has a key. The scenario just above is what actually happens when
// a key disappears: the member leaves entries and gets a 401. The 503 is kept only as a
// fail-closed backstop against a future change to staffConfig, so there is no reachable
// input to assert it against — proving that is itself the point.

// ── RAG_REQUIRE_LOGIN: SIGN-IN CANNOT SILENTLY SWITCH ITSELF OFF ────────────────
// RAG_STAFF is the on/off switch for the whole auth layer, and it fails in the
// dangerous direction: emptied or typo'd, the code drops to single-key mode. On the
// public site that is correct. On the internal site — which deliberately has no
// basic-auth password in front of it — it is the boundary vanishing. These prove the
// declaration is enforced, and equally that it does NOT touch the public deployment.
section("a deployment that declared per-person login refuses to serve without it");
{
  // THE SCENARIO THAT MATTERS: RAG_STAFF broke AND a shared key is present. Without
  // the guard this serves that key's tier to every visitor with no sign-in at all.
  setEnv({ RAG_REQUIRE_LOGIN: "1", RAG_API_KEY: "executive-tier-key" });
  const r = resolveCaller(requestWith(null));
  const d = denied(r);
  check("login required + no staff + stray shared key -> refused", r.ok === false);
  check("  ...as 503 (our config is broken, not their permission)", d?.status === 503);
  check("  ...and the stray key is NEVER served", r.ok === false);
  check("  ...reason names both variables so an operator can act",
    (d?.detail ?? "").includes("RAG_REQUIRE_LOGIN") && (d?.detail ?? "").includes("RAG_STAFF"));
}
{
  setEnv({ RAG_REQUIRE_LOGIN: "1" });
  check("login required + no staff + no key at all -> still refused", resolveCaller(requestWith(null)).ok === false);
}
{
  // The guard must not break the deployment it is protecting: with staff configured,
  // per-person mode behaves exactly as before.
  staffEnv({ RAG_REQUIRE_LOGIN: "1" });
  check("login required + staff configured + no cookie -> ordinary 401", denied(resolveCaller(requestWith(null)))?.status === 401);
  const { value } = createSession("ada", "employee");
  const r = resolveCaller(requestWith(value));
  check("login required + staff configured + valid session -> served normally",
    r.ok === true && r.caller.key === EMP_KEY);
}
{
  // THE PUBLIC DEPLOYMENT MUST BE UNAFFECTED. It never sets RAG_REQUIRE_LOGIN, so an
  // empty RAG_STAFF still means "single-key mode", exactly as before this guard existed.
  setEnv({ RAG_API_KEY: "guest-key" });
  const r = resolveCaller(requestWith(null));
  check("public deployment (no RAG_REQUIRE_LOGIN) is untouched -> still served",
    r.ok === true && r.caller.key === "guest-key");
}
{
  // Only the exact string "1" opts in. A half-set value must not be read as "on" —
  // that would silently 503 a public deployment — nor as "off" by accident.
  for (const v of ["", "0", "true", "yes", "2", " "]) {
    setEnv({ RAG_REQUIRE_LOGIN: v, RAG_API_KEY: "k" });
    check(`RAG_REQUIRE_LOGIN=${JSON.stringify(v)} does not enable the guard`, resolveCaller(requestWith(null)).ok === true);
  }
  setEnv({ RAG_REQUIRE_LOGIN: " 1 ", RAG_API_KEY: "k" });
  check("RAG_REQUIRE_LOGIN=' 1 ' is trimmed and DOES enable it", resolveCaller(requestWith(null)).ok === false);
}

// ── backendHeaders: THE PROXY CHOOSES THE KEY, NOT THE CALLER ───────────────────
section("backendHeaders never lets a client-supplied credential through");
{
  const caller: Caller = { key: "resolved-key", role: "employee", actor: "ada" };
  const out = backendHeaders(caller);
  check("resolved key becomes X-API-Key", out["X-API-Key"] === "resolved-key");
  check("resolved actor becomes X-Actor", out[ACTOR_HEADER] === "ada");
  check("no upload token -> no X-Upload-Token", !("X-Upload-Token" in out));
}
{
  // The safety property: even if a route handler accidentally forwarded the browser's
  // own X-API-Key/X-Actor via `extra`, the resolved caller's values overwrite them.
  const caller: Caller = { key: "resolved-key", role: "employee", actor: "ada" };
  const out = backendHeaders(caller, { "X-API-Key": "attacker-supplied", "X-Actor": "someone-else" });
  check("a client-supplied X-API-Key in extra is overwritten by the resolved key",
    out["X-API-Key"] === "resolved-key");
  check("a client-supplied X-Actor in extra is overwritten by the resolved actor",
    out[ACTOR_HEADER] === "ada");
}
{
  const caller: Caller = { key: "resolved-key", role: "employee", actor: "ada" };
  const out = backendHeaders(caller, {}, "upload-token-123");
  check("an upload token is forwarded when present", out["X-Upload-Token"] === "upload-token-123");
}
{
  // Single-key-with-no-key deployment: an empty key must not produce an empty
  // X-API-Key header (which would look like "authenticate as no-one" rather than
  // "send no credential").
  const caller: Caller = { key: "", role: null, actor: null };
  const out = backendHeaders(caller);
  check("empty key -> no X-API-Key header at all", !("X-API-Key" in out));
  check("null actor -> no X-Actor header", !(ACTOR_HEADER in out));
}

// ── hasApiKey ───────────────────────────────────────────────────────────────────
section("hasApiKey reflects whether the deployment can authenticate at all");
{
  setEnv({ RAG_API_KEY: "k" });
  check("single-key mode with a key -> true", hasApiKey() === true);
  setEnv({});
  check("single-key mode with no key -> false", hasApiKey() === false);
  staffEnv();
  check("per-person mode with servable staff -> true", hasApiKey() === true);
}
{
  // The case the old `return true` got wrong: staff are configured, but NOT ONE role
  // has a backend key, so staffConfig() drops everybody and nobody on earth can sign
  // in. /whoami's `key_configured` must not claim this deployment is configured — that
  // is precisely the moment an operator is reading it to find out what is broken.
  setEnv({
    RAG_SESSION_SECRET: SECRET,
    RAG_STAFF: "ada=employee,peter=executive",
    RAG_CODE_ADA: CODE,
    RAG_CODE_PETER: CODE,
    // deliberately no RAG_KEY_* at all
  });
  check("per-person mode where NO role has a key -> false (was wrongly true)", hasApiKey() === false);
  check("  ...and every configured person is reported as a problem", staffProblemCount() === 2);
}
{
  // Partially configured: employee has a key, executive does not. One person is
  // servable, so the deployment IS configured — and the other is a reported problem.
  setEnv({
    RAG_SESSION_SECRET: SECRET,
    RAG_STAFF: "ada=employee,peter=executive",
    RAG_CODE_ADA: CODE,
    RAG_CODE_PETER: CODE,
    RAG_KEY_EMPLOYEE: EMP_KEY,
  });
  check("per-person mode with one servable tier -> true", hasApiKey() === true);
  check("  ...and the unservable person is a reported problem", staffProblemCount() === 1);
}

// ── callerError / passThroughError ──────────────────────────────────────────────
section("error responses carry the status and detail through, not a blanket 500");
{
  const res = callerError({ status: 401, detail: "Not signed in." });
  check("callerError keeps the status", res.status === 401);
  const body = (await res.json()) as { error?: string };
  check("callerError puts detail under `error`", body.error === "Not signed in.");
}
{
  const res = callerError({ status: 503, detail: "no key for tier" });
  check("callerError passes a 503 through unchanged", res.status === 503);
}
{
  // A backend 403 must stay a 403 so the UI can say "your key can't do that" instead
  // of "the server is broken".
  const backend = Response.json({ detail: "forbidden by clearance" }, { status: 403 });
  const res = await passThroughError(backend);
  check("passThroughError preserves the backend status", res.status === 403);
  const body = (await res.json()) as { error?: string };
  check("passThroughError surfaces the backend detail", body.error === "forbidden by clearance");
}
{
  // A non-JSON backend error falls back to the status text rather than throwing.
  const backend = new Response("upstream boom", { status: 502, statusText: "Bad Gateway" });
  const res = await passThroughError(backend);
  check("non-JSON backend error -> status preserved", res.status === 502);
  const body = (await res.json()) as { error?: string };
  check("non-JSON backend error -> falls back to status text", body.error === "Bad Gateway");
}

// ── RAG_BASE_URL ────────────────────────────────────────────────────────────────
section("RAG_BASE_URL is normalised");
{
  // Evaluated once at import, so this only sanity-checks the shape: a non-empty string
  // with no trailing slash (the proxy appends paths like `${RAG_BASE_URL}/chat`).
  check("RAG_BASE_URL is a non-empty string", typeof RAG_BASE_URL === "string" && RAG_BASE_URL.length > 0);
  check("RAG_BASE_URL has no trailing slash", !RAG_BASE_URL.endsWith("/"));
}

// ── THE VISITOR BUCKET: X-Visitor-Id ────────────────────────────────────────────
// The backend meters a per-API-key ceiling with a per-visitor share nested inside it.
// The public UI holds ONE shared guest key, so without a per-visitor id every visitor
// on the internet shares one bucket and the first heavy one starves the site for the
// day. These prove the id is chosen by the proxy, is stable per browser, and is never
// mistaken for identity.

/** Build a request carrying arbitrary raw cookies and/or headers. The `requestWith`
 *  helper above only speaks session cookies; these tests need to forge both. */
function rawRequest(opts: { cookie?: string; headers?: Record<string, string> } = {}): Request {
  const headers: Record<string, string> = { "content-type": "application/json", ...(opts.headers ?? {}) };
  if (opts.cookie !== undefined) headers["cookie"] = opts.cookie;
  return new Request("http://public.local/api/rag/chat", { method: "POST", headers });
}

/** Pull one cookie's value out of a Set-Cookie string. */
function setCookieValue(setCookie: string, name: string): string | null {
  const m = new RegExp(`(?:^|;\\s*)${name}=([^;]*)`).exec(setCookie);
  return m ? m[1] : null;
}

/**
 * A request whose Cookie header carries bytes a real `Request` would never hold.
 *
 * undici validates header values in the constructor, so a CRLF-bearing cookie cannot
 * be built the normal way (the injection test asserts exactly that). Handing
 * `resolveVisitor` the header directly is the only way to exercise OUR check against
 * such a value — which is the point: the check must stand on its own, not lean on the
 * runtime happening to be strict.
 */
function requestWithRawCookie(raw: string): Request {
  return {
    headers: { get: (name: string) => (name.toLowerCase() === "cookie" ? raw : null) },
  } as unknown as Request;
}

/** A resolved single-key (public) caller: no person, so the visitor cookie branch. */
const PUBLIC_CALLER: Caller = { key: "guest-key", role: null, actor: null };

/** The contract, restated here literally rather than imported. A test that asserts a
 *  value against the very regex the code used to build it proves nothing; this is the
 *  spec written out by hand so a change to the code's pattern shows up as a failure. */
const CONTRACT = /^[A-Za-z0-9._-]{1,64}$/;

/** Resolve a public-mode visitor for the given request. */
function publicVisitor(request: Request) {
  const r = resolveCaller(request);
  if (!r.ok) throw new Error("expected public mode to resolve");
  return { visitor: resolveVisitor(request, r.caller), caller: r.caller };
}

section("backendHeaders never forwards a client-supplied X-Visitor-Id");
{
  // THE CORE PROPERTY. A caller who chooses their own bucket label rotates it on every
  // request and never reaches the per-visitor limit — the same class of failure as
  // supplying your own API key, which is why it is stripped in the same place.
  const caller: Caller = { key: "resolved-key", role: null, actor: null };
  const out = backendHeaders(caller, { [VISITOR_HEADER]: "attacker-chosen-bucket" }, null, "ours-12345");
  check("a client-supplied X-Visitor-Id is overwritten by the proxy's own",
    out[VISITOR_HEADER] === "ours-12345");
  check("  ...and the attacker's value appears nowhere in the outbound headers",
    !JSON.stringify(out).includes("attacker-chosen-bucket"));
}
{
  // HTTP header names are case-insensitive; the keys of a plain JS object are not. A
  // lowercase `x-visitor-id` slipped in via `extra` would survive a naive
  // `delete out["X-Visitor-Id"]` and reach the backend as a second, conflicting header.
  const caller: Caller = { key: "resolved-key", role: null, actor: null };
  const out = backendHeaders(caller, { "x-visitor-id": "sneaky-lowercase" }, null, "ours-12345");
  const values = Object.entries(out)
    .filter(([k]) => k.toLowerCase() === VISITOR_HEADER.toLowerCase())
    .map(([, v]) => v);
  check("a lowercase x-visitor-id is stripped too (headers are case-insensitive)",
    values.length === 1 && values[0] === "ours-12345");
}
{
  // "We chose nothing" must not degrade to "the caller chose". With no id of our own,
  // the client's value must still be removed rather than left in place.
  const caller: Caller = { key: "resolved-key", role: null, actor: null };
  const out = backendHeaders(caller, { [VISITOR_HEADER]: "attacker-chosen-bucket" });
  check("no visitor id resolved -> a client-supplied one is still stripped",
    !(VISITOR_HEADER in out));
  const anyCasing = Object.keys(out).some((k) => k.toLowerCase() === VISITOR_HEADER.toLowerCase());
  check("  ...in any casing", anyCasing === false);
}
{
  const caller: Caller = { key: "resolved-key", role: null, actor: null };
  const out = backendHeaders(caller, {}, null, "bucket-abc");
  check("a resolved visitor id becomes X-Visitor-Id", out[VISITOR_HEADER] === "bucket-abc");
  check("the other headers are untouched by the visitor logic", out["X-API-Key"] === "resolved-key");
}

section("per-person mode buckets by the person, not the browser");
{
  staffEnv();
  const { value } = createSession("ada", "employee");
  const request = requestWith(value);
  const r = resolveCaller(request);
  check("staff session resolves", r.ok === true);
  if (r.ok) {
    const v = resolveVisitor(request, r.caller);
    check("staff visitor id IS the person id (real per-person limiting)", v.id === "ada");
    check("  ...the same value already used for actor", v.id === r.caller.actor);
    check("staff mode sets NO cookie (the session already says who this is)", v.setCookie === null);
    check("staff id satisfies the contract", CONTRACT.test(v.id));
  }
}
{
  // A visitor cookie left in the browser by the public site must not displace the
  // person id on the staff deployment. Identity wins; the stray cookie is inert.
  staffEnv();
  const { value } = createSession("peter", "executive");
  const request = new Request("http://internal.local/api/rag/chat", {
    method: "POST",
    headers: { cookie: `${VISITOR_COOKIE}=leftover-from-public-site; ${SESSION_COOKIE}=${value}` },
  });
  const r = resolveCaller(request);
  check("staff session still resolves alongside a stray visitor cookie", r.ok === true);
  if (r.ok) {
    const v = resolveVisitor(request, r.caller);
    check("a stray visitor cookie is IGNORED in staff mode", v.id === "peter");
    check("  ...and is not re-issued", v.setCookie === null);
  }
}
{
  // staffConfig() constrains ids to [a-z0-9_-] but not their LENGTH, so an absurd id is
  // reachable through config. Truncating to 64 would merge two people sharing a prefix
  // into one bucket; hashing keeps one person to one bucket. Stability is the property
  // that matters — the same actor must map to the same id on every request.
  const longActor = "a".repeat(70);
  const caller: Caller = { key: "k", role: "employee", actor: longActor };
  const v1 = resolveVisitor(rawRequest(), caller);
  const v2 = resolveVisitor(rawRequest(), caller);
  check("an over-long actor id is reduced to a contract-valid id", CONTRACT.test(v1.id));
  check("  ...deterministically (same actor -> same bucket every request)", v1.id === v2.id);
  check("  ...and is not a truncation of the original", v1.id !== longActor.slice(0, 64));
  const other: Caller = { key: "k", role: "employee", actor: "a".repeat(69) + "b" };
  check("  ...two long ids sharing a 64-char prefix do NOT collide",
    resolveVisitor(rawRequest(), other).id !== v1.id);
  check("  ...and still no cookie is issued for a staff caller", v1.setCookie === null);
}

section("single-key mode mints an opaque bucket id and persists it");
{
  setEnv({ RAG_API_KEY: "guest-key" });
  const { visitor, caller } = publicVisitor(rawRequest());
  check("a first-time visitor gets an id", typeof visitor.id === "string" && visitor.id.length > 0);
  check("the minted id satisfies the charset/length contract", CONTRACT.test(visitor.id));
  check("  ...and is comfortably inside the 64-char cap", visitor.id.length <= 64);
  check("a cookie is issued so the same browser returns to the same bucket", visitor.setCookie !== null);
  check("the cookie carries exactly the minted id",
    setCookieValue(visitor.setCookie ?? "", VISITOR_COOKIE) === visitor.id);
  // Requirement 4: a random browser id is not a person, and must never be recorded as
  // one. The public deployment's audit trail stays anonymous.
  check("minting a visitor id does NOT populate actor", caller.actor === null);
  const out = backendHeaders(caller, {}, null, visitor.id);
  check("  ...and the visitor id does NOT leak into X-Actor", !(ACTOR_HEADER in out));
  check("  ...it travels only in X-Visitor-Id", out[VISITOR_HEADER] === visitor.id);
}
{
  setEnv({ RAG_API_KEY: "guest-key" });
  // Two separate first-time visitors must land in different buckets, or the whole
  // exercise collapses back to one shared bucket.
  const a = publicVisitor(rawRequest()).visitor.id;
  const b = publicVisitor(rawRequest()).visitor.id;
  check("two first-time visitors get DIFFERENT ids", a !== b);
}
{
  setEnv({ RAG_API_KEY: "guest-key" });
  // THE REUSE PROPERTY: the browser sends the cookie back, and the same bucket is used
  // without re-issuing it. Without this the "limit" resets on every single request.
  const first = publicVisitor(rawRequest()).visitor;
  const minted = first.id;
  const second = publicVisitor(rawRequest({ cookie: `${VISITOR_COOKIE}=${minted}` })).visitor;
  check("a returning browser reuses the SAME bucket id", second.id === minted);
  check("  ...and no new cookie is issued", second.setCookie === null);
  const third = publicVisitor(rawRequest({ cookie: `theme=dark; ${VISITOR_COOKIE}=${minted}; x=1` })).visitor;
  check("  ...found among other cookies too", third.id === minted && third.setCookie === null);
}

section("the visitor cookie is HttpOnly, SameSite=Lax, and Secure on the same terms as the session");
{
  setEnv({ RAG_API_KEY: "guest-key" });
  const c = publicVisitor(rawRequest()).visitor.setCookie ?? "";
  check("HttpOnly (page JS and an XSS cannot read or rewrite the bucket)", c.includes("HttpOnly"));
  check("SameSite=Lax (same posture as the session cookie)", c.includes("SameSite=Lax"));
  check("Secure is on by DEFAULT (opt out, never opt in)", c.includes("Secure"));
  check("Path=/ so every route sees the same bucket", c.includes("Path=/"));
  check("a Max-Age is set — a session-scoped cookie would reset the bucket per tab", c.includes("Max-Age="));
  const maxAge = Number(/Max-Age=(\d+)/.exec(c)?.[1] ?? "0");
  check("  ...and it outlives a daily budget window by a wide margin", maxAge > 24 * 60 * 60);
}
{
  // The SAME escape hatch as session.ts, not a new one: one variable governs both
  // cookies, so a local http deployment cannot end up with one Secure and one not.
  setEnv({ RAG_API_KEY: "guest-key", RAG_COOKIE_INSECURE: "1" });
  check("RAG_COOKIE_INSECURE=1 drops Secure for local http",
    !(publicVisitor(rawRequest()).visitor.setCookie ?? "").includes("Secure"));
  setEnv({ RAG_API_KEY: "guest-key", RAG_COOKIE_INSECURE: "true" });
  check("only the exact string '1' opts out",
    (publicVisitor(rawRequest()).visitor.setCookie ?? "").includes("Secure"));
}
{
  setEnv({ RAG_API_KEY: "guest-key" });
  const c = publicVisitor(rawRequest()).visitor.setCookie ?? "";
  // It is a bucket label, not a credential. If this ever starts looking like the
  // session cookie, someone will eventually treat it as one.
  check("the visitor cookie is NOT the session cookie", !c.startsWith(`${SESSION_COOKIE}=`));
  check("  ...it is its own name", c.startsWith(`${VISITOR_COOKIE}=`));
}

section("the visitor cookie is re-validated on the way in — it is client input like any other");
{
  // HttpOnly stops PAGE SCRIPTS touching the cookie. It does not stop a scripted client
  // sending whatever bytes it likes in a Cookie header. So a value read back out of our
  // own cookie is untrusted, and a bad one is replaced rather than forwarded.
  setEnv({ RAG_API_KEY: "guest-key" });
  const bad: [string, string][] = [
    ["too long", "a".repeat(65)],
    ["disallowed char (slash)", "abc/def"],
    ["disallowed char (space)", "abc def"],
    ["disallowed char (colon)", "abc:def"],
    ["empty", ""],
    ["non-ascii", "abcédef"],
  ];
  for (const [label, value] of bad) {
    const v = publicVisitor(rawRequest({ cookie: `${VISITOR_COOKIE}=${value}` })).visitor;
    check(`a cookie value that is ${label} is rejected and replaced`,
      v.id !== value && CONTRACT.test(v.id) && v.setCookie !== null);
  }
}
{
  // THE INJECTION CASE. An unvalidated cookie value flows into an outbound request
  // header; a CR or LF in it would be header injection against the backend.
  //
  // There are two independent guards, and this asserts both rather than assuming
  // either. Relying only on the platform's would mean the check quietly stops being
  // load-bearing the day this code runs somewhere less strict.
  setEnv({ RAG_API_KEY: "guest-key" });
  const evil = "abc\r\nX-API-Key: stolen";

  // GUARD 1 — the runtime. undici refuses to construct a Request holding a CRLF
  // header value at all, so such a value cannot arrive through a real request.
  let runtimeRefused = false;
  try {
    new Request("http://public.local/", { headers: { cookie: `${VISITOR_COOKIE}=${evil}` } });
  } catch {
    runtimeRefused = true;
  }
  check("the runtime itself refuses to build a Request with a CRLF header value", runtimeRefused);

  // GUARD 2 — ours. Fed the value directly (the only way to get past guard 1), the
  // contract still rejects it, so nothing carrying a header separator goes outbound.
  const v = resolveVisitor(requestWithRawCookie(`${VISITOR_COOKIE}=${evil}`), PUBLIC_CALLER);
  check("a CRLF-bearing cookie value never becomes the outbound id", v.id !== evil);
  check("  ...the replacement contains no CR or LF", !/[\r\n]/.test(v.id));
  check("  ...and it satisfies the contract", CONTRACT.test(v.id));
  check("  ...and a fresh cookie replaces the poisoned one", v.setCookie !== null);
}
{
  // A cookie whose NAME merely resembles ours must not be picked up — the same
  // exact-name rule the session parser uses.
  setEnv({ RAG_API_KEY: "guest-key" });
  const v = publicVisitor(rawRequest({ cookie: `${VISITOR_COOKIE}_other=borrowed; x${VISITOR_COOKIE}=borrowed` })).visitor;
  check("a near-miss cookie name is not treated as the visitor cookie", v.id !== "borrowed");
  check("  ...so a fresh id is minted instead", v.setCookie !== null);
}

console.log(`\nRESULT: ${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
