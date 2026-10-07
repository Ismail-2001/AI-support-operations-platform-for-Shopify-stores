import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { parseConnection, useConnection } from "../useConnection";

const STORAGE_KEY = "cs-agent-console-connection";

describe("useConnection", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("starts with no connection when storage is empty", () => {
    const { result } = renderHook(() => useConnection());
    expect(result.current.connection).toBeNull();
  });

  it("recovers from corrupt JSON instead of white-screening", () => {
    localStorage.setItem(STORAGE_KEY, "{not json!!");
    const { result } = renderHook(() => useConnection());
    expect(result.current.connection).toBeNull();
    // the bad value is removed so the Connect screen shows on reload
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("rejects well-formed JSON of the wrong shape", () => {
    for (const bad of ["5", '"just a string"', "null", "[1,2]", '{"url":"x","token":"y"}']) {
      localStorage.clear();
      localStorage.setItem(STORAGE_KEY, bad);
      const { result } = renderHook(() => useConnection());
      expect(result.current.connection).toBeNull();
      expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    }
  });

  it("loads a valid stored connection", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ baseUrl: "http://localhost:8001", apiKey: "k" }),
    );
    const { result } = renderHook(() => useConnection());
    expect(result.current.connection).toEqual({
      baseUrl: "http://localhost:8001",
      apiKey: "k",
    });
  });

  it("persists a connection and clears it on disconnect", () => {
    const { result } = renderHook(() => useConnection());
    act(() => {
      result.current.setConnection({ baseUrl: "http://localhost:8001", apiKey: "k" });
    });
    expect(result.current.connection).toEqual({
      baseUrl: "http://localhost:8001",
      apiKey: "k",
    });
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).apiKey).toBe("k");

    act(() => {
      result.current.setConnection(null);
    });
    expect(result.current.connection).toBeNull();
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("ignores a corrupt value arriving from another tab", () => {
    const { result } = renderHook(() => useConnection());
    expect(result.current.connection).toBeNull();

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: STORAGE_KEY, newValue: "{broken" }),
      );
    });
    expect(result.current.connection).toBeNull();
  });

  it("accepts a valid value arriving from another tab", () => {
    const { result } = renderHook(() => useConnection());
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: STORAGE_KEY,
          newValue: JSON.stringify({ baseUrl: "http://other-tab:8001", apiKey: "z" }),
        }),
      );
    });
    expect(result.current.connection).toEqual({
      baseUrl: "http://other-tab:8001",
      apiKey: "z",
    });
  });
});

describe("parseConnection", () => {
  it("returns null for null input", () => {
    expect(parseConnection(null)).toBeNull();
  });

  it("requires a non-empty baseUrl", () => {
    expect(parseConnection(JSON.stringify({ baseUrl: "", apiKey: "k" }))).toBeNull();
    expect(
      parseConnection(JSON.stringify({ baseUrl: "http://x", apiKey: "" })),
    ).toEqual({ baseUrl: "http://x", apiKey: "" });
  });
});
