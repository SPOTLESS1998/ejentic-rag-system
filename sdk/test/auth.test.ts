/**
 * The SDK's half of the auth boundary.
 *
 * The backend decides clearance from the API key (backend/auth.py). The SDK's only
 * job is to put that key on EVERY authenticated call and never to invent a
 * clearance the caller didn't ask for. Both halves are easy to break silently:
 *
 *  - drop the header on one endpoint and that endpoint just 401s in production;
 *  - default `clearance_level` to "guest" and an executive key quietly answers
 *    public-only, which looks like broken retrieval, not a client bug.
 *
 * These tests pin both. No server, no keys, no network.
 */
import { describe, expect, it, vi } from "vitest";
import { RAGClient } from "../src/client";

const API_KEY = "test-key-not-a-real-secret";

function mockResponse(body: unknown, status = 200): Response {
  const ok = status >= 200 && status < 300;
  return {
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    headers: new Headers(),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

/** Capture every request the SDK makes so we can assert on its headers. */
function spyClient(opts: { apiKey?: string } = {}) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith("/clients")) return mockResponse({ clients: [] });
    if (String(url).endsWith("/metrics")) return mockResponse({ totals: {}, recent: [] });
    if (String(url).endsWith("/upload")) {
      return mockResponse({ status: "success", message: "ok", upload_token: "tok", filename: "a.pdf" });
    }
    return mockResponse({ status: "success", response: "answer" });
  }) as unknown as typeof fetch;

  const client = new RAGClient({
    baseUrl: "http://rag.test:8002",
    platform: "test",
    fetchImpl,
    ...opts,
  });
  return { client, calls };
}

/** Header lookup that tolerates either a plain object or a Headers instance. */
function header(init: RequestInit, name: string): string | null {
  const h = init.headers;
  if (!h) return null;
  if (h instanceof Headers) return h.get(name);
  const rec = h as Record<string, string>;
  const hit = Object.keys(rec).find((k) => k.toLowerCase() === name.toLowerCase());
  return hit ? rec[hit] : null;
}

describe("API key plumbing", () => {
  it("sends X-API-Key on every authenticated endpoint", async () => {
    const { client, calls } = spyClient({ apiKey: API_KEY });

    await client.query("hi");
    await client.streamText("hi").catch(() => {
      /* mock body isn't a real stream; the request is what we're asserting */
    });
    await client.metrics();
    await client.clients();
    await client.upload(new Blob(["x"]), "a.pdf");

    expect(calls.length).toBeGreaterThanOrEqual(5);
    for (const { url, init } of calls) {
      expect(header(init, "X-API-Key"), `missing key on ${url}`).toBe(API_KEY);
    }
  });

  it("omits the header entirely when no key is configured", async () => {
    // An instance running auth.required:false must still work — an empty or
    // literal "undefined" header value would be a bug, not an absent one.
    const { client, calls } = spyClient();
    await client.query("hi");
    expect(header(calls[0].init, "X-API-Key")).toBeNull();
  });

  it("leaves GET / (liveness) unauthenticated", async () => {
    // Load balancers hit this with no credential; requiring one would make the
    // instance look dead.
    const { client, calls } = spyClient({ apiKey: API_KEY });
    await client.health();
    expect(calls[0].url).toBe("http://rag.test:8002/");
    expect(header(calls[0].init, "X-API-Key")).toBeNull();
  });

  it("does not put the key in the URL or the body", async () => {
    // A key in a query string lands in access logs and browser history forever.
    const { client, calls } = spyClient({ apiKey: API_KEY });
    await client.query("hi");
    expect(calls[0].url).not.toContain(API_KEY);
    expect(String(calls[0].init.body)).not.toContain(API_KEY);
  });
});

describe("clearance is requested, never assumed", () => {
  it("defaults clearance_level to empty, not 'guest'", async () => {
    // The backend reads "" as "everything my key grants". Defaulting to "guest"
    // here would narrow every executive key down to public material.
    const { client, calls } = spyClient({ apiKey: API_KEY });
    await client.query("hi");
    expect(JSON.parse(String(calls[0].init.body)).clearance_level).toBe("");
  });

  it("passes an explicit clearance through, lower-cased", async () => {
    const { client, calls } = spyClient({ apiKey: API_KEY });
    await client.query("hi", { clearanceLevel: "Employee" });
    expect(JSON.parse(String(calls[0].init.body)).clearance_level).toBe("employee");
  });

  it("surfaces a widening refusal (403) as an error, not a downgrade", async () => {
    // The dangerous failure mode is a client that swallows the 403 and shows the
    // caller a narrower answer as if it were complete.
    const fetchImpl = vi.fn(async () =>
      mockResponse({ detail: "role 'guest' cannot request 'executive'" }, 403),
    ) as unknown as typeof fetch;
    const client = new RAGClient({ baseUrl: "http://rag.test:8002", apiKey: API_KEY, fetchImpl });
    await expect(client.query("hi", { clearanceLevel: "executive" })).rejects.toThrow(/403/);
  });

  it("surfaces a missing/invalid key (401) as an error", async () => {
    const fetchImpl = vi.fn(async () =>
      mockResponse({ detail: "missing API key" }, 401),
    ) as unknown as typeof fetch;
    const client = new RAGClient({ baseUrl: "http://rag.test:8002", fetchImpl });
    await expect(client.query("hi")).rejects.toThrow(/401/);
  });
});

describe("per-caller uploads", () => {
  it("returns an upload token", async () => {
    const { client } = spyClient({ apiKey: API_KEY });
    const res = await client.upload(new Blob(["x"]), "a.pdf");
    expect(res.upload_token).toBe("tok");
  });

  it("sends the upload token only when one is given", async () => {
    // Uploads are scoped by token: no token on the query means the backend does
    // not fold that document in, so one caller's PDF can't reach another's.
    const { client, calls } = spyClient({ apiKey: API_KEY });
    await client.query("hi");
    expect(header(calls[0].init, "X-Upload-Token")).toBeNull();

    await client.query("hi", { uploadToken: "tok-abc" });
    expect(header(calls[1].init, "X-Upload-Token")).toBe("tok-abc");
  });

  it("does not set Content-Type on upload (fetch adds the multipart boundary)", async () => {
    // Setting it by hand omits the boundary and the server can't parse the form.
    const { client, calls } = spyClient({ apiKey: API_KEY });
    await client.upload(new Blob(["x"]), "a.pdf");
    expect(header(calls[0].init, "Content-Type")).toBeNull();
  });
});
