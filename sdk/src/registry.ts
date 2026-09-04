import { RAGClient } from "./client";
import { ClientSummary } from "./types";

/**
 * Lightweight SDK-side registry of known Ejentic RAG deployments.
 *
 * The BACKEND is the source of truth for full client configs (GET /clients);
 * this registry only maps a client id -> RAG base URL so TypeScript services
 * can resolve "which deployment do I talk to?" without hardcoding URLs.
 * Add a deployment here when you stand one up (see backend/clients/<id>.json).
 */

export const DEFAULT_CLIENT_ID = "ejentic";

export const DEFAULT_KNOWN_URL = "http://localhost:8002";

// id -> base URL for each known deployment. Keep this in sync with the
// backend's backend/clients/*.json registry and any deployed instances.
const KNOWN_DEPLOYMENTS: Record<string, string> = {
  [DEFAULT_CLIENT_ID]: DEFAULT_KNOWN_URL,
};

/** True if a deployment id is registered in this SDK. */
export function isClientRegistered(id: string): boolean {
  return Object.prototype.hasOwnProperty.call(KNOWN_DEPLOYMENTS, id);
}

/** Compact summaries of every deployment this SDK knows about. */
export function listClients(): ClientSummary[] {
  return Object.entries(KNOWN_DEPLOYMENTS).map(([id, baseUrl]) => ({
    id,
    name: id,
    description: `RAG deployment (baseUrl: ${baseUrl})`,
    index_name: "",
    namespace: "",
    llm_model: "",
    embed_model: "",
    clearance_roles: [],
  }));
}

/**
 * Resolve a known deployment id to a configured RAGClient (default: the
 * "ejentic" client). Throws for unknown ids so code never silently points at
 * the wrong index.
 */
export function getClient(id: string = DEFAULT_CLIENT_ID, platform?: string): RAGClient {
  const baseUrl = KNOWN_DEPLOYMENTS[id];
  if (!baseUrl) {
    throw new Error(
      `RAG deployment '${id}' is not registered in the SDK. Known: ${Object.keys(
        KNOWN_DEPLOYMENTS,
      ).join(", ")}.`,
    );
  }
  return new RAGClient({ baseUrl, client: id, platform: platform ?? "SDK" });
}