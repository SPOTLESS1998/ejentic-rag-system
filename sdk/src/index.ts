import { RAGClient } from "./client.js";
import { ClientSummary, RAGQueryOptions } from "./types.js";

export { RAGClient } from "./client.js";
export type {
  ChatStreamEvent,
  ClientSummary,
  ClientsResponse,
  HealthResponse,
  MetricsResponse,
  QueryLogRow,
  RAGClientOptions,
  RAGQueryOptions,
  RAGResponse,
  RAGUploadResponse,
  TokenUsage,
} from "./types.js";

/**
 * Simplest possible entry point: build a client for the DEFAULT RAG backend.
 * Applications that talk to one RAG instance can use this directly.
 *
 * @example
 *   import { rag } from "@ejentic/rag-sdk";
 *   const answer = await rag.query("What are core hours?", { clearanceLevel: "employee" });
 */
export function createClient(baseUrl: string, options?: Partial<ConstructorParameters<typeof RAGClient>[0]>) {
  return new RAGClient({ baseUrl, ...options });
}

let _default: RAGClient | null = null;

/** Lazily-built default client from `EJENTIC_RAG_URL` (falls back to localhost). */
export function rag(): RAGClient {
  if (!_default) {
    const baseUrl =
      typeof process !== "undefined" && process.env?.EJENTIC_RAG_URL
        ? process.env.EJENTIC_RAG_URL
        : "http://localhost:8002";
    _default = new RAGClient({ baseUrl, platform: "SDK-default" });
  }
  return _default;
}

export { DEFAULT_CLIENT_ID, isClientRegistered, listClients, getClient } from "./registry.js";