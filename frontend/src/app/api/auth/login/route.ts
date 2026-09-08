/**
 * Sign in with a personal access code.
 *
 * The code is checked here, on the server, and never stored by the browser. What the
 * browser gets back is a signed, short-lived session cookie (see src/lib/session.ts)
 * — so a stolen cookie expires on its own, and it is not a backend credential.
 */
import { resolveStaff, loginEnabled, staffConfig } from "@/lib/staff";
import { createSession, sessionSetCookie } from "@/lib/session";

export const dynamic = "force-dynamic";

/**
 * Attempt throttling, per client address.
 *
 * The codes are 32 random bytes, so this is not what stops guessing — arithmetic
 * does. It exists to stop a broken script or a bored visitor from filling the logs
 * and burning CPU on hash comparisons.
 *
 * Known limits, stated rather than implied: it lives in this process's memory, so it
 * resets on restart and is not shared between replicas. That is acceptable for a
 * single internal container; if this ever runs replicated, this needs to move to
 * shared state.
 */
const WINDOW_MS = 15 * 60 * 1000;
const MAX_ATTEMPTS = 10;
const attempts = new Map<string, { count: number; resetAt: number }>();

function throttled(ip: string, now = Date.now()): boolean {
  const rec = attempts.get(ip);
  if (!rec || now > rec.resetAt) {
    attempts.set(ip, { count: 1, resetAt: now + WINDOW_MS });
    // Opportunistic cleanup so the map cannot grow without bound.
    if (attempts.size > 5000) {
      for (const [k, v] of attempts) if (now > v.resetAt) attempts.delete(k);
    }
    return false;
  }
  rec.count += 1;
  return rec.count > MAX_ATTEMPTS;
}

/** Best-effort client address. Behind Caddy the real address is in X-Forwarded-For;
 *  it is spoofable in general, which only means the throttle is best-effort too. */
function clientIp(request: Request): string {
  const fwd = request.headers.get("x-forwarded-for") ?? "";
  return (fwd.split(",")[0] ?? "").trim() || request.headers.get("x-real-ip") || "unknown";
}

export async function POST(request: Request) {
  if (!loginEnabled()) {
    // Deployment has no staff configured — there is nothing to sign in to. Saying so
    // plainly is safe (it reveals only which deployment you are talking to) and saves
    // an operator from debugging a login that was never meant to exist here.
    return Response.json(
      { error: "This deployment does not use per-person sign-in." },
      { status: 404 },
    );
  }

  const ip = clientIp(request);
  if (throttled(ip)) {
    return Response.json(
      { error: "Too many attempts. Wait a few minutes and try again." },
      { status: 429, headers: { "Retry-After": String(Math.ceil(WINDOW_MS / 1000)) } },
    );
  }

  let body: { code?: unknown };
  try {
    body = await request.json();
  } catch {
    return Response.json({ error: "Body must be JSON." }, { status: 400 });
  }
  const code = typeof body.code === "string" ? body.code : "";

  const person = resolveStaff(code);
  if (!person) {
    // Deliberately one message for every failure: unknown code, removed person,
    // misconfigured entry. Distinguishing them would tell an attacker which codes
    // are worth more attempts.
    //
    // The code itself is NEVER logged — not even truncated. A log line is a place
    // secrets go to be read later by someone who should not have them.
    console.warn(`[auth] failed sign-in attempt from ${ip}`);
    return Response.json({ error: "That code is not valid." }, { status: 401 });
  }

  // A configured-but-unusable person (their role has no backend key) is already
  // filtered out of staffConfig().entries, so resolveStaff cannot return one. Assert
  // it here anyway: if that invariant ever changes, this must fail closed rather than
  // mint a session the proxy will then reject on every request.
  const usable = staffConfig().entries.some((e) => e.id === person.id);
  if (!usable) {
    console.error(`[auth] ${person.id} resolved but is not serviceable — refusing`);
    return Response.json(
      { error: "Your account is not fully configured on this deployment." },
      { status: 503 },
    );
  }

  const { value, maxAge } = createSession(person.id, person.role);
  console.log(`[auth] ${person.id} signed in as ${person.role}`);
  return Response.json(
    { id: person.id, role: person.role, expires_in: maxAge },
    { status: 200, headers: { "Set-Cookie": sessionSetCookie(value, maxAge) } },
  );
}
