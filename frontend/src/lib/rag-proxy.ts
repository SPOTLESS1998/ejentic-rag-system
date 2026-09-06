/**
 * Server-only helpers for talking to the RAG backend.
 *
 * WHY A PROXY EXISTS AT ALL
 * -------------------------
 * The backend derives a caller's clearance from an API key. A key shipped to the
 * browser is a PUBLIC key — anyone can open devtools, read it, and hold whatever
 * clearance it grants. So the browser never sees a key: it calls our own
 * `/api/rag/*` routes, and this module (which only ever runs on the server) adds
 * the key on the way out.
 *
 * `RAG_API_KEY` must NOT be prefixed `NEXT_PUBLIC_` — that prefix is exactly the
 * mechanism that inlines a value into the client bundle.
 */

/** Base URL of the FastAPI backend. Server-side, so no NEXT_PUBLIC_ needed. */
export const RAG_BASE_URL = (
  process.env.RAG_BACKEND_URL ??
  process.env.NEXT_PUBLIC_BACKEND_URL ??
  "http://localhost:8002"
).replace(/\/+$/, "");

/** The key this deployment authenticates as. Read at request time, never bundled. */
function apiKey(): string {
  return (process.env.RAG_API_KEY ?? "").trim();
}

/**
 * Headers for an outbound backend call: our key, plus a caller-supplied upload
 * token when one is present.
 *
 * Note what is NOT forwarded: any client-sent `X-API-Key`. If we passed the
 * browser's headers through, a caller could supply their own key (or a guessed
 * one) and the proxy would become the open door it exists to close.
 */
export function backendHeaders(
  extra: Record<string, string> = {},
  uploadToken?: string | null,
): Record<string, string> {
  const out: Record<string, string> = { ...extra };
  const key = apiKey();
  if (key) out["X-API-Key"] = key;
  if (uploadToken) out["X-Upload-Token"] = uploadToken;
  return out;
}

/** True when this deployment has a key configured. */
export function hasApiKey(): boolean {
  return apiKey().length > 0;
}

/**
 * Forward a JSON error from the backend as-is.
 *
 * Deliberately passes the backend's status through: a 401/403/409 must stay
 * itself so the UI can say what actually happened. Collapsing everything to 500
 * turns "your key can't do that" into "the server is broken".
 */
export async function passThroughError(res: Response): Promise<Response> {
  let detail = res.statusText;
  try {
    const body = (await res.json()) as { detail?: string };
    if (body?.detail) detail = body.detail;
  } catch {
    /* non-JSON body */
  }
  return Response.json({ error: detail }, { status: res.status });
}
