/**
 * Streaming chat proxy: browser -> here -> backend `POST /chat`.
 *
 * The browser holds no API key. This route adds it, and pipes the backend's SSE
 * body straight back unchanged so the `data: {"chunk"}` / `[DONE]` contract the
 * UI already parses is untouched.
 */
import { backendHeaders, passThroughError, RAG_BASE_URL } from "@/lib/rag-proxy";

/** Never prerender or cache: every call is a live stream. */
export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  let body: { query?: string; clearance_level?: string; platform?: string };
  try {
    body = await request.json();
  } catch {
    return Response.json({ error: "Body must be JSON." }, { status: 400 });
  }

  const query = (body.query ?? "").trim();
  if (!query) {
    return Response.json({ error: "query is required." }, { status: 400 });
  }

  // clearance_level is forwarded but can only ever NARROW: the backend derives
  // the real role from our key and answers 403 if this asks for more. We do not
  // validate it here — one authority for that decision, and it is the backend.
  const upstream = await fetch(`${RAG_BASE_URL}/chat`, {
    method: "POST",
    headers: backendHeaders(
      { "Content-Type": "application/json", Accept: "text/event-stream" },
      request.headers.get("X-Upload-Token"),
    ),
    body: JSON.stringify({
      query,
      clearance_level: (body.clearance_level ?? "").toLowerCase(),
      platform: "WEB_UI",
    }),
    // Let the browser's disconnect cancel the upstream stream instead of leaving
    // the backend generating tokens for an answer nobody will read.
    signal: request.signal,
  });

  if (!upstream.ok || !upstream.body) {
    return passThroughError(upstream);
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      // Without this an nginx/proxy in front will buffer the whole stream and the
      // answer arrives all at once, which reads as a hang.
      "X-Accel-Buffering": "no",
    },
  });
}
