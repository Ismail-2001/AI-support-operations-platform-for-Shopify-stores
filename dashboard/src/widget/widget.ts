/**
 * Embeddable storefront chat widget.
 *
 * Install: <script src="https://<api>/chat/widget.js" data-key="..." async></script>
 *
 * Design constraints:
 *  - Zero dependencies, vanilla TS -> esbuild IIFE, tiny (< 80KB gzipped by miles).
 *  - Shadow DOM + inline styles -> no style bleed either direction with the store's theme.
 *  - Fetch-based SSE reading (POST + streaming body) -> stage events render live.
 *  - Degrades gracefully: config fetch fails or backend down -> widget simply doesn't
 *    render / shows a retry — it must never break the storefront page itself.
 *  - Accessible: role=dialog, aria-live log, Esc closes, focus management, labeled controls.
 */

export interface WidgetOptions {
  apiKey: string;
  apiBase: string;
  email?: string;
  name?: string;
  orderNumber?: string;
  fetchImpl?: typeof fetch;
  storage?: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  mount?: ParentNode;
}

export interface WidgetConfig {
  enabled: boolean;
  title: string;
  greeting: string;
  welcome_message: string;
  color: string;
  logo_url: string;
  show_confidence: boolean;
}

export interface SSEEvent {
  event: string;
  data: Record<string, any>;
}

const SESSION_KEY = "cschat_session_v1";

export const STAGE_LABELS: Record<string, string> = {
  load_history: "Loading conversation…",
  classify_ticket: "Understanding your message…",
  apply_escalation: "Checking conversation history…",
  fetch_order_context: "Looking up your order…",
   fetch_knowledge_context: "Checking our help docs…",
   fetch_subscription_context: "Checking your subscription…",
  generate_response: "Writing a reply…",
  decide_auto_send: "Reviewing confidence…",
  save_results: "Saving…",
};

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] || "Working…";
}

/** Incremental SSE parser: feed decoded text chunks, get complete events back. */
export function createSSEParser(): (chunk: string) => SSEEvent[] {
  let buffer = "";
  return (chunk: string): SSEEvent[] => {
    buffer += chunk;
    const events: SSEEvent[] = [];
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = "message";
      const dataLines: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length === 0) continue;
      try {
        events.push({ event, data: JSON.parse(dataLines.join("\n")) });
      } catch {
        /* skip malformed block — never crash the widget */
      }
    }
    return events;
  };
}

function memoryStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  const map = new Map<string, string>();
  return {
    getItem: (k) => (map.has(k) ? map.get(k)! : null),
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

interface Bubble {
  el: HTMLElement;
}

export async function initWidget(opts: WidgetOptions): Promise<{ destroy: () => void } | null> {
  const doc = opts.mount?.ownerDocument ?? document;
  const store = opts.storage ?? safeLocalStorage();
  const fetchImpl =
    opts.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));
  const apiBase = opts.apiBase.replace(/\/$/, "");

  async function api(path: string, init?: RequestInit): Promise<Response> {
    return fetchImpl(`${apiBase}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        "X-Widget-Key": opts.apiKey,
        ...(init?.headers || {}),
      },
    });
  }

  // 1. Config gate — bad key / backend down / disabled => do not render at all.
  let config: WidgetConfig;
  try {
    const res = await api(`/chat/config?key=${encodeURIComponent(opts.apiKey)}`);
    if (!res.ok) return null;
    config = (await res.json()) as WidgetConfig;
    if (!config.enabled) return null;
  } catch {
    return null;
  }

  const brand = /^#[0-9a-fA-F]{6}$/.test(config.color) ? config.color : "#2E8C82";

  // 2. Session (persisted so a returning visitor resumes the same thread).
  let sessionId = store?.getItem(SESSION_KEY) || null;

  async function ensureSession(): Promise<string | null> {
    if (sessionId) {
      try {
        const res = await api(`/chat/sessions/${sessionId}`);
        if (res.ok) return sessionId;
        if (res.status === 404) {
          sessionId = null;
          store?.removeItem(SESSION_KEY);
        }
      } catch {
        return null; // backend unreachable — send() will surface a retry bubble
      }
    }
    try {
      const res = await api("/chat/sessions", {
        method: "POST",
        body: JSON.stringify({
          email: opts.email || null,
          name: opts.name || null,
          order_number: opts.orderNumber || null,
        }),
      });
      if (!res.ok) return null;
      const body = await res.json();
      sessionId = body.session_id;
      store?.setItem(SESSION_KEY, sessionId!);
      renderHistory(body.history || []);
      return sessionId;
    } catch {
      return null;
    }
  }

  async function loadHistory(): Promise<void> {
    if (!sessionId) return;
    try {
      const res = await api(`/chat/sessions/${sessionId}`);
      if (res.ok) {
        const body = await res.json();
        renderHistory(body.history || []);
      }
    } catch {
      /* history is best-effort */
    }
  }

  // ── DOM ──────────────────────────────────────────────────────
  const host = doc.createElement("div");
  host.setAttribute("data-cs-chat", "");
  const shadow = host.attachShadow({ mode: "open" });
  (opts.mount ?? doc.body).appendChild(host);

  const style = doc.createElement("style");
  style.textContent = `
    :host { all: initial; }
    * { box-sizing: border-box; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
    .launcher {
      position: fixed; right: 20px; bottom: 20px; z-index: 2147483000;
      width: 56px; height: 56px; border-radius: 50%; border: none; cursor: pointer;
      background: ${brand}; color: #fff; display: flex; align-items: center; justify-content: center;
      box-shadow: 0 6px 20px rgba(0,0,0,.25); transition: transform .15s ease;
    }
    .launcher:hover { transform: scale(1.06); }
    .launcher:focus-visible { outline: 3px solid #111; outline-offset: 2px; }
    .panel {
      position: fixed; right: 20px; bottom: 86px; z-index: 2147483001;
      width: 380px; max-width: calc(100vw - 32px); height: 560px; max-height: calc(100vh - 120px);
      background: #fff; color: #1a1f2b; border-radius: 16px; overflow: hidden;
      box-shadow: 0 12px 40px rgba(0,0,0,.28); display: none; flex-direction: column;
      border: 1px solid rgba(0,0,0,.08);
    }
    .panel.open { display: flex; }
    .header {
      background: ${brand}; color: #fff; padding: 14px 16px; display: flex; align-items: center; gap: 10px;
    }
    .header img { width: 28px; height: 28px; border-radius: 6px; object-fit: cover; background: #fff; }
    .header .title { font-weight: 600; font-size: 15px; flex: 1; }
    .close {
      background: transparent; border: none; color: #fff; cursor: pointer; font-size: 20px;
      line-height: 1; padding: 4px 6px; border-radius: 6px;
    }
    .close:hover { background: rgba(255,255,255,.18); }
    .close:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }
    .messages { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 8px; background: #f7f8fa; }
    .msg { max-width: 82%; padding: 9px 12px; border-radius: 14px; font-size: 14px; line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
    .msg.customer { align-self: flex-end; background: ${brand}; color: #fff; border-bottom-right-radius: 4px; }
    .msg.assistant, .msg.agent { align-self: flex-start; background: #fff; color: #1a1f2b; border: 1px solid rgba(0,0,0,.07); border-bottom-left-radius: 4px; }
    .msg.system { align-self: center; background: transparent; color: #5b6675; font-size: 12.5px; text-align: center; max-width: 92%; border: none; }
    .msg.error { align-self: center; background: #fdecec; color: #9b2c2c; font-size: 13px; }
    .meta { display: flex; gap: 6px; align-items: center; margin-top: 6px; flex-wrap: wrap; }
    .chip { font-size: 10.5px; padding: 2px 7px; border-radius: 999px; background: #eef1f5; color: #5b6675; border: 1px solid rgba(0,0,0,.06); }
    .chip.human { background: #fff6e5; color: #8a6116; border-color: #f0dfb5; }
    .handoff {
      margin-top: 8px; font-size: 13px; padding: 7px 12px; border-radius: 10px; cursor: pointer;
      background: #fff; color: ${brand}; border: 1px solid ${brand}; font-weight: 600;
    }
    .handoff:hover { background: ${brand}14; }
    .handoff:focus-visible { outline: 2px solid ${brand}; outline-offset: 2px; }
    .typing { align-self: flex-start; display: flex; gap: 5px; align-items: center; padding: 10px 12px; background: #fff; border: 1px solid rgba(0,0,0,.07); border-radius: 14px; font-size: 13px; color: #5b6675; }
    .typing .dots { display: inline-flex; gap: 3px; }
    .typing .dots i { width: 5px; height: 5px; border-radius: 50%; background: #9aa6b5; animation: blink 1.2s infinite; }
    .typing .dots i:nth-child(2) { animation-delay: .2s; }
    .typing .dots i:nth-child(3) { animation-delay: .4s; }
    @keyframes blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }
    .composer { display: flex; gap: 8px; padding: 10px; border-top: 1px solid rgba(0,0,0,.08); background: #fff; }
    .composer input {
      flex: 1; border: 1px solid rgba(0,0,0,.14); border-radius: 10px; padding: 10px 12px; font-size: 14px;
      outline: none; background: #fff; color: #1a1f2b;
    }
    .composer input:focus { border-color: ${brand}; box-shadow: 0 0 0 3px ${brand}33; }
    .composer button {
      border: none; background: ${brand}; color: #fff; border-radius: 10px; padding: 0 16px; cursor: pointer; font-weight: 600; font-size: 14px;
    }
    .composer button:disabled { opacity: .5; cursor: default; }
    .composer button:focus-visible { outline: 2px solid #111; outline-offset: 2px; }
    @media (max-width: 480px) {
      .panel { right: 0; bottom: 0; left: 0; top: 0; width: 100vw; max-width: 100vw; height: 100dvh; max-height: 100dvh; border-radius: 0; }
      .launcher { right: 14px; bottom: 14px; }
    }
    .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `;
  shadow.appendChild(style);

  const launcher = doc.createElement("button");
  launcher.className = "launcher";
  launcher.setAttribute("aria-label", `Open ${config.title}`);
  launcher.setAttribute("aria-expanded", "false");
  launcher.innerHTML =
    '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>';

  const panel = doc.createElement("div");
  panel.className = "panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-label", config.title);

  const header = doc.createElement("div");
  header.className = "header";
  if (config.logo_url) {
    const img = doc.createElement("img");
    img.src = config.logo_url;
    img.alt = "";
    header.appendChild(img);
  }
  const title = doc.createElement("span");
  title.className = "title";
  title.textContent = config.title;
  header.appendChild(title);
  const closeBtn = doc.createElement("button");
  closeBtn.className = "close";
  closeBtn.setAttribute("aria-label", "Close chat");
  closeBtn.textContent = "×";
  header.appendChild(closeBtn);

  const messages = doc.createElement("div");
  messages.className = "messages";
  messages.setAttribute("role", "log");
  messages.setAttribute("aria-live", "polite");
  messages.setAttribute("aria-label", "Chat messages");

  const composer = doc.createElement("form");
  composer.className = "composer";
  composer.setAttribute("aria-label", "Send a message");
  const input = doc.createElement("input");
  input.type = "text";
  input.placeholder = "Type a message…";
  input.setAttribute("aria-label", "Message");
  input.maxLength = 4000;
  const sendBtn = doc.createElement("button");
  sendBtn.type = "submit";
  sendBtn.textContent = "Send";
  composer.appendChild(input);
  composer.appendChild(sendBtn);

  panel.appendChild(header);
  panel.appendChild(messages);
  panel.appendChild(composer);
  shadow.appendChild(panel);
  shadow.appendChild(launcher);

  // ── Behavior ─────────────────────────────────────────────────
  let open = false;
  let busy = false;

  function setOpen(next: boolean): void {
    open = next;
    panel.classList.toggle("open", open);
    launcher.setAttribute("aria-expanded", String(open));
    if (open) {
      void ensureSession();
      input.focus();
    } else {
      launcher.focus();
    }
  }

  launcher.addEventListener("click", () => setOpen(!open));
  closeBtn.addEventListener("click", () => setOpen(false));
  doc.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Escape" && open) setOpen(false);
  });

  function scrollDown(): void {
    messages.scrollTop = messages.scrollHeight;
  }

  function addBubble(role: "customer" | "assistant" | "agent" | "system" | "error", text: string): Bubble {
    const el = doc.createElement("div");
    el.className = `msg ${role}`;
    el.textContent = text;
    messages.appendChild(el);
    scrollDown();
    return { el };
  }

  function renderHistory(history: Array<{ role: string; content: string }>): void {
    messages.innerHTML = "";
    addBubble("system", config.welcome_message);
    for (const m of history) {
      if (m.role === "customer") addBubble("customer", m.content);
      else if (m.role === "agent") addBubble("agent", m.content);
      else if (m.role === "assistant") addBubble("assistant", m.content);
    }
    scrollDown();
  }

  function addTyping(label: string): { update: (l: string) => void; remove: () => void } {
    const el = doc.createElement("div");
    el.className = "typing";
    const text = doc.createElement("span");
    text.textContent = label;
    const dots = doc.createElement("span");
    dots.className = "dots";
    dots.innerHTML = "<i></i><i></i><i></i>";
    el.appendChild(dots);
    el.appendChild(text);
    messages.appendChild(el);
    scrollDown();
    return {
      update: (l: string) => {
        text.textContent = l;
        scrollDown();
      },
      remove: () => el.remove(),
    };
  }

  /** Reveal long replies progressively (the backend sends the full text at once). */
  function revealText(el: HTMLElement, full: string): Promise<void> {
    return new Promise((resolve) => {
      const total = full.length;
      if (total <= 60) {
        el.textContent = full;
        scrollDown();
        resolve();
        return;
      }
      const step = Math.max(2, Math.ceil(total / 60));
      let i = 0;
      const tick = () => {
        i = Math.min(total, i + step);
        el.textContent = full.slice(0, i);
        scrollDown();
        if (i < total) requestAnimationFrame(tick);
        else resolve();
      };
      requestAnimationFrame(tick);
    });
  }

  function attachMessageMeta(el: HTMLElement, payload: Record<string, any>): void {
    const meta = doc.createElement("div");
    meta.className = "meta";
    if (payload.show_confidence && typeof payload.confidence === "number") {
      const chip = doc.createElement("span");
      chip.className = "chip";
      chip.textContent = `confidence ${Math.round(payload.confidence * 100)}%`;
      meta.appendChild(chip);
    }
    if (payload.needs_human) {
      const chip = doc.createElement("span");
      chip.className = "chip human";
      chip.textContent = "a human will follow up";
      meta.appendChild(chip);
      const btn = doc.createElement("button");
      btn.className = "handoff";
      btn.type = "button";
      btn.textContent = "Talk to a human";
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.textContent = "Connecting…";
        try {
          const res = await api(`/chat/sessions/${sessionId}/handoff`, {
            method: "POST",
            body: JSON.stringify({ reason: "customer_requested" }),
          });
          if (res.ok) {
            btn.textContent = "We've alerted our team ✓";
          } else {
            btn.disabled = false;
            btn.textContent = "Talk to a human";
          }
        } catch {
          btn.disabled = false;
          btn.textContent = "Talk to a human";
        }
      });
      meta.appendChild(btn);
    }
    if (meta.childElementCount > 0) {
      el.appendChild(meta);
      scrollDown();
    }
  }

  async function sendMessage(text: string): Promise<void> {
    if (busy) return;
    const sid = await ensureSession();
    if (!sid) {
      addBubble("error", "We can't reach support right now. Please try again in a moment.");
      return;
    }
    busy = true;
    sendBtn.disabled = true;
    addBubble("customer", text);
    const typing = addTyping(stageLabel("classify_ticket"));

    try {
      const res = await api(`/chat/sessions/${sid}/messages`, {
        method: "POST",
        body: JSON.stringify({ body: text }),
      });
      if (!res.ok || !res.body) {
        throw new Error(`HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      const parse = createSSEParser();
      let handled = false;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        for (const evt of parse(decoder.decode(value, { stream: true }))) {
          if (evt.event === "stage") {
            typing.update(String(evt.data.label || stageLabel(evt.data.stage)));
          } else if (evt.event === "message") {
            typing.remove();
            handled = true;
            const data = evt.data as Record<string, any>;
            const bubble = addBubble("assistant", "");
            // Never surface an unreviewed draft to the storefront: when a human
            // should follow up, show a holding line instead — the draft itself
            // stays in the dashboard for the agent to review and send.
            const needsHuman = Boolean(data.needs_human);
            const shown = needsHuman
              ? "Thanks! A member of our team will follow up with you shortly."
              : String(data.content ?? "");
            await revealText(bubble.el, shown);
            attachMessageMeta(
              bubble.el,
              needsHuman ? { ...data, show_confidence: false } : data
            );
          } else if (evt.event === "error") {
            typing.remove();
            addBubble("error", String(evt.data.message || "Something went wrong. Please try again."));
            handled = true;
          }
        }
      }
      if (!handled) {
        typing.remove();
        addBubble("error", "No reply came through. Please try again.");
      }
    } catch {
      typing.remove();
      addBubble("error", "We lost connection to support. Please try again.");
    } finally {
      busy = false;
      sendBtn.disabled = false;
      input.focus();
      scrollDown();
    }
  }

  composer.addEventListener("submit", (e: Event) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    input.value = "";
    void sendMessage(text);
  });

  renderHistory([]);
  void loadHistory();

  return {
    destroy(): void {
      host.remove();
    },
  };
}

function safeLocalStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  try {
    const ls = globalThis.localStorage;
    const probe = "__cschat_probe__";
    ls.setItem(probe, "1");
    ls.removeItem(probe);
    return ls;
  } catch {
    return memoryStorage();
  }
}

function bootstrap(): void {
  const script = document.currentScript as HTMLScriptElement | null;
  if (!script) return;
  const key = script.dataset.key;
  if (!key) {
    console.warn("[cs-chat] missing data-key — widget not started");
    return;
  }
  let apiBase = script.dataset.api || "";
  if (!apiBase) {
    try {
      apiBase = new URL(script.src, window.location.href).origin;
    } catch {
      apiBase = window.location.origin;
    }
  }
  void initWidget({
    apiKey: key,
    apiBase,
    email: script.dataset.email,
    name: script.dataset.name,
    orderNumber: script.dataset.order,
  });
}

if (typeof document !== "undefined") {
  bootstrap();
}
