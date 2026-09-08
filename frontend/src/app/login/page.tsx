"use client";

import { useState, useEffect } from "react";

/**
 * Sign-in screen for the internal deployment.
 *
 * The person types their personal access code; it goes to /api/auth/login, is checked
 * on the server, and comes back as a signed session cookie. The code itself is never
 * stored in the browser — not in localStorage, not in a cookie, not in component
 * state after submit. Nothing here remembers it, deliberately: a code kept in
 * localStorage is a long-lived credential sitting in a place any XSS can read.
 */
export default function LoginPage() {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [next, setNext] = useState("/");

  // Where to go after signing in. Validated here as well as in proxy.ts: only a
  // same-origin path is allowed. Without this check, `?next=https://evil.example`
  // would turn our own sign-in page into a redirector that borrows our domain's
  // credibility to send staff somewhere else.
  useEffect(() => {
    const raw = new URLSearchParams(window.location.search).get("next") ?? "/";
    const safe = raw.startsWith("/") && !raw.startsWith("//") && !raw.includes("\\");
    setNext(safe ? raw : "/");
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const entered = code.trim();
    if (!entered || busy) return;

    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: entered }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(data?.error ?? `Sign-in failed (${res.status}).`);
        setCode("");
        return;
      }
      // Full navigation rather than a client-side route change: the session cookie
      // was just set, and a hard load guarantees every server component and route
      // handler sees it on the next request.
      window.location.href = next;
    } catch {
      setError("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-neutral-900 p-6 font-sans text-neutral-100">
      <div className="w-full max-w-md">
        <div className="mb-8 flex items-center space-x-4">
          <div className="flex h-10 w-10 items-center justify-center rounded-md bg-white text-xl font-bold text-black shadow-md">
            E
          </div>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-white">Ejentic AI</h1>
            <p className="text-sm text-neutral-400">Internal Knowledge Core</p>
          </div>
        </div>

        <form
          onSubmit={submit}
          className="rounded-xl border border-neutral-800 bg-neutral-950 p-8 shadow-2xl"
        >
          <label htmlFor="code" className="block text-sm font-medium text-neutral-200">
            Access code
          </label>
          <p className="mt-1 mb-4 text-sm text-neutral-500">
            Your personal code. It decides which material you can see.
          </p>

          <input
            id="code"
            type="password"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            autoComplete="one-time-code"
            autoFocus
            spellCheck={false}
            placeholder="••••••••••••••••"
            className="w-full rounded-lg border border-neutral-800 bg-neutral-900 px-4 py-3 font-mono text-sm text-white outline-none transition-all placeholder-neutral-700 focus:ring-1 focus:ring-neutral-600"
            disabled={busy}
          />

          {error && (
            <p
              className="mt-4 rounded-md border border-red-900 bg-red-950/50 px-3 py-2 text-sm text-red-300"
              role="alert"
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy || !code.trim()}
            className="mt-6 w-full rounded-lg bg-white py-3 font-semibold text-black transition-all hover:bg-neutral-200 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? "Checking…" : "Sign in"}
          </button>

          <p className="mt-6 border-t border-neutral-800 pt-4 text-xs leading-relaxed text-neutral-500">
            Lost your code? It cannot be recovered — only replaced. Ask whoever
            administers this deployment to issue a new one.
          </p>
        </form>
      </div>
    </main>
  );
}
