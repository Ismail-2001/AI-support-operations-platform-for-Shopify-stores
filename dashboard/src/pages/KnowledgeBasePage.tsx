import { useEffect, useState } from "react";
import { BookOpen, Sparkles, Search, Plus, AlertCircle } from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";

export function KnowledgeBasePage({ connection }: { connection: Connection }) {
  const [chunkCount, setChunkCount] = useState<number | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState("");
  const [syncError, setSyncError] = useState(false);

  const [source, setSource] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [ingesting, setIngesting] = useState(false);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<{ source: string; title: string; content: string; score: number }[] | null>(null);
  const [searching, setSearching] = useState(false);

  function refreshStatus() {
    api.kbStatus(connection).then((s) => setChunkCount(s.chunk_count));
  }
  useEffect(refreshStatus, []); // eslint-disable-line

  async function handleSync() {
    setSyncing(true);
    setSyncMessage("");
    setSyncError(false);
    try {
      const res = await api.kbSyncShopify(connection);
      setSyncMessage(`Synced — ${res.total_chunks} chunks ingested from Shopify.`);
      refreshStatus();
    } catch {
      setSyncMessage("Sync failed — check Shopify is connected in the sidebar status.");
      setSyncError(true);
    } finally {
      setSyncing(false);
    }
  }

  async function handleIngest(e: React.FormEvent) {
    e.preventDefault();
    setIngesting(true);
    try {
      await api.kbIngest(connection, source, title, content);
      setSource(""); setTitle(""); setContent("");
      refreshStatus();
    } finally {
      setIngesting(false);
    }
  }

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    setSearching(true);
    try {
      const res = await api.kbSearch(connection, query);
      setResults(res.results);
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className="max-w-3xl">
      <header className="mb-6">
        <p className="font-mono text-[11px] tracking-[0.18em] uppercase text-ink-400 dark:text-ink-dark-400 mb-1">Grounding</p>
        <h1 className="font-display text-3xl text-ink-900 dark:text-ink-dark-900">Knowledge base</h1>
      </header>

      <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5 mb-6 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <BookOpen className="w-5 h-5 text-teal" />
          <div>
            <p className="text-sm font-medium text-ink-900 dark:text-ink-dark-900">{chunkCount ?? "–"} chunks indexed</p>
            <p className="text-xs text-ink-400 dark:text-ink-dark-400">
              These are the only sources the agent can use — every reply stays grounded in this content.
            </p>
          </div>
        </div>
        <button
          onClick={handleSync}
          disabled={syncing}
          className="flex items-center gap-1.5 text-xs bg-ink-900 dark:bg-white text-white dark:text-ink-900 rounded-lg px-3.5 py-2 font-medium hover:bg-ink-700 dark:hover:bg-ink-dark-900 disabled:opacity-40 transition-colors"
        >
          <Sparkles className="w-3.5 h-3.5" /> {syncing ? "Syncing…" : "Sync from Shopify"}
        </button>
      </div>
      {syncError && (
        <div className="rounded-xl2 bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-sm px-4 py-3 mb-6 flex items-start gap-2.5">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <div className="flex-1">
            <p>{syncMessage}</p>
            <button onClick={handleSync} className="mt-2 text-xs font-medium underline hover:text-rose-900 dark:hover:text-rose transition-colors">
              Try again
            </button>
          </div>
        </div>
      )}
      {!syncError && syncMessage && <p className="text-xs text-ink-600 dark:text-ink-dark-600 -mt-4 mb-6">{syncMessage}</p>}

      <section className="mb-8">
        <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-3">Add content manually</h2>
        <form onSubmit={handleIngest} className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5 space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Source id</label>
              <input value={source} onChange={(e) => setSource(e.target.value)} placeholder="faq:sizing"
                className="w-full rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-2.5 py-2 text-sm font-mono text-ink-900 dark:text-ink-dark-900 placeholder:text-ink-400 dark:placeholder:text-ink-dark-400 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20" required />
            </div>
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Title</label>
              <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Sizing guide"
                className="w-full rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-2.5 py-2 text-sm text-ink-900 dark:text-ink-dark-900 placeholder:text-ink-400 dark:placeholder:text-ink-dark-400 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20" required />
            </div>
          </div>
          <div>
            <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Content</label>
            <textarea value={content} onChange={(e) => setContent(e.target.value)} rows={4}
              className="w-full rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-2.5 py-2 text-sm text-ink-900 dark:text-ink-dark-900 placeholder:text-ink-400 dark:placeholder:text-ink-dark-400 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20 resize-none" required />
          </div>
          <button type="submit" disabled={ingesting}
            className="flex items-center gap-1.5 text-xs bg-teal text-white rounded-lg px-3.5 py-2 font-medium hover:bg-teal-700 disabled:opacity-40 transition-colors">
            <Plus className="w-3.5 h-3.5" /> {ingesting ? "Adding…" : "Add to knowledge base"}
          </button>
        </form>
      </section>

      <section>
        <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-3">Test retrieval</h2>
        <form onSubmit={handleSearch} className="flex gap-2 mb-4">
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Is the hoodie waterproof?"
            className="flex-1 rounded-lg border border-line dark:border-line-dark bg-surface dark:bg-surface-dark px-3 py-2.5 text-sm text-ink-900 dark:text-ink-dark-900 placeholder:text-ink-400 dark:placeholder:text-ink-dark-400 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20" />
          <button type="submit" disabled={searching}
            className="flex items-center gap-1.5 text-xs bg-ink-900 dark:bg-white text-white dark:text-ink-900 rounded-lg px-4 py-2.5 font-medium hover:bg-ink-700 dark:hover:bg-ink-dark-900 disabled:opacity-40 transition-colors">
            <Search className="w-3.5 h-3.5" /> Search
          </button>
        </form>
        {results && (
          <div className="space-y-2">
            {results.length === 0 && <p className="text-sm text-ink-400 dark:text-ink-dark-400">No matches above the similarity threshold.</p>}
            {results.map((r, i) => (
              <div key={i} className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 p-4">
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-sm font-medium text-ink-900 dark:text-ink-dark-900">{r.title}</span>
                  <span className="font-mono text-[11px] text-teal-700 dark:text-teal">{Math.round(r.score * 100)}% match</span>
                </div>
                <p className="text-xs text-ink-600 dark:text-ink-dark-600 leading-relaxed">{r.content}</p>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
