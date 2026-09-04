import {
  ChatStreamEvent,
  ClientSummary,
  HealthResponse,
  MetricsResponse,
  RAGClientOptions,
  RAGQueryOptions,
  RAGResponse,
  RAGUploadResponse,
  TokenUsage,
} from "./types";

const DONE_SENTINEL = "[DONE]";

function stripTrailingSlashes(url: string): string {
  return url.replace(/\/+$/, "");
}

function headerInt(res: Response, name: string): number | null {
  const raw = res.headers.get(name);
  if (raw === null || raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

function readTokenUsage(res: Response): TokenUsage {
  return {
    promptTokens: headerInt(res, "X-Prompt-Tokens"),
    completionTokens: headerInt(res, "X-Completion-Tokens"),
    totalTokens: headerInt(res, "X-Total-Tokens"),
    tokenSource: res.headers.get("X-Token-Source"),
    gated: (res.headers.get("X-Gated") ?? "false") === "true",
    savedTokens: headerInt(res, "X-Saved-Tokens"),
  };
}

/**
 * Typed client for the Ejentic Enterprise RAG API.
 *
 * One uniform entry point for every Ejentic system that needs retrieval
 * augmentation. Thin, dependency-free (uses global `fetch`), and built around
 * the backend's frozen contracts.
 *
 * @example
 *   const rag = new RAGClient({ baseUrl: "http://localhost:8002", platform: "leadgen" });
 *   const { response, tokenUsage } = await rag.query("What services do we offer?", {
 *     clearanceLevel: "employee",
 *   });
 */
export class RAGClient {
  private readonly baseUrl: string;
  private readonly client?: string;
  private readonly platform: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: RAGClientOptions) {
    if (!opts.baseUrl) throw new Error("RAGClient: baseUrl is required");
    this.baseUrl = stripTrailingSlashes(opts.baseUrl);
    this.client = opts.client;
    this.platform = opts.platform ?? "SDK";
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    this.fetchImpl = opts.fetchImpl ?? ((...args) => fetch(...args));
  }

  private body(opts?: RAGQueryOptions) {
    return {
      query: "",
      clearance_level: (opts?.clearanceLevel ?? "guest").toLowerCase(),
      platform: opts?.platform ?? this.platform,
      client: opts?.client ?? this.client ?? "",
    };
  }

  private async rawRequest(
    path: string,
    init: RequestInit,
  ): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const outer = init.signal;
    if (outer) {
      if (outer.aborted) controller.abort();
      else outer.addEventListener("abort", () => controller.abort(), { once: true });
    }
    try {
      return await this.fetchImpl(this.baseUrl + path, {
        ...init,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  }

  private async requestJson<T>(path: string, init: RequestInit): Promise<T> {
    const res = await this.rawRequest(path, init);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = (await res.json()) as { detail?: string };
        if (body.detail) detail = body.detail;
      } catch {
        /* non-JSON error body */
      }
      throw new Error(`RAG ${path} failed (${res.status}): ${detail}`);
    }
    return (await res.json()) as T;
  }

  /** Non-streaming, grounded answer — `POST /api/rag`. */
  async query(query: string, opts?: RAGQueryOptions): Promise<RAGResponse> {
    if (!query || !query.trim()) throw new Error("RAGClient.query: query is required");
    const body = this.body(opts);
    body.query = query;
    const res = await this.rawRequest("/api/rag", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = (await res.json()) as { detail?: string };
        if (j.detail) detail = j.detail;
      } catch {
        /* ignore */
      }
      throw new Error(`RAG query failed (${res.status}): ${detail}`);
    }
    const json = (await res.json()) as { status: "success"; response: string };
    return {
      status: json.status,
      response: json.response,
      tokenUsage: readTokenUsage(res),
    };
  }

/** Streaming, grounded answer — `POST /chat` (Server-Sent Events). */
  async *stream(query: string, opts?: RAGQueryOptions): AsyncGenerator<ChatStreamEvent> {
    if (!query || !query.trim()) throw new Error("RAGClient.stream: query is required");
    const body = this.body(opts);
    body.query = query;

    const res = await this.rawRequest("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) {
      throw new Error(`RAG chat failed (${res.status}: ${res.statusText})`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let sep: number;
        while ((sep = buffer.indexOf("\n\n")) !== -1) {
          const raw = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          const dataLine = raw
            .split("\n")
            .find((line) => line.trim().startsWith("data: "));
          if (!dataLine) continue;
          const data = dataLine.slice("data: ".length).trim();
          if (data === DONE_SENTINEL) {
            yield { type: "done" };
            return;
          }
          try {
            const parsed = JSON.parse(data) as { chunk?: string; error?: string };
            if (typeof parsed.error === "string") {
              yield { type: "error", error: parsed.error };
              return;
            }
            if (typeof parsed.chunk === "string" && parsed.chunk.length > 0) {
              yield { type: "chunk", text: parsed.chunk };
            }
          } catch {
            /* skip malformed events */
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  /** Concatenate a whole streamed answer into one string (no events exposed). */
  async streamText(query: string, opts?: RAGQueryOptions): Promise<string> {
    let out = "";
    for await (const event of this.stream(query, opts)) {
      if (event.type === "chunk") out += event.text;
      if (event.type === "error") throw new Error(`RAG stream error: ${event.error}`);
    }
    return out;
  }

  /** Index a one-off document (PDF/text) for the caller's session — `POST /upload`. */
  async upload(
    file: Blob | File,
    filename: string,
    signal?: AbortSignal,
  ): Promise<RAGUploadResponse> {
    const form = new FormData();
    form.append("file", file, filename);
    return this.requestJson<RAGUploadResponse>("/upload", {
      method: "POST",
      body: form,
      signal,
    });
  }

  /** Token-accounting dashboard — `GET /metrics`. */
  async metrics(signal?: AbortSignal): Promise<MetricsResponse> {
    return this.requestJson<MetricsResponse>("/metrics", { method: "GET", signal });
  }

  /** Registered client configs — `GET /clients` (admin surface). */
  async clients(signal?: AbortSignal): Promise<ClientSummary[]> {
    const res = await this.requestJson<{ clients: ClientSummary[] }>("/clients", {
      method: "GET",
      signal,
    });
    return res.clients;
  }

  /** Liveness + active client info — `GET /`. */
  async health(signal?: AbortSignal): Promise<HealthResponse> {
    return this.requestJson<HealthResponse>("/", { method: "GET", signal });
  }
}