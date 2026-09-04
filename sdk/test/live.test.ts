/** Live integration test: SDK against the real backend at localhost:8002. */
import { createClient, getClient } from "@ejentic/rag-sdk";

async function main() {
  const rag = createClient("http://localhost:8002", { platform: "live-test" });

  const health = await rag.health();
  console.log("✅ health:", health.client, "|", health.index, "|", health.reranker);

  console.log("⏳ querying...");
  const answer = await rag.query("What services does Ejentic AI offer?", {
    clearanceLevel: "guest",
  });
  console.log("✅ answer:", answer.response.slice(0, 120) + "...");
  console.log("✅ tokens:", JSON.stringify(answer.tokenUsage));

  const metrics = await rag.metrics();
  console.log("✅ metrics: queries=", metrics.totals.queries, "tokens=", metrics.totals.total_tokens);

  const tenantRag = getClient("ejentic", "live-test");
  const tenantHealth = await tenantRag.health();
  console.log("✅ tenant:", tenantHealth.client_name);

  try {
    await rag.query("test", { clearanceLevel: "guest", client: "acme" });
    console.log("❌ cross-tenant guard: should have thrown");
  } catch (e: any) {
    console.log("✅ cross-tenant guard:", e.message);
  }
}

main().catch((err) => {
  console.error("FAILED:", err);
  process.exitCode = 1;
});
