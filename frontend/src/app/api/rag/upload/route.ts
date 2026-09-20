/**
 * Upload proxy: browser -> here -> backend `POST /upload`.
 *
 * Forwards the multipart body untouched (so the backend, not us, decides the
 * stored filename and enforces the size cap) and returns the `upload_token` the
 * browser must present to query its own document.
 */
import {
  backendHeaders,
  callerError,
  passThroughError,
  resolveCaller,
  resolveVisitor,
  RAG_BASE_URL,
} from "@/lib/rag-proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  // Resolve identity BEFORE reading the body: an unauthenticated caller should be
  // refused without this process first buffering a file it will discard.
  const resolved = resolveCaller(request);
  if (!resolved.ok) return callerError(resolved);

  // Which rate-limit bucket to meter against. Resolved from the same request, before
  // the body is touched, so an upload is charged to the same visitor its chats are.
  const visitor = resolveVisitor(request, resolved.caller);

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
    headers: backendHeaders(resolved.caller, {}, null, visitor.id),
    body: out,
    signal: request.signal,
  });

  // Attach on both paths, for the same reason as /chat: a refused upload must not
  // hand the visitor a fresh bucket to retry in.
  const response = res.ok
    ? Response.json(await res.json())
    : await passThroughError(res);
  if (visitor.setCookie) response.headers.append("Set-Cookie", visitor.setCookie);
  return response;
}
