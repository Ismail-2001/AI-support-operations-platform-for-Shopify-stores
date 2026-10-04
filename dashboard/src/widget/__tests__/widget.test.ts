import { describe, it, expect, vi, afterEach } from "vitest";
import { createSSEParser, initWidget, stageLabel, STAGE_LABELS } from "../widget";

const CONFIG = {
  enabled: true,
  title: "Help Center",
  greeting: "Hi",
  welcome_message: "Welcome!",
  color: "#223344",
  logo_url: "",
  show_confidence: true,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sseBody(events: Array<{ event: string; data: Record<string, unknown> }>): string {
  return events.map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}`).join("\n\n") + "\n\n";
}

function memoryStorage(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
}

function host(): Element | null {
  return document.querySelector("[data-cs-chat]");
}

function shadow(): ShadowRoot {
  const h = host();
  if (!h || !h.shadowRoot) throw new Error("widget host not mounted");
  return h.shadowRoot;
}

function submit(root: ShadowRoot, text: string): void {
  const input = root.querySelector<HTMLInputElement>(".composer input");
  const form = root.querySelector<HTMLFormElement>(".composer");
  if (!input || !form) throw new Error("composer not rendered");
  input.value = text;
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
}

type FetchMock = ReturnType<typeof vi.fn>;

async function start(fetchMock: FetchMock, storage = memoryStorage()) {
  const widget = await initWidget({
    apiKey: "test-key",
    apiBase: "https://api.test",
    fetchImpl: fetchMock as unknown as typeof fetch,
    storage,
  });
  expect(widget).not.toBeNull();
  const root = shadow();
  root.querySelector<HTMLButtonElement>(".launcher")!.click();
  await vi.waitFor(() => expect(root.querySelector(".msg.system")).toBeTruthy());
  return { widget, root };
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("stageLabel", () => {
  it("maps known stages to human labels", () => {
    expect(stageLabel("classify_ticket")).toBe(STAGE_LABELS.classify_ticket);
    expect(stageLabel("generate_response")).toBe("Writing a reply…");
  });

  it("falls back for unknown stages", () => {
    expect(stageLabel("mystery_stage")).toBe("Working…");
  });
});

describe("createSSEParser", () => {
  it("parses a complete event", () => {
    const parse = createSSEParser();
    expect(parse('event: stage\ndata: {"stage":"classify_ticket"}\n\n')).toEqual([
      { event: "stage", data: { stage: "classify_ticket" } },
    ]);
  });

  it("buffers events split across chunks", () => {
    const parse = createSSEParser();
    expect(parse('event: message\ndata: {"content":"hel')).toEqual([]);
    expect(parse('lo"}\n\n')).toEqual([{ event: "message", data: { content: "hello" } }]);
  });

  it("parses multiple events from one chunk", () => {
    const parse = createSSEParser();
    const events = parse('event: stage\ndata: {"stage":"a"}\n\nevent: done\ndata: {"ok":true}\n\n');
    expect(events).toEqual([
      { event: "stage", data: { stage: "a" } },
      { event: "done", data: { ok: true } },
    ]);
  });

  it("defaults the event name to message", () => {
    const parse = createSSEParser();
    expect(parse('data: {"content":"hi"}\n\n')).toEqual([
      { event: "message", data: { content: "hi" } },
    ]);
  });

  it("skips malformed JSON without throwing", () => {
    const parse = createSSEParser();
    const events = parse('event: stage\ndata: {broken\n\nevent: message\ndata: {"content":"ok"}\n\n');
    expect(events).toEqual([{ event: "message", data: { content: "ok" } }]);
  });

  it("skips blocks with no data lines", () => {
    const parse = createSSEParser();
    expect(parse("event: ping\n\n")).toEqual([]);
  });
});

describe("initWidget config gate", () => {
  it("renders a launcher and sends the widget key on the config request", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse(CONFIG),
    );
    const widget = await initWidget({
      apiKey: "test-key",
      apiBase: "https://api.test",
      fetchImpl: fetchMock as unknown as typeof fetch,
      storage: memoryStorage(),
    });
    expect(widget).not.toBeNull();

    const root = shadow();
    const launcher = root.querySelector<HTMLButtonElement>(".launcher");
    expect(launcher).toBeTruthy();
    expect(launcher!.getAttribute("aria-label")).toBe("Open Help Center");
    expect(launcher!.getAttribute("aria-expanded")).toBe("false");

    const configCall = fetchMock.mock.calls.find(([u]) => String(u).includes("/chat/config"));
    expect(configCall).toBeTruthy();
    expect(String(configCall![0])).toBe("https://api.test/chat/config?key=test-key");
    expect((configCall![1] as RequestInit).headers).toMatchObject({ "X-Widget-Key": "test-key" });
  });

  it("returns null when the key is rejected", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ error: "unauthorized" }, 401));
    const widget = await initWidget({
      apiKey: "bad",
      apiBase: "",
      fetchImpl: fetchMock as unknown as typeof fetch,
      storage: memoryStorage(),
    });
    expect(widget).toBeNull();
    expect(host()).toBeNull();
  });

  it("returns null when the widget is disabled", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ...CONFIG, enabled: false }));
    const widget = await initWidget({
      apiKey: "test-key",
      apiBase: "",
      fetchImpl: fetchMock as unknown as typeof fetch,
      storage: memoryStorage(),
    });
    expect(widget).toBeNull();
    expect(host()).toBeNull();
  });

  it("returns null when the config request fails", async () => {
    const fetchMock = vi.fn(async () => {
      throw new Error("backend down");
    });
    const widget = await initWidget({
      apiKey: "test-key",
      apiBase: "",
      fetchImpl: fetchMock as unknown as typeof fetch,
      storage: memoryStorage(),
    });
    expect(widget).toBeNull();
    expect(host()).toBeNull();
  });

  it("destroy() removes the widget from the DOM", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(CONFIG));
    const widget = await initWidget({
      apiKey: "test-key",
      apiBase: "",
      fetchImpl: fetchMock as unknown as typeof fetch,
      storage: memoryStorage(),
    });
    expect(host()).toBeTruthy();
    widget!.destroy();
    expect(host()).toBeNull();
  });

  it("mounts into a provided container", async () => {
    const container = document.createElement("section");
    document.body.appendChild(container);
    const fetchMock = vi.fn(async () => jsonResponse(CONFIG));
    await initWidget({
      apiKey: "test-key",
      apiBase: "",
      fetchImpl: fetchMock as unknown as typeof fetch,
      storage: memoryStorage(),
      mount: container,
    });
    expect(container.querySelector("[data-cs-chat]")).toBeTruthy();
    expect(document.body.querySelector(":scope > [data-cs-chat]")).toBeNull();
  });
});

describe("initWidget conversation", () => {
  it("creates a session, streams a reply, and handles handoff", async () => {
    const sse = sseBody([
      { event: "stage", data: { stage: "classify_ticket" } },
      { event: "stage", data: { stage: "generate_response" } },
      {
        event: "message",
        data: {
          content: "Hi! How can I help?",
          confidence: 0.92,
          show_confidence: true,
          needs_human: true,
        },
      },
    ]);

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.includes("/chat/config")) return jsonResponse(CONFIG);
      if (method === "POST" && url.endsWith("/chat/sessions"))
        return jsonResponse({ session_id: "s1", history: [] });
      if (method === "GET" && url.includes("/chat/sessions/s1"))
        return jsonResponse({ session_id: "s1", history: [] });
      if (url.endsWith("/handoff"))
        return jsonResponse({ ticket_id: "t1", status: "handoff_requested" });
      if (url.endsWith("/messages")) return new Response(sse);
      return jsonResponse({ error: "not found" }, 404);
    });

    const { root } = await start(fetchMock);
    const launcher = root.querySelector<HTMLButtonElement>(".launcher")!;
    const panel = root.querySelector(".panel")!;
    expect(launcher.getAttribute("aria-expanded")).toBe("true");
    expect(panel.classList.contains("open")).toBe(true);
    expect(root.querySelector(".msg.system")!.textContent).toBe("Welcome!");

    const createCall = fetchMock.mock.calls.find(
      ([u, i]) => String(u).endsWith("/chat/sessions") && i?.method === "POST",
    );
    expect(createCall).toBeTruthy();

    submit(root, "Where is my order?");
    const input = root.querySelector<HTMLInputElement>(".composer input")!;
    expect(input.value).toBe("");

    await vi.waitFor(() => {
      expect(root.querySelector(".msg.customer")!.textContent).toBe("Where is my order?");
      expect(root.querySelector(".msg.assistant")!.textContent).toContain("Hi! How can I help?");
    });
    expect(root.querySelector(".typing")).toBeNull();
    expect(root.querySelector(".chip")!.textContent).toBe("confidence 92%");
    expect(root.querySelector(".chip.human")!.textContent).toBe("a human will follow up");

    const messageCall = fetchMock.mock.calls.find(([u]) => String(u).endsWith("/messages"))!;
    expect(String(messageCall[0])).toBe("https://api.test/chat/sessions/s1/messages");
    expect(JSON.parse((messageCall[1] as RequestInit).body as string)).toEqual({
      body: "Where is my order?",
    });

    const handoffBtn = root.querySelector<HTMLButtonElement>(".handoff")!;
    expect(handoffBtn.textContent).toBe("Talk to a human");
    handoffBtn.click();
    await vi.waitFor(() => {
      expect(handoffBtn.textContent).toBe("We've alerted our team ✓");
    });
    expect(
      fetchMock.mock.calls.some(([u]) => String(u).endsWith("/chat/sessions/s1/handoff")),
    ).toBe(true);
  });

  it("resumes a persisted session without creating a new one", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.includes("/chat/config")) return jsonResponse(CONFIG);
      if (method === "GET" && url.includes("/chat/sessions/s1"))
        return jsonResponse({
          session_id: "s1",
          history: [{ role: "customer", content: "Previous question" }],
        });
      return jsonResponse({ error: "not found" }, 404);
    });

    const { root } = await start(fetchMock, memoryStorage({ cschat_session_v1: "s1" }));
    await vi.waitFor(() => {
      expect(root.querySelector(".msg.customer")!.textContent).toBe("Previous question");
    });
    expect(
      fetchMock.mock.calls.some(([u, i]) => String(u).endsWith("/chat/sessions") && i?.method === "POST"),
    ).toBe(false);
  });

  it("shows an error bubble when the stream yields no message event", async () => {
    const sse = 'event: stage\ndata: {"stage":"classify_ticket"}\n\n';
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.includes("/chat/config")) return jsonResponse(CONFIG);
      if (method === "POST" && url.endsWith("/chat/sessions"))
        return jsonResponse({ session_id: "s1", history: [] });
      if (method === "GET" && url.includes("/chat/sessions/s1"))
        return jsonResponse({ session_id: "s1", history: [] });
      if (url.endsWith("/messages")) return new Response(sse);
      return jsonResponse({ error: "not found" }, 404);
    });

    const { root } = await start(fetchMock);
    submit(root, "hello");
    await vi.waitFor(() => {
      expect(root.querySelector(".msg.error")!.textContent).toBe(
        "No reply came through. Please try again.",
      );
    });
    expect(root.querySelector(".typing")).toBeNull();
  });

  it("shows an error bubble when the network fails during send", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.includes("/chat/config")) return jsonResponse(CONFIG);
      if (method === "POST" && url.endsWith("/chat/sessions"))
        return jsonResponse({ session_id: "s1", history: [] });
      if (method === "GET" && url.includes("/chat/sessions/s1"))
        return jsonResponse({ session_id: "s1", history: [] });
      if (url.endsWith("/messages")) throw new Error("network down");
      return jsonResponse({ error: "not found" }, 404);
    });

    const { root } = await start(fetchMock);
    submit(root, "hello");
    await vi.waitFor(() => {
      expect(root.querySelector(".msg.error")!.textContent).toBe(
        "We lost connection to support. Please try again.",
      );
    });
    expect(root.querySelector(".typing")).toBeNull();
  });

  it("closes the panel on Escape", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(CONFIG));
    await initWidget({
      apiKey: "test-key",
      apiBase: "",
      fetchImpl: fetchMock as unknown as typeof fetch,
      storage: memoryStorage(),
    });
    const root = shadow();
    const launcher = root.querySelector<HTMLButtonElement>(".launcher")!;
    launcher.click();
    expect(root.querySelector(".panel")!.classList.contains("open")).toBe(true);

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(root.querySelector(".panel")!.classList.contains("open")).toBe(false);
    expect(launcher.getAttribute("aria-expanded")).toBe("false");
  });
});
