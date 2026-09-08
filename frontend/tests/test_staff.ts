/**
 * Offline tests for src/lib/staff.ts — WHO the internal deployment thinks you are.
 *
 * Run:  npm run test:staff
 *
 * No network, no keys, no test framework. Node 24 strips TypeScript types natively,
 * so this runs as a plain script — same shape as the backend's tests/ and lead-gen's,
 * and it honours the project's "no heavy frameworks" rule even in the test layer.
 *
 * WHAT IS ACTUALLY BEING PROVEN
 * -----------------------------
 * This module is a security boundary: it decides which backend key a request gets to
 * act with. The assertions below are grouped around the two ways that goes wrong:
 *
 *   1. Someone gets in who shouldn't  — resolveStaff matching a bad/blank code.
 *   2. Someone gets a tier they weren't configured for — the silent-downgrade and
 *      silent-upgrade paths. This is the subtler failure, and the one the
 *      "role has no backend key" tests exist for: a person configured for a tier this
 *      deployment cannot serve must be REFUSED, never quietly served at a narrower
 *      tier, or they will believe they searched internal material when they did not.
 *
 * Timing is deliberately NOT asserted. A unit test cannot measure constant-time
 * comparison reliably on a loaded laptop, and a flaky security test gets deleted.
 * What IS asserted is the property that made the constant-time path necessary: a
 * length-mismatched code must not throw (see the hash-then-compare note in staff.ts).
 */
import {
  keyEnvForRole,
  codeEnvForId,
  staffConfig,
  loginEnabled,
  resolveStaff,
  backendKeyForRole,
  staffStatus,
} from "../src/lib/staff.ts";

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

/**
 * Wipe the entire RAG_* namespace before each scenario.
 *
 * Without this a stray `export RAG_KEY_EMPLOYEE=...` in the developer's shell — or a
 * future .env that Node happens to load — could make a fail-closed test pass for
 * entirely the wrong reason. A security test that passes by accident is worse than no
 * test, so every scenario starts from a known-empty environment.
 */
function setEnv(vars: Record<string, string> = {}): void {
  for (const k of Object.keys(process.env)) {
    if (k.startsWith("RAG_")) delete process.env[k];
  }
  for (const [k, v] of Object.entries(vars)) process.env[k] = v;
}

/** 32 hex chars — what `openssl rand -hex 16` produces, comfortably over the floor. */
const CODE_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90";
const CODE_B = "0f9e8d7c6b5a40312e1d0c9b8a776655";
/** Under MIN_CODE_LENGTH (24). Stands in for a human-typed placeholder. */
const SHORT_CODE = "changeme123";
const KEY_EMP = "backend-key-for-employee-tier";
const KEY_EXEC = "backend-key-for-executive-tier";

// ---------------------------------------------------------------------------
section("env var name derivation");
// ---------------------------------------------------------------------------
check("keyEnvForRole: employee -> RAG_KEY_EMPLOYEE", keyEnvForRole("employee") === "RAG_KEY_EMPLOYEE");
check("keyEnvForRole: trims and upper-cases", keyEnvForRole("  Executive  ") === "RAG_KEY_EXECUTIVE");
check(
  "keyEnvForRole: non-alphanumerics collapse to underscore",
  keyEnvForRole("data-analyst") === "RAG_KEY_DATA_ANALYST",
);
check("codeEnvForId: ada -> RAG_CODE_ADA", codeEnvForId("ada") === "RAG_CODE_ADA");
check("codeEnvForId: hyphens collapse", codeEnvForId("mary-jane") === "RAG_CODE_MARY_JANE");

// ---------------------------------------------------------------------------
section("loginEnabled — the single hinge between public and internal mode");
// ---------------------------------------------------------------------------
setEnv({});
check("RAG_STAFF unset -> login disabled (public deployment)", loginEnabled() === false);
setEnv({ RAG_STAFF: "" });
check("RAG_STAFF empty -> login disabled", loginEnabled() === false);
setEnv({ RAG_STAFF: "   " });
check("RAG_STAFF whitespace-only -> login disabled", loginEnabled() === false);
setEnv({ RAG_STAFF: "ada=employee" });
check("RAG_STAFF configured -> login enabled", loginEnabled() === true);

// ---------------------------------------------------------------------------
section("staffConfig — a fully configured person");
// ---------------------------------------------------------------------------
setEnv({ RAG_STAFF: "ada=employee", RAG_CODE_ADA: CODE_A, RAG_KEY_EMPLOYEE: KEY_EMP });
{
  const { entries, problems } = staffConfig();
  check("one entry parsed", entries.length === 1);
  check("id is captured", entries[0]?.id === "ada");
  check("role is captured", entries[0]?.role === "employee");
  check("codeEnv points at the right variable", entries[0]?.codeEnv === "RAG_CODE_ADA");
  check("no problems reported", problems.length === 0);
}

setEnv({
  RAG_STAFF: "ada=employee,peter=executive",
  RAG_CODE_ADA: CODE_A,
  RAG_CODE_PETER: CODE_B,
  RAG_KEY_EMPLOYEE: KEY_EMP,
  RAG_KEY_EXECUTIVE: KEY_EXEC,
});
{
  const { entries, problems } = staffConfig();
  check("two people at different tiers both parse", entries.length === 2 && problems.length === 0);
  check(
    "each person keeps their own tier",
    entries.find((e) => e.id === "ada")?.role === "employee" &&
      entries.find((e) => e.id === "peter")?.role === "executive",
  );
}

// ---------------------------------------------------------------------------
section("staffConfig — fail-closed: unusable people are excluded, not downgraded");
// ---------------------------------------------------------------------------
setEnv({ RAG_STAFF: "ada=employee", RAG_KEY_EMPLOYEE: KEY_EMP });
{
  const { entries, problems } = staffConfig();
  check("no access code set -> not a usable entry", entries.length === 0);
  check("no access code set -> reported as a problem", problems.length === 1);
  check(
    "the problem names the variable to set",
    problems[0]?.reason.includes("RAG_CODE_ADA") === true,
  );
}

setEnv({ RAG_STAFF: "ada=employee", RAG_CODE_ADA: SHORT_CODE, RAG_KEY_EMPLOYEE: KEY_EMP });
{
  const { entries, problems } = staffConfig();
  check("short access code -> not a usable entry", entries.length === 0);
  check(
    "short access code -> problem states the length floor",
    problems[0]?.reason.includes("too short") === true,
  );
}

// THE IMPORTANT ONE. A person configured for a tier whose backend key is absent
// cannot be served at that tier. Refusing them is correct; serving them at whatever
// key happens to exist is the silent downgrade this whole layer exists to prevent.
setEnv({ RAG_STAFF: "peter=executive", RAG_CODE_PETER: CODE_B, RAG_KEY_EMPLOYEE: KEY_EMP });
{
  const { entries, problems } = staffConfig();
  check("role with no backend key -> NOT served (fail closed)", entries.length === 0);
  check(
    "role with no backend key -> problem names the missing key var",
    problems[0]?.reason.includes("RAG_KEY_EXECUTIVE") === true,
  );
  check(
    "and is NOT silently downgraded to a tier that does have a key",
    entries.every((e) => e.role !== "employee"),
  );
}

// A half-broken config must still serve the people who ARE fully configured.
setEnv({
  RAG_STAFF: "ada=employee,peter=executive",
  RAG_CODE_ADA: CODE_A,
  RAG_CODE_PETER: CODE_B,
  RAG_KEY_EMPLOYEE: KEY_EMP,
});
{
  const { entries, problems } = staffConfig();
  check("partial config: the usable person is still served", entries.length === 1 && entries[0]?.id === "ada");
  check("partial config: the unusable person is reported", problems.length === 1 && problems[0]?.id === "peter");
}

// ---------------------------------------------------------------------------
section("staffConfig — parsing edge cases");
// ---------------------------------------------------------------------------
const full = (staff: string, extra: Record<string, string> = {}) =>
  setEnv({
    RAG_STAFF: staff,
    RAG_CODE_ADA: CODE_A,
    RAG_CODE_SAM: CODE_B,
    RAG_KEY_EMPLOYEE: KEY_EMP,
    RAG_KEY_EXECUTIVE: KEY_EXEC,
    ...extra,
  });

full("adaemployee");
check("missing '=' -> problem, no entry", staffConfig().entries.length === 0 && staffConfig().problems.length === 1);

full("=employee");
check("empty id -> problem, no entry", staffConfig().entries.length === 0 && staffConfig().problems.length === 1);

full("ada=");
check("empty role -> problem, no entry", staffConfig().entries.length === 0 && staffConfig().problems.length === 1);

full("ada=employee,ada=executive");
{
  const { entries, problems } = staffConfig();
  // Two entries for one id would make the tier depend on parse order — refuse instead.
  check("duplicate id -> only one entry survives", entries.length === 1);
  check("duplicate id -> reported as a problem", problems.some((p) => p.reason === "duplicate id"));
}

full("ad a=employee");
check("space inside id -> rejected", staffConfig().entries.length === 0);

full("ADA=EMPLOYEE");
{
  const { entries } = staffConfig();
  check("id is lower-cased", entries[0]?.id === "ada");
  check("role is lower-cased", entries[0]?.role === "employee");
}

full("  ada = employee ,  sam = employee  ");
check("surrounding whitespace is tolerated", staffConfig().entries.length === 2);

full("ada=employee,,sam=employee");
{
  const { entries, problems } = staffConfig();
  check("empty list segments are skipped, not flagged", entries.length === 2 && problems.length === 0);
}

// ---------------------------------------------------------------------------
section("resolveStaff — matching a presented code to a person");
// ---------------------------------------------------------------------------
setEnv({
  RAG_STAFF: "ada=employee,peter=executive",
  RAG_CODE_ADA: CODE_A,
  RAG_CODE_PETER: CODE_B,
  RAG_KEY_EMPLOYEE: KEY_EMP,
  RAG_KEY_EXECUTIVE: KEY_EXEC,
});
check("correct code resolves to the right person", resolveStaff(CODE_A)?.id === "ada");
check("each code resolves to its own person", resolveStaff(CODE_B)?.id === "peter");
check("resolved person carries their tier", resolveStaff(CODE_B)?.role === "executive");
check("wrong code -> null", resolveStaff("not-a-real-code-not-a-real-code") === null);
check("empty string -> null", resolveStaff("") === null);
check("null -> null", resolveStaff(null) === null);
check("undefined -> null", resolveStaff(undefined) === null);
check("whitespace-only -> null", resolveStaff("      ") === null);
check("surrounding whitespace on a valid code still matches", resolveStaff(`  ${CODE_A}  `)?.id === "ada");
check("a code that is a PREFIX of a real one does not match", resolveStaff(CODE_A.slice(0, 20)) === null);
check("a code with one flipped character does not match", resolveStaff(`${CODE_A.slice(0, -1)}f`) === null);

{
  // The hash-then-compare fix: timingSafeEqual throws on unequal-length buffers, so
  // comparing raw inputs would crash on any wrong-length code. Hashing both sides to
  // 32 bytes first removes both the crash and the length side-channel.
  let threw = false;
  try {
    resolveStaff("x");
  } catch {
    threw = true;
  }
  check("a length-mismatched code does not throw (hash-then-compare)", !threw);
}

// A person excluded by staffConfig (their tier has no key) must not be resolvable —
// fail closed at the login door, not three requests later.
setEnv({ RAG_STAFF: "peter=executive", RAG_CODE_PETER: CODE_B, RAG_KEY_EMPLOYEE: KEY_EMP });
check("a person whose tier has no backend key cannot sign in at all", resolveStaff(CODE_B) === null);

// An empty code must never match an entry whose code variable is also empty.
setEnv({ RAG_STAFF: "ada=employee", RAG_CODE_ADA: "", RAG_KEY_EMPLOYEE: KEY_EMP });
check("blank code vs blank configured code -> still no match", resolveStaff("") === null);

setEnv({});
check("no staff configured -> nothing resolves", resolveStaff(CODE_A) === null);

// ---------------------------------------------------------------------------
section("backendKeyForRole");
// ---------------------------------------------------------------------------
setEnv({ RAG_KEY_EMPLOYEE: KEY_EMP, RAG_KEY_EXECUTIVE: "   " });
check("configured role returns its key", backendKeyForRole("employee") === KEY_EMP);
check("unset role returns null", backendKeyForRole("partner") === null);
check("whitespace-only key is treated as absent", backendKeyForRole("executive") === null);

// ---------------------------------------------------------------------------
section("staffStatus — safe to expose; must leak no secrets");
// ---------------------------------------------------------------------------
setEnv({ RAG_STAFF: "ada=employee", RAG_CODE_ADA: CODE_A, RAG_KEY_EMPLOYEE: KEY_EMP });
{
  const status = staffStatus();
  const json = JSON.stringify(status);
  check("reports login_enabled", status.login_enabled === true);
  check("lists people with id and role", status.people[0]?.id === "ada" && status.people[0]?.role === "employee");
  // This endpoint's output reaches the browser via /whoami. If a code or a backend key
  // ever appears in it, the proxy has handed away the thing it exists to protect.
  check("NEVER contains an access code", !json.includes(CODE_A));
  check("NEVER contains a backend key", !json.includes(KEY_EMP));
  check("NEVER contains a code env var name", !json.includes("RAG_CODE_ADA"));
}

setEnv({ RAG_STAFF: "peter=executive", RAG_CODE_PETER: CODE_B, RAG_KEY_EMPLOYEE: KEY_EMP });
{
  const status = staffStatus();
  check("surfaces misconfiguration so an operator can see it", status.problems.length === 1);
  check("problem output carries no secret", !JSON.stringify(status).includes(CODE_B));
}

setEnv({});
console.log(`\nRESULT: ${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
