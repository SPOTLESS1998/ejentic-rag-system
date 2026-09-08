"use client";

import { useState, useRef, useEffect, useCallback } from "react";

/**
 * The browser NEVER talks to the RAG backend directly and NEVER holds an API key.
 *
 * Every call below goes to our own /api/rag/* route handlers, which run on the
 * server and add the key there (see src/lib/rag-proxy.ts). This is why the
 * clearance dropdown is gone: clearance now comes from the key the server holds,
 * so the UI's job is to DISPLAY what that key grants, not to let anyone pick it.
 */

type Message = { role: string; content: string };

type WhoAmI = {
  role: string;
  client: string;
  clearance_tags: string[] | "*";
  can_narrow_to: string[];
  auth_required: boolean;
  is_admin: boolean;
  key_configured: boolean;
  /** Per-person deployment only: who is signed in, and whether sign-in applies here.
   *  On the public deployment `login_required` is false and `actor` is null. */
  signed_in?: boolean;
  actor?: string | null;
  login_required?: boolean;
};

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: "assistant",
      content:
        "Good day. I am the Ejentic AI Knowledge Core. Ask me anything within your clearance.",
    },
  ]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadStatus, setUploadStatus] = useState<string | null>(null);
  const [uploadToken, setUploadToken] = useState<string | null>(null);
  const [uploadName, setUploadName] = useState<string | null>(null);
  const [who, setWho] = useState<WhoAmI | null>(null);
  const [whoError, setWhoError] = useState<string | null>(null);
  /** Optional NARROWING: an executive may preview the guest view. The server
   *  rejects any attempt to widen, so this control can only ever restrict. */
  const [viewAs, setViewAs] = useState<string>("");

  const fileInputRef = useRef<HTMLInputElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Ask the server what our key is worth. The answer is authoritative; nothing
  // here can change it.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/rag/whoami", { cache: "no-store" });
        const data = await res.json();
        if (cancelled) return;
        if (!res.ok) {
          setWhoError(data?.error ?? `Could not verify clearance (${res.status}).`);
          return;
        }
        // On the internal deployment a session can expire while this tab sits open.
        // Sending them to sign in beats leaving a chat box that 401s every question.
        if (data?.login_required && data?.signed_in === false) {
          window.location.href = "/login";
          return;
        }
        setWho(data as WhoAmI);
      } catch {
        if (!cancelled) setWhoError("Backend unreachable.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const signOut = useCallback(async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      window.location.href = "/login";
    }
  }, []);

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setIsUploading(true);
    setUploadStatus("Uploading document...");

    const formData = new FormData();
    formData.append("file", file);

    try {
      const res = await fetch("/api/rag/upload", { method: "POST", body: formData });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error ?? "Upload failed");

      // The token is what makes this document ours. Without it the backend does
      // not fold the file into a query, so no other user can reach it.
      setUploadToken(data.upload_token ?? null);
      setUploadName(file.name);
      setUploadStatus("Document indexed.");
      setTimeout(() => setUploadStatus(null), 5000);

      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: `I have indexed "${file.name}" for this session only. Ask me about its contents.`,
        },
      ]);
    } catch (error) {
      setUploadStatus(error instanceof Error ? error.message : "Failed to index document.");
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const clearUpload = useCallback(() => {
    setUploadToken(null);
    setUploadName(null);
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim()) return;

    const userMessage = input.trim();
    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: userMessage }]);
    setIsLoading(true);

    try {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (uploadToken) headers["X-Upload-Token"] = uploadToken;

      const res = await fetch("/api/rag/chat", {
        method: "POST",
        headers,
        // clearance_level is only ever a NARROWING request; "" means "everything
        // my key grants". The server 403s anything wider.
        body: JSON.stringify({ query: userMessage, clearance_level: viewAs }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err?.error ?? `Request failed (${res.status})`);
      }

      setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

      const reader = res.body?.getReader();
      if (!reader) return;
      const decoder = new TextDecoder();

      let assistantMessage = "";
      let buffer = "";
      let done = false;

      const paint = (text: string) =>
        setMessages((prev) => {
          const next = [...prev];
          next[next.length - 1] = { role: "assistant", content: text };
          return next;
        });

      while (!done) {
        const { done: streamDone, value } = await reader.read();
        if (streamDone) break;

        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop() ?? "";

        for (const event of events) {
          const line = event.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          const data = line.slice(6).trim();
          if (data === "[DONE]") {
            done = true;
            break;
          }
          try {
            const parsed = JSON.parse(data);
            if (parsed.chunk) {
              assistantMessage += parsed.chunk;
              paint(assistantMessage);
            } else if (parsed.error) {
              assistantMessage += `\n\n[Error: ${parsed.error}]`;
              paint(assistantMessage);
            }
          } catch {
            /* skip malformed event */
          }
        }
      }
    } catch (error) {
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content:
            error instanceof Error
              ? `System error: ${error.message}`
              : "System error: unable to reach the Knowledge Core.",
        },
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  const tagLabel =
    who?.clearance_tags === "*"
      ? "all tiers"
      : Array.isArray(who?.clearance_tags)
        ? who!.clearance_tags.join(" · ")
        : "";

  return (
    <main className="flex min-h-screen flex-col items-center justify-between p-4 md:p-12 bg-neutral-900 text-neutral-100 font-sans selection:bg-neutral-700">
      {/* Header */}
      <div className="w-full max-w-5xl flex items-center justify-between mb-8 pb-6 border-b border-neutral-800">
        <div className="flex items-center space-x-4">
          <div className="flex items-center justify-center w-10 h-10 bg-white text-black font-bold text-xl rounded-md shadow-md">
            E
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-white tracking-tight">Ejentic AI</h1>
            <p className="text-sm text-neutral-400">Enterprise Hybrid RAG System</p>
          </div>
        </div>

        <div className="flex items-center space-x-3">
          {uploadStatus && (
            <span className="text-sm text-emerald-400">{uploadStatus}</span>
          )}

          {/* Clearance: DISPLAY, not a choice. Replaces the old dropdown that
              let the browser name its own role. */}
          {whoError ? (
            <span
              className="text-sm px-3 py-2 rounded-md border border-red-900 bg-red-950/50 text-red-300"
              title={whoError}
            >
              ⚠ clearance unverified
            </span>
          ) : who ? (
            <div className="flex items-center gap-2">
              {/* WHO, on the internal deployment. Distinct from clearance on
                  purpose: the person comes from the session cookie this server
                  signed, the tier comes from the backend. Two questions, two
                  authorities — showing them separately keeps that honest. */}
              {who.actor && (
                <span
                  className="text-sm px-3 py-2 rounded-md border border-neutral-700 bg-neutral-800 text-neutral-200"
                  title="Signed in on this browser. Your access code decided your tier."
                >
                  <span className="text-neutral-500">signed in</span>{" "}
                  <span className="font-medium text-white">{who.actor}</span>
                </span>
              )}

              <span
                className="text-sm px-3 py-2 rounded-md border border-neutral-700 bg-neutral-800 text-neutral-200"
                title={`Granted by this deployment's API key. Tags: ${tagLabel}`}
              >
                <span className="text-neutral-500">clearance</span>{" "}
                <span className="font-medium text-white">{who.role}</span>
              </span>

              {!who.auth_required && (
                <span
                  className="text-sm px-3 py-2 rounded-md border border-amber-800 bg-amber-950/40 text-amber-300"
                  title="This backend accepts unauthenticated callers. Local dev only — set auth.required=true before exposing it."
                >
                  ⚠ auth off
                </span>
              )}

              {/* Narrowing only: options come from the server, computed from the
                  key. Widening is impossible to express here and 403 anyway. */}
              {who.can_narrow_to.length > 1 && (
                <select
                  value={viewAs}
                  onChange={(e) => setViewAs(e.target.value)}
                  title="Preview a narrower clearance. You cannot request more than your key grants."
                  className="bg-neutral-800 text-neutral-200 text-sm py-2 px-3 rounded-md border border-neutral-700 outline-none hover:bg-neutral-700 transition-colors cursor-pointer"
                >
                  <option value="">View as: full access</option>
                  {who.can_narrow_to
                    .filter((r) => r !== who.role)
                    .map((r) => (
                      <option key={r} value={r}>
                        View as: {r}
                      </option>
                    ))}
                </select>
              )}
            </div>
          ) : (
            <span className="text-sm text-neutral-500 px-3 py-2">checking clearance…</span>
          )}

          <input
            type="file"
            accept=".pdf,.txt,.md"
            className="hidden"
            ref={fileInputRef}
            onChange={handleFileUpload}
          />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={isUploading}
            className="flex items-center space-x-2 bg-neutral-800 hover:bg-neutral-700 text-neutral-200 text-sm font-medium py-2 px-4 rounded-md border border-neutral-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isUploading ? (
              <span className="animate-spin text-lg leading-none">↻</span>
            ) : (
              <span>↑</span>
            )}
            <span>Upload Document</span>
          </button>

          {/* Only on the internal deployment — the public one has nobody to sign
              out. Ends the session on THIS browser; a code is revoked by the
              operator, not from here (see deploy/GO_LIVE.md). */}
          {who?.actor && (
            <button
              onClick={signOut}
              title={`Signed in as ${who.actor}. Sign out of this browser.`}
              className="text-neutral-400 hover:text-white text-sm font-medium py-2 px-4 rounded-md border border-neutral-800 hover:border-neutral-700 transition-colors"
            >
              Sign out
            </button>
          )}
        </div>
      </div>

      {/* Chat Container */}
      <div className="flex flex-col w-full max-w-5xl h-[75vh] bg-neutral-950 border border-neutral-800 rounded-xl shadow-2xl overflow-hidden relative">
        {uploadName && (
          <div className="flex items-center justify-between px-6 py-2 bg-neutral-900 border-b border-neutral-800 text-sm text-neutral-400">
            <span>
              Searching your document{" "}
              <span className="text-neutral-200 font-medium">{uploadName}</span>{" "}
              alongside the knowledge base
            </span>
            <button
              onClick={clearUpload}
              className="text-neutral-500 hover:text-neutral-200 transition-colors"
              title="Stop including this document"
            >
              remove ✕
            </button>
          </div>
        )}

        <div className="flex-1 overflow-y-auto p-6 md:p-8 space-y-6 scroll-smooth">
          {messages.map((m, index) => (
            <div
              key={index}
              className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}
            >
              <div
                className={`max-w-[85%] rounded-lg p-5 leading-relaxed shadow-sm ${
                  m.role === "user"
                    ? "bg-neutral-800 text-white border border-neutral-700"
                    : "bg-neutral-900 text-neutral-200 border border-neutral-800"
                }`}
              >
                <p className="whitespace-pre-wrap">{m.content}</p>
              </div>
            </div>
          ))}
          {isLoading && (
            <div className="flex justify-start">
              <div className="max-w-[80%] rounded-lg p-5 bg-neutral-900 border border-neutral-800 text-neutral-400 flex items-center space-x-3">
                <span className="animate-spin text-lg leading-none">↻</span>
                <span className="text-sm font-medium">Processing query...</span>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        <div className="p-4 bg-neutral-900 border-t border-neutral-800">
          <form onSubmit={handleSubmit} className="flex space-x-3">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Query the Ejentic Knowledge Base or an uploaded document..."
              className="flex-1 bg-neutral-950 border border-neutral-800 text-white rounded-lg px-5 py-4 focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600 text-sm transition-all"
              disabled={isLoading}
            />
            <button
              type="submit"
              disabled={isLoading || !input.trim()}
              className="flex items-center justify-center bg-white hover:bg-neutral-200 text-black font-semibold py-4 px-8 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <span className="mr-2">➤</span>
              Send
            </button>
          </form>
        </div>
      </div>
    </main>
  );
}
