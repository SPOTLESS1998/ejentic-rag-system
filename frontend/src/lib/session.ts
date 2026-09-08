/**
 * THE SESSION — how the browser proves, on each request, who logged in.
 *
 * After a successful login the browser must not hold the access code (a long-lived
 * bearer credential) and must not hold a backend API key (the whole reason the proxy
 * exists). So it holds neither: it gets a short-lived, signed statement of identity
 * that this server minted and only this server can verify.
 *
 * WHY HAND-ROLLED AND NOT A LIBRARY
 * ---------------------------------
 * This is ~60 lines of HMAC over JSON with a constant-time compare and an explicit
 * expiry check. The project's standing rule is no heavy frameworks, and a security
 * boundary is the worst place to add a dependency whose internals nobody here has
 * read. The tradeoff is that the classic footguns have to be avoided deliberately,
 * so each one is named below.
 *
 * THE FORMAT
 * ----------
 *     base64url(payload) "." base64url(HMAC-SHA256(secret, base64url(payload)))
 *
 * Deliberately NOT a JWT. A JWT carries its own algorithm in a header, and trusting
 * that header is the single most common way these are broken (`alg: none`, or
 * RS256 downgraded to HS256 with the public key as the secret). Here the algorithm
 * is not data — it is fixed in this file and cannot be influenced by the cookie.
 *
 * WHAT THE SIGNATURE DOES AND DOESN'T DO
 * --------------------------------------
 * The payload is signed, not encrypted: anyone can base64-decode the cookie and read
 * `{"id":"ada","role":"employee","exp":1234567890}`. That is fine — it is the person's
 * own id and tier, which they already know. What they cannot do is CHANGE it, because
 * any edit invalidates the signature. Never put anything actually secret in here.
 */
import { createHmac, timingSafeEqual } from "node:crypto";

/** Cookie name. The `__Host-` prefix would be stronger, but it requires Secure +
 *  path=/ + no Domain, which breaks plain-http local development; the flags below
 *  are set explicitly instead. */
export const SESSION_COOKIE = "rag_session";

/** How long a login lasts. One working day by default: long enough not to nag,
 *  short enough that a stolen cookie stops working on its own. */
const DEFAULT_TTL_SECONDS = 12 * 60 * 60;

export interface Session {
  /** Person id, as configured in RAG_STAFF. */
  id: string;
  /** The clearance tier this person reads at. */
  role: string;
  /** Expiry, epoch seconds. */
  exp: number;
}

/** Why a cookie was rejected. Useful in logs; never shown to the browser, which
 *  only ever learns "not signed in". */
export type SessionFailure =
  | "absent"
  | "malformed"
  | "bad-signature"
  | "expired"
  | "no-secret";

function ttlSeconds(): number {
  const raw = Number((process.env.RAG_SESSION_TTL_SECONDS ?? "").trim());
  // Clamp rather than trust: a typo'd 0 would mint sessions that expire instantly
  // (an infinite login loop), and a pasted 999999999 would mint ones that never do.
  if (!Number.isFinite(raw) || raw <= 0) return DEFAULT_TTL_SECONDS;
  return Math.min(Math.max(Math.floor(raw), 300), 7 * 24 * 60 * 60);
}

/**
 * The signing secret. **Absent means no session can be minted or verified** — this
 * throws rather than falling back to a default or a random per-process value.
 *
 * A random per-process secret would look like it worked: logins succeed, and then
 * everybody is silently signed out whenever the container restarts or a second
 * replica serves the request. A hardcoded default is worse — anyone with the source
 * could forge a session for any role. Refusing to start is the only honest option,
 * and it matches the backend refusing to boot without its keys.
 */
function secret(): string {
  const s = (process.env.RAG_SESSION_SECRET ?? "").trim();
  if (s.length < 32) {
    throw new Error(
      "RAG_SESSION_SECRET is missing or too short (need 32+ chars). " +
        "Generate one with `openssl rand -hex 32`. Refusing to sign sessions with " +
        "a weak or absent secret.",
    );
  }
  return s;
}

const b64url = (buf: Buffer | string): string =>
  Buffer.from(buf as never).toString("base64url");

function sign(encodedPayload: string): string {
  return createHmac("sha256", secret()).update(encodedPayload).digest("base64url");
}

/** Mint a signed session for a person. */
export function createSession(id: string, role: string): { value: string; maxAge: number } {
  const maxAge = ttlSeconds();
  const payload: Session = { id, role, exp: Math.floor(Date.now() / 1000) + maxAge };
  const encoded = b64url(JSON.stringify(payload));
  return { value: `${encoded}.${sign(encoded)}`, maxAge };
}

/**
 * Verify a cookie value and return the session, or a reason it was rejected.
 *
 * Order matters: the SIGNATURE is checked before the payload is parsed or trusted
 * for anything. Reading `exp` out of an unverified payload to decide expiry first
 * would mean acting on attacker-controlled data.
 */
export function readSession(
  value: string | null | undefined,
  now = Date.now(),
): { session: Session; failure?: undefined } | { session: null; failure: SessionFailure } {
  const raw = (value ?? "").trim();
  if (!raw) return { session: null, failure: "absent" };

  const dot = raw.lastIndexOf(".");
  if (dot < 1 || dot === raw.length - 1) return { session: null, failure: "malformed" };
  const encoded = raw.slice(0, dot);
  const presented = raw.slice(dot + 1);

  let expected: string;
  try {
    expected = sign(encoded);
  } catch {
    // No usable secret. Fail closed: nobody is signed in.
    return { session: null, failure: "no-secret" };
  }

  // Compare as fixed-width buffers. Unequal lengths make timingSafeEqual throw, so
  // check that first — and a length mismatch is already a definitive rejection, so
  // returning early there leaks nothing an attacker cannot see from the cookie
  // they sent.
  const a = Buffer.from(presented);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    return { session: null, failure: "bad-signature" };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
  } catch {
    return { session: null, failure: "malformed" };
  }
  if (typeof parsed !== "object" || parsed === null) {
    return { session: null, failure: "malformed" };
  }
  const { id, role, exp } = parsed as Record<string, unknown>;
  if (typeof id !== "string" || !id || typeof role !== "string" || !role || typeof exp !== "number") {
    return { session: null, failure: "malformed" };
  }
  if (!Number.isFinite(exp) || exp * 1000 <= now) {
    return { session: null, failure: "expired" };
  }
  return { session: { id, role, exp } };
}

/** Cookie attributes. Assembled here so every place that sets or clears the cookie
 *  cannot disagree about the flags — a logout that clears a cookie with different
 *  attributes than the one that was set does not clear it at all. */
function cookieAttributes(maxAge: number): string {
  // Secure by DEFAULT, opt OUT for local http. The reverse (opt in) means a
  // deployment that forgets one variable ships session cookies over plain HTTP.
  const insecure = (process.env.RAG_COOKIE_INSECURE ?? "").trim() === "1";
  const parts = [
    `Path=/`,
    `Max-Age=${maxAge}`,
    // HttpOnly: JavaScript cannot read it, so an XSS cannot exfiltrate the session.
    `HttpOnly`,
    // Lax, not Strict: Lax already withholds the cookie on cross-site POST (which
    // is what blocks CSRF against these endpoints), while Strict would also
    // withhold it when a staff member follows a link from Slack or email — they
    // would land looking signed out, reload, and be signed in, which reads as a bug.
    `SameSite=Lax`,
  ];
  if (!insecure) parts.push("Secure");
  return parts.join("; ");
}

export function sessionSetCookie(value: string, maxAge: number): string {
  return `${SESSION_COOKIE}=${value}; ${cookieAttributes(maxAge)}`;
}

/** Clear the cookie. Same attributes as when it was set, with Max-Age=0. */
export function sessionClearCookie(): string {
  return `${SESSION_COOKIE}=; ${cookieAttributes(0)}`;
}

/** Pull the session cookie out of a request. */
export function sessionFromRequest(request: Request, now = Date.now()) {
  const header = request.headers.get("cookie") ?? "";
  // Parse conservatively: split on ";", take the first exact name match. Cookie
  // values here are base64url + ".", so they never contain "=" ambiguity beyond
  // the first separator.
  for (const part of header.split(";")) {
    const item = part.trim();
    const eq = item.indexOf("=");
    if (eq < 1) continue;
    if (item.slice(0, eq) !== SESSION_COOKIE) continue;
    return readSession(item.slice(eq + 1), now);
  }
  return { session: null as null, failure: "absent" as SessionFailure };
}
