import { describe, expect, it, vi } from "vitest";
import { RAGClient } from "../src/client";
import { getClient, isClientRegistered, listClients } from "../src/registry";

function mockClient(handler: (url: string, init: RequestInit) => Promise<Response>) {
  const fetchImpl = vi.fn(handler) as unknown as typeof fetch;
  return new RAGClient({ baseUrl: "http://rag.test:8002", platform: "test", fetchImpl });
}

function sseResponse(payload: string): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(payload));
      controller.close();
    },
  });
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: new Headers({ "Content-Type": "text/event-stream" }),
    body: stream,
  } as unknown as Response;
}

describe("RAGClient.stream (SSE)", () => {
  it("collects chunks and yields a done event", async () => {
    const client = mockClient(async () =>
      sseResponse('data: {"chunk": "Hello "}\n\ndata: {"chunk": "world"}\n\ndata: [DONE]\n\n'),
    );
    const parts: string[] = [];
    for await (const evt of client.stream("hi")) {
      if (evt.type === "chunk") parts.push(evt.text);
      if (evt.type === "done") parts.push("<done>");
    }
    expect(parts).toEqual(["Hello ", "world", "<done>"]);
  });

  it("surfaces stream errors emitted by the backend", async () => {
    const client = mockClient(async () =>
      sseResponse('data: {"error": "gate refused"}\n\ndata: [DONE]\n\n'),
    );
    const events: string[] = [];
    for await (const evt of client.stream("hi")) {
      events.push(evt.type);
    }
    expect(events).toEqual(["error"]);
  });
});

describe("registry", () => {
  it("knows the default deployment and fails loudly on unknown ids", () => {
    expect(isClientRegistered("ejentic")).toBe(true);
    expect(isClientRegistered("ghost")).toBe(false);
    expect(listClients().some((c) => c.id === "ejentic")).toBe(true);
    expect(() => getClient("ghost")).toThrow(/not registered/);
  });
});