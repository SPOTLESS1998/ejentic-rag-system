/**
 * WHO IS ASKING — the only place the internal deployment decides a person's identity.
 *
 * WHY THIS EXISTS
 * ---------------
 * The backend derives clearance from an API key, and the frontend proxy holds that
 * key server-side so the browser never sees one. That works perfectly for a
 * single-tier deployment, and it has one hard consequence: **one deployed UI holds
 * one key, so it answers at one clearance for everyone who opens it.** There is no
 * "log in and see more".
 *
 * This module adds the missing half for the STAFF deployment: it authenticates the
 * person, and the proxy then acts at that person's tier. The public deployment does
 * not load any of this — with `RAG_STAFF` unset, `loginEnabled()` is false and the
 * proxy falls back to the single `RAG_UI_KEY` exactly as before.
 *
 * THE MODEL — deliberately the same shape as backend/auth.py, one layer up
 * ----------------------------------------------------------------------
 * `RAG_STAFF` maps a person id to a ROLE (structure, not secret — safe to read):
 *
 *     RAG_STAFF="peter=executive,ada=employee,sam=employee"
 *
 * and each person's access code lives in its OWN env var, never in that string:
 *
 *     RAG_CODE_PETER=<openssl rand -hex 32>
 *     RAG_CODE_ADA=<openssl rand -hex 32>
 *
 * That is the same split the tenant config uses (`auth.keys` names env vars and
 * never holds their values), for the same reason: the structure can be read,
 * reviewed and committed; the secrets cannot.
 *
 * WHY PER-PERSON CODES AND NOT ONE SHARED STAFF PASSWORD
 * -----------------------------------------------------
 *   • Revocation. Removing one person is deleting one line. A shared password can
 *     only be rotated for everyone at once, which means in practice it never is.
 *   • Accountability. The audit row records WHO asked, not just "someone with the
 *     staff password".
 *   • No password rules to get wrong. A code is generated, not chosen, so there is
 *     nothing to hash, no reset flow, no user picking `ejentic2026`.
 *
 * A code is a bearer credential: whoever holds it is that person. It is compared in
 * constant time, never logged, and never sent to the browser after login — the
 * browser gets a signed session cookie instead (see session.ts).
 *
 * FAIL-CLOSED
 * -----------
 * A person configured with a role whose backend key is missing CANNOT be served, so
 * they are refused at login rather than silently downgraded to a narrower tier.
 * Silently downgrading is worse than refusing: the person believes they are seeing
 * internal material and quietly is not, which is how someone concludes "the
 * knowledge base doesn't have it" about a document that is right there.
 */
import { createHash, timingSafeEqual } from "node:crypto";

/** A configured staff member: an id, the tier they read at, and the env var that
 *  holds their access code. Never the code itself. */
export interface StaffEntry {
  id: string;
  role: string;
  codeEnv: string;
}

/** What is wrong with the configuration, if anything. Surfaced by /whoami so a
 *  half-configured deployment is visible instead of merely broken. */
export interface StaffConfig {
  entries: StaffEntry[];
  /** Ids that cannot be served, with the reason. These are refused at login. */
  problems: { id: string; reason: string }[];
}

/** Minimum length for an access code. 24 chars of hex is ~96 bits — far beyond
 *  guessing. The point of the floor is to catch a human-typed placeholder like
 *  "changeme", which would otherwise become a working credential. */
const MIN_CODE_LENGTH = 24;

/** Env var holding the backend key for a role, e.g. employee -> RAG_KEY_EMPLOYEE. */
export function keyEnvForRole(role: string): string {
  return `RAG_KEY_${role.trim().toUpperCase().replace(/[^A-Z0-9]+/g, "_")}`;
}

/** Env var holding a person's access code, e.g. ada -> RAG_CODE_ADA. */
export function codeEnvForId(id: string): string {
  return `RAG_CODE_${id.trim().toUpperCase().replace(/[^A-Z0-9]+/g, "_")}`;
}

/**
 * Parse `RAG_STAFF` into entries plus a list of what is unusable and why.
 *
 * Read fresh on every call rather than cached at module scope: Next evaluates
 * modules during the build, where none of these variables exist, so a module-level
 * snapshot would bake in "no staff configured" and never recover at runtime.
 */
export function staffConfig(): StaffConfig {
  const raw = (process.env.RAG_STAFF ?? "").trim();
  const entries: StaffEntry[] = [];
  const problems: { id: string; reason: string }[] = [];
  if (!raw) return { entries, problems };

  const seen = new Set<string>();
  for (const part of raw.split(",")) {
    const item = part.trim();
    if (!item) continue;

    const eq = item.indexOf("=");
    if (eq < 1) {
      problems.push({ id: item, reason: `expected "id=role", got "${item}"` });
      continue;
    }
    const id = item.slice(0, eq).trim().toLowerCase();
    const role = item.slice(eq + 1).trim().toLowerCase();

    if (!/^[a-z0-9_-]+$/.test(id)) {
      problems.push({ id, reason: "id may contain only a-z, 0-9, hyphen, underscore" });
      continue;
    }
    if (!role) {
      problems.push({ id, reason: "no role given" });
      continue;
    }
    if (seen.has(id)) {
      // Two entries for one id would make which tier they get depend on ordering.
      problems.push({ id, reason: "duplicate id" });
      continue;
    }
    seen.add(id);

    const codeEnv = codeEnvForId(id);
    const code = (process.env[codeEnv] ?? "").trim();
    if (!code) {
      problems.push({ id, reason: `no access code — set ${codeEnv}` });
      continue;
    }
    if (code.length < MIN_CODE_LENGTH) {
      problems.push({
        id,
        reason: `access code in ${codeEnv} is too short (${code.length} chars, need ${MIN_CODE_LENGTH}+)`,
      });
      continue;
    }
    // Can this deployment actually ACT at that tier? If the backend key for the
    // role is absent, serving this person is impossible; refusing at login is the
    // fail-closed answer, and silently narrowing them is the dangerous one.
    if (!(process.env[keyEnvForRole(role)] ?? "").trim()) {
      problems.push({
        id,
        reason: `role "${role}" has no backend key in this deployment — set ${keyEnvForRole(role)}`,
      });
      continue;
    }

    entries.push({ id, role, codeEnv });
  }
  return { entries, problems };
}

/** Does this deployment authenticate individual people?
 *
 *  False on the PUBLIC deployment, where `RAG_STAFF` is unset and the proxy uses
 *  the single `RAG_UI_KEY`. That is the compatibility hinge: everything in this
 *  module is inert unless a deployment opts in by configuring staff. */
export function loginEnabled(): boolean {
  return (process.env.RAG_STAFF ?? "").trim().length > 0;
}

/** Constant-time string comparison that tolerates unequal lengths.
 *
 *  `timingSafeEqual` THROWS when the buffers differ in length, and catching that
 *  would itself leak the length through timing. Hashing both sides to a fixed
 *  width first removes the length signal entirely — compare digests, not inputs. */
function sameSecret(a: string, b: string): boolean {
  const ha = createHash("sha256").update(a, "utf8").digest();
  const hb = createHash("sha256").update(b, "utf8").digest();
  return timingSafeEqual(ha, hb);
}

/**
 * Reverse-map a presented access code to the person it identifies.
 *
 * Returns null when it matches nobody — deliberately without saying whether the
 * code was unknown, belonged to a removed person, or was merely misconfigured.
 *
 * Every configured entry is compared even after a match, so the time taken does
 * not reveal WHICH person matched or how far down the list they sit. This mirrors
 * `resolve_role` in backend/auth.py exactly.
 */
export function resolveStaff(code: string | null | undefined): StaffEntry | null {
  const presented = (code ?? "").trim();
  const { entries } = staffConfig();
  let matched: StaffEntry | null = null;
  for (const entry of entries) {
    const expected = (process.env[entry.codeEnv] ?? "").trim();
    if (!expected) continue;
    if (sameSecret(presented, expected) && matched === null) matched = entry;
  }
  // An empty presented code must never match, even against a misconfigured entry.
  return presented ? matched : null;
}

/** The backend key to act with for a role, or null when this deployment has none. */
export function backendKeyForRole(role: string): string | null {
  const v = (process.env[keyEnvForRole(role)] ?? "").trim();
  return v || null;
}

/** Configuration posture, for /whoami. Names and reasons only — never a code, and
 *  never whether a particular code is valid. */
export function staffStatus(): {
  login_enabled: boolean;
  people: { id: string; role: string }[];
  problems: { id: string; reason: string }[];
} {
  const { entries, problems } = staffConfig();
  return {
    login_enabled: loginEnabled(),
    people: entries.map((e) => ({ id: e.id, role: e.role })),
    problems,
  };
}
