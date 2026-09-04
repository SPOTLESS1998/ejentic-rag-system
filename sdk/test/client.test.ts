import { describe, expect, it, vi } from "vitest";
import { RAGClient } from "../src/client";
import { ClientSummary, MetricsResponse, RAGResponse } from "../src/types";

/** Build a Response-like from a plain object (no undici dependency). */
function mockResponse(
  body: unknown,
  { status = 200, statusText, headers = {} }: { status?: number; statusText?: string; headers?: Record<string, string> } = {},
): Response {
  const ok = status >= 200 && status < 300;
  return {
    ok,
    status,
    statusText: statusText ?? (ok ? "OK" : "Error"),
    headers: new Headers(headers),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function mockClient(handler: (url: string, init: RequestInit) => Promise<Response>) {
  const fetchImpl = vi.fn(handler) as unknown as typeof fetch;
  return new RAGClient({ baseUrl: "http://rag.test:8002", platform: "test", fetchImpl });
}

const RAG_BODY: RAGResponse = {
  status: "success",
  response: "Ejentic offers RAG, agents, and fine-tuning.",
  tokenUsage: {
    promptTokens: 42,
    completionTokens: 17,
    totalTokens: 59,
    tokenSource: "provider",
    gated: false,
    savedTokens: null,
  },
};

describe("RAGClient.query", () => {
  it("posts the frozen body and parses token headers", async () => {
    const client = mockClient(async (url, init) => {
      expect(url).toBe("http://rag.test:8002/api/rag");
      expect(init.method).toBe("POST");
      const body = JSON.parse(String(init.body));
      expect(body).toMatchObject({
        query: "What services?",
        clearance_level: "employee",
        platform: "test",
        client: "",
      });
      return mockResponse(RAG_BODY, {
        headers: {
          "X-Prompt-Tokens": "42",
          "X-Completion-Tokens": "17",
          "X-Total-Tokens": "59",
          "X-Token-Source": "provider",
          "X-Gated": "false",
          "X-Saved-Tokens": "0",
        },
      });
    });

    const res = await client.query("What services?", { clearanceLevel: "employee" });
    expect(res.status).toBe("success");
    expect(res.response).toContain("RAG");
    expect(res.tokenUsage).toMatchObject({
      promptTokens: 42,
      completionTokens: 17,
      totalTokens: 59,
      tokenSource: "provider",
      gated: false,
      savedTokens: 0,
    });
  });

  it("surfaces multi-tenant 409 as a clear error", async () => {
    const client = mockClient(async () =>
      mockResponse({ detail: "client 'acme' is not the active client..." }, { status: 409 }),
    );
    await expect(client.query("hi", { client: "acme" })).rejects.toThrow(/409/);
  });

  it("rejects an empty query without hitting the network", async () => {
    const client = mockClient(async () => {
      throw new Error("should not be called");
    });
    await expect(client.query("   ")).rejects.toThrow(/query is required/);
  });
});

describe("RAGClient.health / metrics / clients", () => {
  it("health returns the active client", async () => {
    const client = mockClient(async (url) => {
      expect(url).toBe("http://rag.test:8002/");
      return mockResponse({
        status: "ok",
        message: "ok",
        client: "ejentic",
        client_name: "Ejentic AI Knowledge Base",
        hybrid_search: true,
        reranker: null,
        index: "ejentic-global",
        token_metering: true,
        metrics_endpoint: "/metrics",
      });
    });
    const h = await client.health();
    expect(h.client).toBe("ejentic");
    expect(h.index).toBe("ejentic-global");
  });

  it("metrics parse the token dashboard", async () => {
    const metrics: MetricsResponse = {
      totals: {
        queries: 10,
        answered: 8,
        gated: 2,
        prompt_tokens: 500,
        completion_tokens: 300,
        total_tokens: 800,
        estimated_saved_tokens: 120,
        avg_tokens_per_answer: 100,
      },
      recent: [],
      config: {
        client: "ejentic",
        llm_model: "llama",
        confidence_threshold: 0.2,
        retrieve_top_k: 10,
        rerank_top_n: 4,
        query_rewrite_enabled: true,
      },
    };
    const client = mockClient(async () => mockResponse(metrics));
    const m = await client.metrics();
    expect(m.totals.queries).toBe(10);
    expect(m.totals.estimated_saved_tokens).toBe(120);
  });

  it("clients() returns the registered summaries", async () => {
    const summaries: ClientSummary[] = [
      {
        id: "ejentic",
        name: "Ejentic AI",
        description: "default",
        index_name: "ejentic-global",
        namespace: "ejentic-internal",
        llm_model: "llama",
        embed_model: "e5",
        clearance_roles: ["employee", "executive", "guest"],
      },
    ];
    const client = mockClient(async () =>
      mockResponse({ active_client: "ejentic", client_count: 1, clients: summaries }),
    );
    const list = await client.clients();
    expect(list).toHaveLength(1);
    expect(list[0].id).toBe("ejentic");
  });
});