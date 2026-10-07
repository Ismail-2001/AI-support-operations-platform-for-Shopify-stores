import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, UNAUTHORIZED_EVENT } from "../api";

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const CONN = { baseUrl: "http://api.test", apiKey: "bad-key" };

describe("central 401 handling", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("dispatches cs-api-unauthorized on a 401 response", async () => {
    const handler = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, handler);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "Invalid API key" }, 401)),
    );

    await expect(api.health(CONN)).rejects.toMatchObject({
      status: 401,
      message: "Invalid API key",
    });
    expect(handler).toHaveBeenCalledTimes(1);
    const event = handler.mock.calls[0][0] as CustomEvent<string>;
    expect(event.detail).toBe("Invalid API key");
    window.removeEventListener(UNAUTHORIZED_EVENT, handler);
  });

  it("does not dispatch for non-401 failures", async () => {
    const handler = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, handler);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "boom" }, 500)),
    );

    await expect(api.health(CONN)).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
    window.removeEventListener(UNAUTHORIZED_EVENT, handler);
  });

  it("still throws ApiError with the parsed detail (per-call handling)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "Invalid API key" }, 401)),
    );
    const err = await api.health(CONN).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
    expect((err as ApiError).message).toBe("Invalid API key");
  });
});
