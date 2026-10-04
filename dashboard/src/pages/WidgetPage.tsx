import { useEffect, useState } from "react";
import { MessageSquare, Copy, RefreshCw, Check, AlertCircle } from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";
import type { WidgetConfig } from "../lib/types";
import { useToast } from "../components/Toast";

const input =
  "w-full rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-2.5 py-2 text-sm text-ink-900 dark:text-ink-dark-900 placeholder:text-ink-400 dark:placeholder:text-ink-dark-400 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20";
const label = "block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1";

export function WidgetPage({ connection }: { connection: Connection }) {
  const [config, setConfig] = useState<WidgetConfig | null>(null);
  const [widgetKey, setWidgetKey] = useState("");
  const [draft, setDraft] = useState<WidgetConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [rotating, setRotating] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const { toast } = useToast();

  useEffect(() => {
    api
      .getWidgetSettings(connection)
      .then((s) => {
        setConfig(s.config);
        setDraft(s.config);
        setWidgetKey(s.key);
      })
      .catch(() => setError("Could not load widget settings."));
  }, [connection]);

  function patch(p: Partial<WidgetConfig>) {
    setDraft((d) => (d ? { ...d, ...p } : d));
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!draft) return;
    setSaving(true);
    setError("");
    try {
      const res = await api.updateWidgetConfig(connection, draft);
      setConfig(res.config);
      setDraft(res.config);
      toast("success", "Widget appearance saved — live on the storefront immediately.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save widget settings.");
    } finally {
      setSaving(false);
    }
  }

  async function rotateKey() {
    if (!window.confirm("Rotate the widget key? The storefront snippet must be re-copied, or the widget stops opening sessions.")) return;
    setRotating(true);
    try {
      const res = await api.rotateWidgetKey(connection);
      setWidgetKey(res.key);
      toast("success", "Key rotated — re-copy the install snippet below.");
    } catch (err) {
      toast("error", err instanceof Error ? err.message : "Could not rotate the key.");
    } finally {
      setRotating(false);
    }
  }

  const apiBase = connection.baseUrl.replace(/\/$/, "");
  const snippet = `<script src="${apiBase}/chat/widget.js" data-key="${widgetKey}" async></script>`;

  async function copySnippet() {
    try {
      await navigator.clipboard.writeText(snippet);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast("error", "Clipboard unavailable — select the snippet and copy manually.");
    }
  }

  if (!config || !draft) {
    return (
      <div className="max-w-3xl">
        <p className="text-sm text-ink-400 dark:text-ink-dark-400">{error || "Loading widget settings…"}</p>
      </div>
    );
  }

  return (
    <div className="max-w-3xl">
      <header className="mb-6">
        <p className="font-mono text-[11px] tracking-[0.18em] uppercase text-ink-400 dark:text-ink-dark-400 mb-1">Storefront</p>
        <h1 className="font-display text-3xl text-ink-900 dark:text-ink-dark-900">Chat widget</h1>
        <p className="text-sm text-ink-600 dark:text-ink-dark-600 mt-2 leading-relaxed">
          A native, embeddable chat bubble that streams replies through the same
          LangGraph pipeline as email — classification, knowledge grounding, confidence
          gating, human handoff. Zero third-party chat service.
        </p>
      </header>

      <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5 mb-6">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <MessageSquare className="w-5 h-5 text-teal" />
            <div>
              <p className="text-sm font-medium text-ink-900 dark:text-ink-dark-900">
                {config.enabled ? "Widget enabled" : "Widget disabled"}
              </p>
              <p className="text-xs text-ink-400 dark:text-ink-dark-400">
                Publishable key — safe to ship in the storefront <span className="font-mono">{widgetKey.slice(0, 10)}…</span>
              </p>
            </div>
          </div>
          <label className="flex items-center gap-2 text-xs text-ink-600 dark:text-ink-dark-600 cursor-pointer">
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={(e) => patch({ enabled: e.target.checked })}
              className="accent-teal"
            />
            Enabled
          </label>
        </div>
      </div>

      {error && (
        <div className="rounded-xl2 bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-sm px-4 py-3 mb-6 flex items-start gap-2.5">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <p>{error}</p>
        </div>
      )}

      <form onSubmit={save} className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5 mb-6 space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={label}>Widget title</label>
            <input value={draft.title} onChange={(e) => patch({ title: e.target.value })} maxLength={80} className={input} />
          </div>
          <div>
            <label className={label}>Brand color (hex)</label>
            <div className="flex gap-2">
              <input
                type="color"
                value={/^#[0-9a-fA-F]{6}$/.test(draft.color) ? draft.color : "#2E8C82"}
                onChange={(e) => patch({ color: e.target.value })}
                className="w-10 h-9 rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark cursor-pointer"
              />
              <input value={draft.color} onChange={(e) => patch({ color: e.target.value })} maxLength={7} className={`${input} flex-1 font-mono`} />
            </div>
          </div>
        </div>
        <div>
          <label className={label}>Greeting (shown when the bubble opens)</label>
          <input value={draft.greeting} onChange={(e) => patch({ greeting: e.target.value })} maxLength={200} className={input} />
        </div>
        <div>
          <label className={label}>Welcome message</label>
          <textarea value={draft.welcome_message} onChange={(e) => patch({ welcome_message: e.target.value })} maxLength={400} rows={2} className={`${input} resize-none`} />
        </div>
        <div>
          <label className={label}>Logo URL (optional)</label>
          <input value={draft.logo_url} onChange={(e) => patch({ logo_url: e.target.value })} placeholder="https://cdn.example.com/logo.png" maxLength={500} className={input} />
          <p className="text-[11px] text-ink-400 dark:text-ink-dark-400 mt-1">Must be an http(s) URL. Shown inside the chat header.</p>
        </div>
        <div className="flex items-center justify-between pt-1">
          <label className="flex items-center gap-2 text-xs text-ink-600 dark:text-ink-dark-600 cursor-pointer">
            <input
              type="checkbox"
              checked={draft.show_confidence}
              onChange={(e) => patch({ show_confidence: e.target.checked })}
              className="accent-teal"
            />
            Show reply confidence to customers
          </label>
          <button
            type="submit"
            disabled={saving || JSON.stringify(draft) === JSON.stringify(config)}
            className="text-xs bg-teal text-white rounded-lg px-4 py-2 font-medium hover:bg-teal-700 disabled:opacity-40 transition-colors"
          >
            {saving ? "Saving…" : "Save appearance"}
          </button>
        </div>
      </form>

      <section className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5 mb-6">
        <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-1">Install</h2>
        <p className="text-xs text-ink-600 dark:text-ink-dark-600 mb-3 leading-relaxed">
          Paste this before <span className="font-mono">&lt;/body&gt;</span> on your storefront. The widget loads as a
          single ~4 KB gzipped script — no iframe, no third party.
        </p>
        <div className="relative">
          <pre className="bg-bg dark:bg-bg-dark border border-line dark:border-line-dark rounded-lg px-3 py-3 pr-20 text-[11px] font-mono text-ink-700 dark:text-ink-dark-700 overflow-x-auto whitespace-pre-wrap break-all">
            {snippet}
          </pre>
          <button
            type="button"
            onClick={copySnippet}
            className="absolute top-2 right-2 flex items-center gap-1 text-[11px] bg-ink-900 dark:bg-white text-white dark:text-ink-900 rounded-md px-2 py-1 font-medium hover:bg-ink-700 dark:hover:bg-ink-dark-900 transition-colors"
          >
            {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        <div className="flex items-center justify-between mt-4 pt-4 border-t border-line dark:border-line-dark">
          <p className="text-[11px] text-ink-400 dark:text-ink-dark-400">
            Rotating invalidates the snippet above immediately.
          </p>
          <button
            type="button"
            onClick={rotateKey}
            disabled={rotating}
            className="flex items-center gap-1.5 text-xs border border-line dark:border-line-dark text-ink-600 dark:text-ink-dark-600 rounded-lg px-3 py-1.5 font-medium hover:bg-ink-900/5 dark:hover:bg-white/5 disabled:opacity-40 transition-colors"
          >
            <RefreshCw className="w-3.5 h-3.5" /> {rotating ? "Rotating…" : "Rotate key"}
          </button>
        </div>
      </section>
    </div>
  );
}
