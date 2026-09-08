/**
 * Upload proxy: browser -> here -> backend `POST /upload`.
 *
 * Forwards the multipart body untouched (so the backend, not us, decides the
 * stored filename and enforces the size cap) and returns the `upload_token` the
 * browser must present to query its own document.
 */
import { backendHeaders, callerError, passThroughError, resolveCaller, RAG_BASE_URL } from "@/lib/rag-proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  // Resolve identity BEFORE reading the body: an unauthenticated caller should be
  // refused without this process first buffering a file it will discard.
  const resolved = resolveCaller(request);
  if (!resolved.ok) return callerError(resolved);

  const form = await request.formData();
  const file = form.get("file");
  if (!(file instanceof File)) {
    return Response.json({ error: "No file provided." }, { status: 400 });
  }

  // Rebuild the form rather than streaming the raw body: this drops any extra
  // fields a caller tacked on, so only the file reaches the backend.
  const out = new FormData();
  out.append("file", file, file.name);

  // No Content-Type set here on purpose — fetch derives it with the multipart
  // boundary, and a hand-written value omits the boundary the parser needs.
  const res = await fetch(`${RAG_BASE_URL}/upload`, {
    method: "POST",
    headers: backendHeaders(resolved.caller),
    body: out,
    signal: request.signal,
  });

  if (!res.ok) return passThroughError(res);
  return Response.json(await res.json());
}
