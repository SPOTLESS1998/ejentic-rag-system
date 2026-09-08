/**
 * Sign out — clear the session cookie.
 *
 * POST, not GET, deliberately: a GET logout can be triggered by any page that embeds
 * an image pointing at this URL, so a link in an email could sign someone out. Not a
 * security hole, just an annoying one, and POST plus SameSite=Lax closes it.
 *
 * There is no server-side session store to invalidate — the cookie IS the session. So
 * this ends the session on THIS browser. A cookie already copied elsewhere stays
 * valid until it expires; the way to cut someone off everywhere, immediately, is to
 * clear their RAG_CODE_* variable and restart (see deploy/GO_LIVE.md, "Removing
 * someone's access"), which is stated there rather than implied here.
 */
import { sessionClearCookie } from "@/lib/session";
import { sessionFromRequest } from "@/lib/session";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const { session } = sessionFromRequest(request);
  if (session) console.log(`[auth] ${session.id} signed out`);
  return Response.json(
    { ok: true },
    { status: 200, headers: { "Set-Cookie": sessionClearCookie() } },
  );
}
