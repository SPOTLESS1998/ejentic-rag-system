/**
 * Tells the browser what OUR key grants: browser -> here -> backend `GET /whoami`.
 *
 * This is what replaced the clearance dropdown. The old UI let the browser pick a
 * clearance, which implied the browser decides — it did not, but it also was not
 * stopped. Now the UI displays the answer to "what am I?" and the server is the
 * only thing that can answer.
 */
import { backendHeaders, hasApiKey, passThroughError, RAG_BASE_URL } from "@/lib/rag-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const res = await fetch(`${RAG_BASE_URL}/whoami`, {
    method: "GET",
    headers: backendHeaders(),
    signal: request.signal,
    cache: "no-store",
  });

  if (!res.ok) return passThroughError(res);

  const data = (await res.json()) as Record<string, unknown>;
  // `key_configured` lets the UI distinguish "no key set in this deployment" from
  // "key rejected" — otherwise both look like a generic failure and the operator
  // has nothing to act on.
  return Response.json({ ...data, key_configured: hasApiKey() });
}
