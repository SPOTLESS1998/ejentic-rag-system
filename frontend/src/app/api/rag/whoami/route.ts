/**
 * Tells the browser what it is allowed to see: browser -> here -> backend `GET /whoami`.
 *
 * This is what replaced the clearance dropdown. The old UI let the browser pick a
 * clearance, which implied the browser decides — it did not, but it also was not
 * stopped. Now the UI displays the answer to "what am I?" and the server is the
 * only thing that can answer.
 *
 * On the internal deployment it also answers "who am I?", which is a different
 * question: the tier comes from the backend (authoritative, derived from the key),
 * while the person comes from the session cookie this server signed.
 */
import {
  backendHeaders,
  hasApiKey,
  passThroughError,
  resolveCaller,
  RAG_BASE_URL,
} from "@/lib/rag-proxy";
import { loginEnabled, staffStatus } from "@/lib/staff";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const resolved = resolveCaller(request);

  // Not signed in on a per-person deployment. Answer 200 with `signed_in: false`
  // rather than 401: this endpoint is what the UI calls to find out whether it needs
  // to show the sign-in screen, so "you are nobody" is a valid answer to the
  // question, not an error. The RAG routes are the ones that must refuse.
  if (!resolved.ok) {
    return Response.json(
      {
        signed_in: false,
        login_required: loginEnabled(),
        key_configured: hasApiKey(),
        ...(resolved.status === 503 ? { error: resolved.detail } : {}),
      },
      { status: 200 },
    );
  }

  const res = await fetch(`${RAG_BASE_URL}/whoami`, {
    method: "GET",
    headers: backendHeaders(resolved.caller),
    signal: request.signal,
    cache: "no-store",
  });

  if (!res.ok) return passThroughError(res);

  const data = (await res.json()) as Record<string, unknown>;
  // `key_configured` lets the UI distinguish "no key set in this deployment" from
  // "key rejected" — otherwise both look like a generic failure and the operator
  // has nothing to act on.
  //
  // `problems` surfaces staff who are configured but cannot be served (e.g. a role
  // with no backend key). A half-configured deployment should be visible here rather
  // than discovered by the one person who cannot sign in.
  return Response.json({
    ...data,
    signed_in: resolved.caller.actor !== null,
    actor: resolved.caller.actor,
    login_required: loginEnabled(),
    key_configured: hasApiKey(),
    ...(loginEnabled() ? { staff_problems: staffStatus().problems } : {}),
  });
}
