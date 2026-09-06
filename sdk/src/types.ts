/**
 * Shared types for the Ejentic Enterprise RAG SDK.
 *
 * These mirror the *frozen* contracts of the FastAPI backend (see
 * backend/main.py): the JSON shapes are byte-for-byte stable because n8n, the
 * web UI and any number of Ejentic services depend on them. Do not rename
 * fields here without a coordinated backend change.
 */

/** Optional per-request options. `client` forwards to the active instance's
 *  multi-tenant guard; it must match the instance's RAG_CLIENT or the backend
 *  answers HTTP 409. */
export interface RAGQueryOptions {
  /** Clearance role to answer AS. The backend derives your real role from the
   *  API key and only ever NARROWS: you may request a role your key already
   *  covers or below (an executive key asking for the "guest" view), but asking
   *  for MORE than your key grants is HTTP 403, never a silent downgrade.
   *  Omit it to get everything your key grants. */
  clearanceLevel?: string;
  /** Where the query originates, for audit-log labelling. */
  platform?: string;
  /** Target client id. Defaults to the backend instance's active client. */
  client?: string;
  /** Token from a prior `upload()`, to fold that document into THIS query's
   *  context. Uploads are per-caller: without the token the backend does not
   *  see the document, and no other caller ever can. */
  uploadToken?: string;
  /** Abort signal (cancels the HTTP call). */
  signal?: AbortSignal;
}

/** Token accounting the backend reports per query (via X-* headers). */
export interface TokenUsage {
  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;
  /** "provider" | "estimate" | "mixed" | "gate" | "none". */
  tokenSource: string | null;
  /** True when the confidence gate refused instead of synthesizing. */
  gated: boolean;
  /** Estimated input tokens saved by the gate skipping the LLM. */
  savedTokens: number | null;
}

/** Response shape of `POST /api/rag`. */
export interface RAGResponse {
  status: "success";
  response: string;
  tokenUsage: TokenUsage;
}

/** Payload the backend logs per query (metrics surface). */
export interface QueryLogRow {
  id: number;
  timestamp: string | null;
  clearance_level: string;
  query: string;
  gated: boolean;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  estimated_saved_tokens: number | null;
  token_source: string | null;
}

/** Shape of `GET /metrics`. */
export interface MetricsResponse {
  totals: {
    queries: number;
    answered: number;
    gated: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    estimated_saved_tokens: number;
    avg_tokens_per_answer: number;
  };
  recent: QueryLogRow[];
  config: {
    client: string;
    llm_model: string;
    confidence_threshold: number;
    retrieve_top_k: number;
    rerank_top_n: number;
    query_rewrite_enabled: boolean;
  };
}

/** Per-client summary from `GET /clients` (admin surface). */
export interface ClientSummary {
  id: string;
  name: string;
  description: string;
  index_name: string;
  namespace: string;
  llm_model: string;
  embed_model: string;
  clearance_roles: string[];
}

/** Shape of `GET /clients`. */
export interface ClientsResponse {
  active_client: string;
  client_count: number;
  clients: ClientSummary[];
}

/** Auth posture reported by `GET /`. Exposes no key and no env-var value —
 *  only WHETHER auth is on and which roles are usable, so an unprotected
 *  instance is visible at a glance instead of being assumed safe. */
export interface AuthStatus {
  /** False means the instance accepts unauthenticated callers (local dev only). */
  required: boolean;
  /** Every role declared in the client's config. */
  roles: string[];
  /** Roles whose key env var is actually set — the ones that can be used. */
  roles_with_keys_set: string[];
  /** Declared roles with no key set: unusable until a key is provided. */
  roles_missing_keys: string[];
  /** Role permitted to call administrative endpoints (e.g. POST /ingest). */
  admin_role: string | null;
}

/** Shape of `GET /`. */
export interface HealthResponse {
  status: string;
  message: string;
  client: string;
  client_name: string;
  hybrid_search: boolean;
  reranker: string | null;
  index: string;
  token_metering: boolean;
  metrics_endpoint: string;
  auth: AuthStatus;
}

/** Shape of `POST /upload`. */
export interface RAGUploadResponse {
  status: "success";
  message: string;
  /** Opaque handle for the indexed document. Pass it back as
   *  `RAGQueryOptions.uploadToken` to query against it. Uploads are scoped to
   *  whoever holds the token — they are NOT added to the shared index. */
  upload_token: string;
  /** The original filename, kept as metadata only. The bytes are stored under a
   *  server-generated name, so a hostile filename cannot steer the write path. */
  filename: string;
}

/** Parsed streaming event from `POST /chat`. */
export type ChatStreamEvent =
  | { type: "chunk"; text: string }
  | { type: "done" }
  | { type: "error"; error: string };

/** Constructor options for RAGClient. */
export interface RAGClientOptions {
  /** Base URL of the RAG backend, e.g. "http://localhost:8002". */
  baseUrl: string;
  /**
   * API key for the caller's role, sent as the `X-API-Key` header on every
   * request. THE KEY IS THE IDENTITY: the backend reads the caller's clearance
   * from it, so `clearanceLevel` can only narrow within what this key grants.
   *
   * Keep it server-side. A key in browser JavaScript is a public key — anyone
   * who opens devtools has executive clearance. Call the RAG from a server
   * route (Next.js route handler, API endpoint) that holds the key, and let the
   * browser talk to that route instead.
   *
   * Optional only because a local instance may run with `auth.required: false`.
   * Any deployment that serves real people sets it.
   */
  apiKey?: string;
  /** Default client id sent with every request. */
  client?: string;
  /** Default platform label used for audit logging. */
  platform?: string;
  /** Request timeout in ms (default 300_000). */
  timeoutMs?: number;
  /** Injectable fetch (for tests / edge runtimes). */
  fetchImpl?: typeof fetch;
}