import { useState } from "react";
import { Radio } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { Connection } from "../lib/api";

export function ConnectScreen({ onConnect }: { onConnect: (c: Connection) => void }) {
  const [baseUrl, setBaseUrl] = useState("http://localhost:8001");
  const [apiKey, setApiKey] = useState("");
  const [status, setStatus] = useState<"idle" | "checking" | "error">("idle");
  const [error, setError] = useState("");

  async function handleConnect(e: React.FormEvent) {
    e.preventDefault();
    setStatus("checking");
    setError("");
    const conn = { baseUrl: baseUrl.trim(), apiKey: apiKey.trim() };
    try {
      await api.health(conn);
      onConnect(conn);
    } catch (err) {
      setStatus("error");
      setError(err instanceof ApiError ? `${err.status}: ${err.message}` : "Couldn't reach that URL — check it's running and reachable.");
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-bg dark:bg-bg-dark px-6 transition-colors">
      <div className="w-full max-w-md">
        <div className="flex items-center gap-2.5 mb-8 justify-center">
          <Radio className="w-5 h-5 text-teal" strokeWidth={2.25} />
          <span className="font-mono text-xs tracking-[0.2em] uppercase text-ink-600 dark:text-ink-dark-600">Support Agent Console</span>
        </div>

        <div className="bg-surface dark:bg-surface-dark rounded-xl2 shadow-panel border border-line dark:border-line-dark p-8">
          <h1 className="font-display text-2xl text-ink-900 dark:text-ink-dark-900 mb-1">Connect to your agent</h1>
          <p className="text-sm text-ink-600 dark:text-ink-dark-600 mb-6">Point this console at your deployed cs-agent instance.</p>

          <form onSubmit={handleConnect} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-ink-600 dark:text-ink-dark-600 mb-1.5">API base URL</label>
              <input
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://your-instance.onrender.com"
                className="w-full rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-3 py-2.5 text-sm font-mono text-ink-900 dark:text-ink-dark-900 placeholder:text-ink-400 dark:placeholder:text-ink-dark-400 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-ink-600 dark:text-ink-dark-600 mb-1.5">API key</label>
              <input
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                type="password"
                placeholder="Your X-API-Key"
                className="w-full rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-3 py-2.5 text-sm font-mono text-ink-900 dark:text-ink-dark-900 placeholder:text-ink-400 dark:placeholder:text-ink-dark-400 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
              />
            </div>

            {status === "error" && (
              <div className="rounded-lg bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-sm px-3 py-2.5">
                Couldn't connect. {error}
              </div>
            )}

            <button
              type="submit"
              disabled={status === "checking" || !baseUrl || !apiKey}
              className="w-full bg-ink-900 dark:bg-white text-white dark:text-ink-900 rounded-lg py-2.5 text-sm font-medium hover:bg-ink-700 dark:hover:bg-ink-dark-900 disabled:opacity-40 transition-colors"
            >
              {status === "checking" ? "Connecting…" : "Connect"}
            </button>
          </form>
        </div>

        <p className="text-xs text-ink-400 dark:text-ink-dark-400 text-center mt-5">
          Stored only in this browser. Nothing is sent anywhere but your own backend.
        </p>
      </div>
    </div>
  );
}
