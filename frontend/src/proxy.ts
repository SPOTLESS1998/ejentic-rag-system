/**
 * Optimistic sign-in guard for PAGE requests.
 *
 * ⚠️ This is a redirect for the sake of the person's experience — it is NOT the
 * security boundary, and must never be treated as one. Next's own documentation is
 * blunt about it: proxy "should not be used as a full session management or
 * authorization solution", and a matcher change or a moved route can silently remove
 * its coverage. So the real check lives in every route handler (`resolveCaller` in
 * src/lib/rag-proxy.ts), which refuses with a 401 regardless of whether this file
 * ran. Deleting this file would make the UI uglier, not insecure.
 *
 * What it buys: someone who is not signed in lands on the sign-in screen instead of
 * the chat shell, which then fails every request. That is the whole job.
 *
 * NOTE ON THE FILENAME — in Next 16 `middleware.ts` was renamed to `proxy.ts`. A
 * file still called `middleware.ts` is simply not picked up: no error, no warning, no
 * guard. Worth knowing before "fixing" this file's name back.
 *
 * API routes are deliberately EXCLUDED from the matcher. A `fetch()` that gets a 302
 * to an HTML login page follows it and then fails to parse HTML as JSON, which
 * surfaces to the user as a mysterious syntax error instead of "please sign in". API
 * routes answer 401 JSON and the client decides what to do with it.
 */
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { loginEnabled } from "@/lib/staff";
import { sessionFromRequest } from "@/lib/session";

export function proxy(request: NextRequest) {
  // The public deployment configures no staff, so there is nothing to sign in to and
  // nothing to guard. Same hinge as everywhere else: RAG_STAFF selects the mode.
  if (!loginEnabled()) return NextResponse.next();

  const { session } = sessionFromRequest(request);
  if (session) return NextResponse.next();

  const url = new URL("/login", request.url);
  // Remember where they were heading so sign-in returns them there. Only the path
  // and query travel — never a full URL, which is how open-redirect bugs start. The
  // login page validates this again before using it (defence on both sides).
  const intended = request.nextUrl.pathname + request.nextUrl.search;
  if (intended && intended !== "/") url.searchParams.set("next", intended);
  return NextResponse.redirect(url);
}

export const config = {
  // Everything except API routes, Next's own assets, and the sign-in page itself —
  // guarding /login would be an infinite redirect.
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico|login).*)"],
};
