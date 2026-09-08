/**
 * Offline tests for src/lib/session.ts — how the browser proves who signed in.
 *
 * Run:  npm run test:session
 *
 * No network, no keys, no test framework (Node 24 strips types natively).
 *
 * WHAT IS ACTUALLY BEING PROVEN
 * -----------------------------
 * The session cookie is the only thing standing between "signed in as ada, employee
 * tier" and "signed in as anyone, at any tier". Since it is hand-rolled HMAC rather
 * than a library, the classic footguns have to be proven absent rather than assumed:
 *
 *   • FORGERY — a cookie signed with the wrong secret must be rejected.
 *   • ESCALATION — editing `role` in the payload must invalidate the signature. This
 *     is the single most important test in the file: if it ever goes green-to-red,
 *     anyone can promote themselves to the executive tier by editing a cookie.
 *   • PARSE-BEFORE-VERIFY — the payload must never be trusted before the signature is
 *     checked, so garbage that happens to be correctly signed is the only garbage that
 *     can reach the parser at all.
 *   • FAIL-CLOSED — no secret must mean "nobody is signed in", never "everybody is".
 *   • LOGOUT ACTUALLY LOGS OUT — a Set-Cookie whose attributes differ from the
 *     original does not clear the original, so the two must agree.
 */
import {
  SESSION_COOKIE,
  createSession,
  readSession,
  sessionSetCookie,
  sessionClearCookie,
  sessionFromRequest,
} from "../src/lib/session.ts";
import { createHmac } from "node:crypto";

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

function setEnv(vars: Record<string, string> = {}): void {
  for (const k of Object.keys(process.env)) {
    if (k.startsWith("RAG_")) delete process.env[k];
  }
  for (const [k, v] of Object.entries(vars)) process.env[k] = v;
}

const SECRET = "0123456789abcdef0123456789abcdef"; // exactly 32 chars — the floor
const OTHER_SECRET = "ffffffffffffffffffffffffffffffff";

/** Mint a cookie with an arbitrary payload and an arbitrary secret, the way an
 *  attacker would. Used to reach code paths a well-formed cookie cannot. */
function forge(payloadJson: string, secret: string): string {
  const enc = Buffer.from(payloadJson, "utf8").toString("base64url");
  const sig = createHmac("sha256", secret).update(enc).digest("base64url");
  return `${enc}.${sig}`;
}

const nowSec = () => Math.floor(Date.now() / 1000);

// ---------------------------------------------------------------------------
section("the signing secret is mandatory — no silent fallback");
// ---------------------------------------------------------------------------
setEnv({});
{
  let threw = false;
  try {
    createSession("ada", "employee");
  } catch {
    threw = true;
  }
  // A random per-process fallback would LOOK like it worked, then sign everyone out on
  // every container restart. A hardcoded default would let anyone with the source forge
  // an executive session. Throwing is the only honest option.
  check("no RAG_SESSION_SECRET -> createSession throws", threw);
}

setEnv({ RAG_SESSION_SECRET: "tooshort" });
{
  let threw = false;
  try {
    createSession("ada", "employee");
  } catch {
    threw = true;
  }
  check("secret under 32 chars -> createSession throws", threw);
}

setEnv({ RAG_SESSION_SECRET: SECRET.slice(0, 31) });
{
  let threw = false;
  try {
    createSession("ada", "employee");
  } catch {
    threw = true;
  }
  check("31-char secret -> rejected (boundary)", threw);
}

setEnv({ RAG_SESSION_SECRET: SECRET });
{
  let threw = false;
  try {
    createSession("ada", "employee");
  } catch {
    threw = true;
  }
  check("32-char secret -> accepted (boundary)", !threw);
}

// ---------------------------------------------------------------------------
section("round trip");
// ---------------------------------------------------------------------------
setEnv({ RAG_SESSION_SECRET: SECRET });
{
  const { value, maxAge } = createSession("ada", "employee");
  const read = readSession(value);
  check("a freshly minted session verifies", read.session !== null);
  check("id survives the round trip", read.session?.id === "ada");
  check("role survives the round trip", read.session?.role === "employee");
  check("default TTL is 12 hours", maxAge === 12 * 60 * 60);
  check("exp is roughly now + TTL", Math.abs((read.session?.exp ?? 0) - (nowSec() + maxAge)) <= 2);
  check("cookie has exactly one separator dot", value.split(".").length === 2);
}

// ---------------------------------------------------------------------------
section("forgery and privilege escalation");
// ---------------------------------------------------------------------------
setEnv({ RAG_SESSION_SECRET: SECRET });
{
  // THE test. Take a real employee session, rewrite the tier, keep the signature.
  const { value } = createSession("ada", "employee");
  const [enc, sig] = value.split(".");
  const payload = JSON.parse(Buffer.from(enc, "base64url").toString("utf8")) as Record<string, unknown>;

  check("payload IS readable — signed, not encrypted (so never put a secret in it)", payload.role === "employee");

  payload.role = "executive";
  const tampered = `${Buffer.from(JSON.stringify(payload), "utf8").toString("base64url")}.${sig}`;
  const read = readSession(tampered);
  check("editing `role` to escalate tier -> rejected", read.session === null);
  check("...and specifically as a bad signature", read.failure === "bad-signature");

  payload.role = "employee";
  payload.id = "peter";
  const impersonated = `${Buffer.from(JSON.stringify(payload), "utf8").toString("base64url")}.${sig}`;
  check("editing `id` to impersonate someone -> rejected", readSession(impersonated).session === null);
}

{
  // Signed correctly, but with a secret we do not hold.
  const outsider = forge(`{"id":"ada","role":"executive","exp":${nowSec() + 3600}}`, OTHER_SECRET);
  check("a cookie signed with a different secret -> rejected", readSession(outsider).session === null);
  check("...as a bad signature", readSession(outsider).failure === "bad-signature");
}

{
  const { value } = createSession("ada", "employee");
  const [enc, sig] = value.split(".");
  const flippedSig = `${enc}.${sig.slice(0, -1)}${sig.endsWith("A") ? "B" : "A"}`;
  check("one flipped character in the signature -> rejected", readSession(flippedSig).session === null);
  const truncated = `${enc}.${sig.slice(0, -4)}`;
  check("a truncated signature -> rejected (length mismatch, no throw)", readSession(truncated).session === null);
  const empty = `${enc}.`;
  check("an empty signature -> rejected", readSession(empty).session === null);
}

// ---------------------------------------------------------------------------
section("malformed input — nothing here may throw");
// ---------------------------------------------------------------------------
setEnv({ RAG_SESSION_SECRET: SECRET });
check("empty string -> absent", readSession("").failure === "absent");
check("whitespace only -> absent", readSession("   ").failure === "absent");
check("null -> absent", readSession(null).failure === "absent");
check("undefined -> absent", readSession(undefined).failure === "absent");
check("no dot -> malformed", readSession("notacookie").failure === "malformed");
check("leading dot -> malformed", readSession(".onlysig").failure === "malformed");
check("trailing dot -> malformed", readSession("onlypayload.").failure === "malformed");
check("a lone dot -> malformed", readSession(".").failure === "malformed");

// These are correctly SIGNED, so they get past the signature gate and exercise the
// payload validation that sits behind it — the only way to reach that code.
check("signed but non-JSON payload -> malformed", readSession(forge("not json at all", SECRET)).failure === "malformed");
check("signed JSON array -> malformed", readSession(forge("[1,2,3]", SECRET)).failure === "malformed");
check("signed JSON null -> malformed", readSession(forge("null", SECRET)).failure === "malformed");
check("signed JSON number -> malformed", readSession(forge("42", SECRET)).failure === "malformed");
check(
  "signed payload missing exp -> malformed",
  readSession(forge(`{"id":"ada","role":"employee"}`, SECRET)).failure === "malformed",
);
check(
  "signed payload with exp as a string -> malformed",
  readSession(forge(`{"id":"ada","role":"employee","exp":"9999999999"}`, SECRET)).failure === "malformed",
);
check(
  "signed payload with empty id -> malformed",
  readSession(forge(`{"id":"","role":"employee","exp":${nowSec() + 60}}`, SECRET)).failure === "malformed",
);
check(
  "signed payload with empty role -> malformed",
  readSession(forge(`{"id":"ada","role":"","exp":${nowSec() + 60}}`, SECRET)).failure === "malformed",
);
check(
  "signed payload with a non-finite exp -> rejected",
  readSession(forge(`{"id":"ada","role":"employee","exp":1e999}`, SECRET)).session === null,
);

// ---------------------------------------------------------------------------
section("expiry");
// ---------------------------------------------------------------------------
setEnv({ RAG_SESSION_SECRET: SECRET });
{
  const past = forge(`{"id":"ada","role":"employee","exp":${nowSec() - 10}}`, SECRET);
  check("an expired session -> rejected", readSession(past).session === null);
  check("...as expired, not as a bad signature", readSession(past).failure === "expired");

  const exp = nowSec() + 100;
  const cookie = forge(`{"id":"ada","role":"employee","exp":${exp}}`, SECRET);
  check("valid before exp", readSession(cookie, (exp - 1) * 1000).session !== null);
  check("expired exactly at exp (boundary is inclusive)", readSession(cookie, exp * 1000).failure === "expired");
  check("expired after exp", readSession(cookie, (exp + 1) * 1000).failure === "expired");
}

// ---------------------------------------------------------------------------
section("fail-closed when the secret disappears");
// ---------------------------------------------------------------------------
{
  setEnv({ RAG_SESSION_SECRET: SECRET });
  const { value } = createSession("ada", "employee");
  check("valid while the secret is present", readSession(value).session !== null);

  setEnv({}); // secret removed, e.g. a container restarted without its env file
  const read = readSession(value);
  // The dangerous alternative is treating an unverifiable cookie as valid.
  check("secret gone -> the previously valid session is NOT accepted", read.session === null);
  // Asserting the REASON, not just the rejection, is what makes this bite. Mutation
  // testing proved it: if secret() ever grew a hardcoded fallback, old cookies would
  // still be rejected (as bad-signature, since they were signed with the real secret),
  // so the assertion above would stay green while the deployment quietly became
  // forgeable by anyone who has read the source. Only the reason gives that away.
  check("...and the reason is recorded as no-secret", read.failure === "no-secret");
}

// ---------------------------------------------------------------------------
section("cookie attributes");
// ---------------------------------------------------------------------------
setEnv({ RAG_SESSION_SECRET: SECRET });
{
  const c = sessionSetCookie("abc.def", 3600);
  check("cookie uses the expected name", c.startsWith(`${SESSION_COOKIE}=`));
  check("Path=/ is set", c.includes("Path=/"));
  check("Max-Age is set", c.includes("Max-Age=3600"));
  check("HttpOnly is set (an XSS cannot read the session)", c.includes("HttpOnly"));
  check("SameSite=Lax is set (blocks cross-site POST, survives links from Slack)", c.includes("SameSite=Lax"));
  check("Secure is on by DEFAULT (opt out, never opt in)", c.includes("Secure"));
}

setEnv({ RAG_SESSION_SECRET: SECRET, RAG_COOKIE_INSECURE: "1" });
check("RAG_COOKIE_INSECURE=1 drops Secure for local http", !sessionSetCookie("a.b", 60).includes("Secure"));
setEnv({ RAG_SESSION_SECRET: SECRET, RAG_COOKIE_INSECURE: "0" });
check("RAG_COOKIE_INSECURE=0 keeps Secure", sessionSetCookie("a.b", 60).includes("Secure"));
setEnv({ RAG_SESSION_SECRET: SECRET, RAG_COOKIE_INSECURE: "true" });
check("only the exact string '1' opts out", sessionSetCookie("a.b", 60).includes("Secure"));

setEnv({ RAG_SESSION_SECRET: SECRET });
{
  // A logout that clears a cookie with different attributes than the one that was set
  // does not clear it at all — the browser keeps the original and the person stays
  // signed in. So every attribute except Max-Age must match exactly.
  const strip = (c: string) =>
    c
      .split("; ")
      .filter((p) => !p.startsWith("Max-Age=") && !p.startsWith(`${SESSION_COOKIE}=`))
      .sort()
      .join("; ");
  const setC = sessionSetCookie("a.b", 3600);
  const clearC = sessionClearCookie();
  check("clear cookie matches set cookie on every attribute but Max-Age", strip(setC) === strip(clearC));
  check("clear cookie expires immediately", clearC.includes("Max-Age=0"));
  check("clear cookie carries an empty value", clearC.startsWith(`${SESSION_COOKIE}=;`));
}

// ---------------------------------------------------------------------------
section("TTL configuration is clamped, not trusted");
// ---------------------------------------------------------------------------
const ttlFor = (raw?: string) => {
  setEnv(raw === undefined ? { RAG_SESSION_SECRET: SECRET } : { RAG_SESSION_SECRET: SECRET, RAG_SESSION_TTL_SECONDS: raw });
  return createSession("ada", "employee").maxAge;
};
check("unset -> 12h default", ttlFor() === 12 * 60 * 60);
check("empty -> 12h default", ttlFor("") === 12 * 60 * 60);
check("non-numeric -> 12h default", ttlFor("abc") === 12 * 60 * 60);
// A typo'd 0 would mint sessions that expire instantly: an infinite login loop.
check("zero -> 12h default, not an instant-expiry loop", ttlFor("0") === 12 * 60 * 60);
check("negative -> 12h default", ttlFor("-500") === 12 * 60 * 60);
check("a sane value is honoured", ttlFor("3600") === 3600);
check("below the floor is clamped up to 300s", ttlFor("60") === 300);
check("above the ceiling is clamped down to 7 days", ttlFor("999999999") === 7 * 24 * 60 * 60);
check("a fractional value is floored", ttlFor("3600.9") === 3600);

// ---------------------------------------------------------------------------
section("reading the cookie off a real Request");
// ---------------------------------------------------------------------------
setEnv({ RAG_SESSION_SECRET: SECRET });
{
  const { value } = createSession("ada", "employee");
  const req = (cookie: string) => new Request("http://localhost/", { headers: { cookie } });

  check("no cookie header at all -> absent", sessionFromRequest(new Request("http://localhost/")).failure === "absent");
  check("empty cookie header -> absent", sessionFromRequest(req("")).failure === "absent");
  check("our cookie alone -> found", sessionFromRequest(req(`${SESSION_COOKIE}=${value}`)).session?.id === "ada");
  check(
    "our cookie among others -> found",
    sessionFromRequest(req(`theme=dark; ${SESSION_COOKIE}=${value}; other=1`)).session?.id === "ada",
  );
  check(
    "cookie header with odd spacing -> found",
    sessionFromRequest(req(`  theme=dark ;   ${SESSION_COOKIE}=${value}  `)).session?.id === "ada",
  );
  // Name matching must be exact. A cookie whose name merely CONTAINS ours would
  // otherwise be read as a session, which an attacker who can set any cookie could use.
  check(
    "a cookie whose name only shares a prefix is ignored",
    sessionFromRequest(req(`${SESSION_COOKIE}_other=${value}`)).failure === "absent",
  );
  check(
    "a cookie whose name only shares a suffix is ignored",
    sessionFromRequest(req(`x${SESSION_COOKIE}=${value}`)).failure === "absent",
  );
  check("a valueless cookie entry is skipped", sessionFromRequest(req(`justaname; theme=dark`)).failure === "absent");
  check(
    "an expired cookie on the request -> expired",
    sessionFromRequest(req(`${SESSION_COOKIE}=${forge(`{"id":"ada","role":"employee","exp":${nowSec() - 5}}`, SECRET)}`))
      .failure === "expired",
  );
}

setEnv({});
console.log(`\nRESULT: ${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
