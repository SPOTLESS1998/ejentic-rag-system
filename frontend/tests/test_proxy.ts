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
 */
import {
  resolveCaller,
  backendHeaders,
  hasApiKey,
  callerError,
  passThroughError,
  ACTOR_HEADER,
  RAG_BASE_URL,
  type CallerResult,
  type Caller,
} from "../src/lib/rag-proxy.ts";
import { createSession, SESSION_COOKIE } from "../src/lib/session.ts";
import { keyEnvForRole } from "../src/lib/staff.ts";

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
  check("per-person mode -> true", hasApiKey() === true);
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

console.log(`\nRESULT: ${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
