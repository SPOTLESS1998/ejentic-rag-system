/**
 * Example: wiring the @ejentic/rag-sdk into any Ejentic service.
 *
 * Shows the three integration styles:
 *   1. Default client via EJENTIC_RAG_URL
 *   2. Explicit per-service client
 *   3. Streaming (server-sent events) consumption
 *
 * This is illustrative code — copy what you need into your own service.
 */
import { createClient, getClient, rag } from "@ejentic/rag-sdk";

async function main() {
  // 1. Default client (honours EJENTIC_RAG_URL env var).
  const defaultRag = rag();
  const health = await defaultRag.health();
  console.log("active client:", health.client, "| index:", health.index);

  // 2. Per-service client (e.g. the lead-gen pipeline talking to one tenant).
  const leadgen = createClient(process.env.EJENTIC_RAG_URL ?? "http://localhost:8002", {
    client: "ejentic",
    platform: "leadgen-pipeline",
  });

  const answer = await leadgen.query("What services does Ejentic AI offer?", {
    clearanceLevel: "guest",
  });
  console.log("answer:", answer.response.slice(0, 120));
  console.log("tokens:", answer.tokenUsage);

  // 3. Streaming for interactive UIs (e.g. a Next.js chat page).
  const streamed = leadgen.streamText("What is Project Delta?", {
    clearanceLevel: "employee",
  });

  // 4. Token dashboard — now a measurable architecture.
  const metrics = await leadgen.metrics();
  console.log("lifetime queries:", metrics.totals.queries, "| tokens:", metrics.totals.total_tokens);

  // 5. Deployment registry (resolve a tenant's base URL).
  const tenantRag = getClient("ejentic", "my-service");
  console.log("tenant client:", (await tenantRag.health()).client_name);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});