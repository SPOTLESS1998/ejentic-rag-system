/**
 * Live integration script: SDK against the real backend at localhost:8002.
 *
 * NOT a vitest test — vitest excludes this file (see vitest.config.ts) because it
 * needs a running server and real keys. Run it by hand:
 *
 *     RAG_KEY_GUEST=... RAG_KEY_EXECUTIVE=... npx tsx test/live.test.ts
 *
 * It imports from ../src directly, NOT from "@ejentic/rag-sdk". The package name
 * resolves through package.json "exports" to ./dist, which does not exist until
 * `npm run build` — so importing it by name made `npm run typecheck` fail on a
 * clean checkout, before any code was even wrong.
 */
import { createClient, getClient } from "../src/index.js";

const GUEST_KEY = process.env.RAG_KEY_GUEST ?? "";
const EXEC_KEY = process.env.RAG_KEY_EXECUTIVE ?? "";

let failures = 0;

function check(name: string, cond: boolean, extra = ""): void {
  if (cond) {
    console.log(`✅ ${name}${extra ? " — " + extra : ""}`);
  } else {
    failures += 1;
    console.log(`❌ ${name}${extra ? " — " + extra : ""}`);
  }
}

/** Pull the HTTP status out of the SDK's thrown error message. */
function statusOf(err: unknown): number | null {
  const m = /\((\d{3})\)/.exec(String((err as Error)?.message ?? ""));
  return m ? Number(m[1]) : null;
}

async function main() {
  // GET / is public by design (load balancers need it) — no key here.
  const anon = createClient("http://localhost:8002", { platform: "live-test" });
  const health = await anon.health();
  console.log(
    `   backend: ${health.client} | ${health.index} | reranker=${health.reranker}`,
  );
  check("health reachable without a key (public liveness)", health.status === "ok");

  const authOn = health.auth?.required === true;
  console.log(`   auth.required = ${authOn}`);
  if (!authOn) {
    console.log(
      "\n⚠️  auth.required is FALSE — this instance accepts unauthenticated\n" +
        "   callers, so the 401/403 checks below cannot prove anything. Set\n" +
        '   "auth": {"required": true} in backend/clients/ejentic.json and add the\n' +
        "   key env vars before treating this run as a security result.\n",
    );
  }

  // --- The boundary: no key must be refused -------------------------------
  if (authOn) {
    for (const [label, call] of [
      ["/api/rag", () => anon.query("What services does Ejentic AI offer?")],
      ["/metrics", () => anon.metrics()],
      ["/clients", () => anon.clients()],
    ] as const) {
      try {
        await call();
        check(`${label} rejects a keyless caller`, false, "it ANSWERED");
      } catch (e) {
        check(`${label} rejects a keyless caller`, statusOf(e) === 401,
          `status ${statusOf(e)}`);
      }
    }

    try {
      await createClient("http://localhost:8002", { apiKey: "not-a-real-key" }).query("hi");
      check("a bogus key is rejected", false, "it ANSWERED");
    } catch (e) {
      check("a bogus key is rejected", statusOf(e) === 401, `status ${statusOf(e)}`);
    }
  }

  if (!GUEST_KEY || !EXEC_KEY) {
    console.log(
      "\n⚠️  RAG_KEY_GUEST / RAG_KEY_EXECUTIVE not in the environment — skipping\n" +
        "   the clearance-isolation checks, which are the point of this script.\n",
    );
    return finish();
  }

  const guest = createClient("http://localhost:8002", {
    apiKey: GUEST_KEY,
    platform: "live-test",
  });
  const exec = createClient("http://localhost:8002", {
    apiKey: EXEC_KEY,
    platform: "live-test",
  });

  // --- Narrowing rule: down is fine, up is 403 ----------------------------
  try {
    await guest.query("test", { clearanceLevel: "executive" });
    check("guest key asking for executive is refused", false, "it ANSWERED");
  } catch (e) {
    check("guest key asking for executive is refused", statusOf(e) === 403,
      `status ${statusOf(e)}`);
  }

  const narrowed = await exec.query("What services does Ejentic AI offer?", {
    clearanceLevel: "guest",
  });
  check("executive key may narrow to guest", narrowed.status === "success");

  // --- Isolation: the same question, two clearances -----------------------
  // This is the whole product claim. A guest must NOT be able to reach the
  // executive-tier financials, and an executive MUST — otherwise a "pass" here
  // could just mean retrieval is broken for everyone.
  const SECRET_Q = "What was Q2 revenue?";
  const guestAns = (await guest.query(SECRET_Q)).response;
  const execAns = (await exec.query(SECRET_Q)).response;

  const leaked = /2\.4\s*M|\$2,?400,?000/i.test(guestAns);
  check("guest CANNOT see executive revenue", !leaked,
    leaked ? `LEAKED: ${guestAns.slice(0, 100)}` : "refused/no figure");
  check("executive CAN see it (retrieval still works)",
    /2\.4\s*M|\$2,?400,?000/i.test(execAns), execAns.slice(0, 80));

  // --- Token accounting still rides the X-* headers -----------------------
  const usage = (await exec.query("What are core hours?")).tokenUsage;
  check("token headers present", usage.totalTokens !== null || usage.gated,
    `source=${usage.tokenSource} total=${usage.totalTokens} gated=${usage.gated}`);

  // --- Streaming contract intact ------------------------------------------
  const streamed = await exec.streamText("Summarize what Ejentic AI does.");
  check("SSE stream returns text", streamed.trim().length > 0,
    `${streamed.length} chars`);
  check("stream is not duplicated",
    !isDoubled(streamed), `${streamed.slice(0, 60)}...`);

  // --- Multi-tenancy guard still 409 --------------------------------------
  const tenantRag = getClient("ejentic", "live-test");
  check("registry client resolves", (await tenantRag.health()).client === "ejentic");
  try {
    await exec.query("test", { client: "acme" });
    check("cross-tenant request is refused", false, "it ANSWERED");
  } catch (e) {
    check("cross-tenant request is refused", statusOf(e) === 409,
      `status ${statusOf(e)}`);
  }

  finish();
}

/** Crude repeat detector for the streaming duplication bug: a retry that
 *  replayed the stream produces "Hello worldHello world". */
function isDoubled(s: string): boolean {
  const t = s.trim();
  if (t.length < 40 || t.length % 2 !== 0) return false;
  const half = t.length / 2;
  return t.slice(0, half) === t.slice(half);
}

function finish(): void {
  console.log(`\nRESULT: ${failures === 0 ? "all checks passed" : `${failures} FAILED`}`);
  if (failures > 0) process.exitCode = 1;
}

main().catch((err) => {
  console.error("FAILED:", err);
  process.exitCode = 1;
});
