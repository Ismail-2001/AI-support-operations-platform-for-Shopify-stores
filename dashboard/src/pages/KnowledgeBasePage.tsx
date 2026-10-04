import { useEffect, useState } from "react";
import { BookOpen, Sparkles, Search, Plus, AlertCircle, PackageSearch, CheckCircle2 } from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";
import type { KbSyncStatus, LiveStockVariant, SetupTestResult } from "../lib/types";

export function KnowledgeBasePage({ connection }: { connection: Connection }) {
  const [chunkCount, setChunkCount] = useState<number | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState("");
  const [syncError, setSyncError] = useState(false);
  const [syncStatus, setSyncStatus] = useState<KbSyncStatus | null>(null);

  const [source, setSource] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [ingesting, setIngesting] = useState(false);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<{ source: string; title: string; content: string; score: number }[] | null>(null);
  const [searching, setSearching] = useState(false);

  // Test product knowledge tool
  const [testQuestion, setTestQuestion] = useState("");
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState("");
  const [testChunks, setTestChunks] = useState<{ source: string; title: string; content: string; score: number }[] | null>(null);
  const [testDraft, setTestDraft] = useState<SetupTestResult | null>(null);
  const [testStock, setTestStock] = useState<{ title: string; variants: LiveStockVariant[] } | null>(null);

  function refreshStatus() {
    api.kbStatus(connection).then((s) => setChunkCount(s.chunk_count));
  }
  useEffect(refreshStatus, []); // eslint-disable-line

  async function handleSync(force = false) {
    setSyncing(true);
    setSyncMessage("");
    setSyncError(false);
    setSyncStatus(null);
    try {
      const res = await api.kbSyncShopify(connection, { force, onProgress: setSyncStatus });
      setSyncMessage(
        `Synced — ${res.products_updated} products updated, ${res.products_skipped} unchanged, ` +
          `${res.policies_updated} policies, ${res.chunks_added} chunks added.`,
      );
      refreshStatus();
    } catch (err) {
      setSyncMessage(err instanceof Error ? err.message : "Sync failed — check Shopify and the Google API key.");
      setSyncError(true);
    } finally {
      setSyncing(false);
      setSyncStatus(null);
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

  async function runProductTest(e: React.FormEvent) {
    e.preventDefault();
    setTesting(true);
    setTestError("");
    setTestChunks(null);
    setTestDraft(null);
    setTestStock(null);
    try {
      const kb = await api.kbSearch(connection, testQuestion);
      setTestChunks(kb.results);
      const productHit = kb.results.find((r) => r.source.startsWith("product:"));
      const [draft, stock] = await Promise.all([
        api.setupTest(connection, testQuestion).catch(() => null),
        productHit
          ? api.kbLiveStock(connection, productHit.source.slice("product:".length)).catch(() => null)
          : Promise.resolve(null),
      ]);
      setTestDraft(draft);
      setTestStock(stock);
      if (!kb.results.length && !draft) setTestError("Nothing retrieved and no draft produced — is the knowledge base synced?");
    } catch (err) {
      setTestError(err instanceof Error ? err.message : "Test failed.");
    } finally {
      setTesting(false);
    }
  }

  const syncProgress = syncStatus
    ? `${syncStatus.products_seen} products seen · ${syncStatus.products_updated} updated · ${syncStatus.products_skipped} unchanged`
    : "";

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
        <div className="flex items-center gap-2">
          {!syncing && (
            <button
              onClick={() => handleSync(true)}
              className="text-xs border border-line dark:border-line-dark text-ink-500 dark:text-ink-dark-500 rounded-lg px-3 py-2 font-medium hover:bg-ink-900/5 dark:hover:bg-white/5 transition-colors"
              title="Ignore change detection and re-embed everything"
            >
              Re-sync all
            </button>
          )}
          <button
            onClick={() => handleSync()}
            disabled={syncing}
            className="flex items-center gap-1.5 text-xs bg-ink-900 dark:bg-white text-white dark:text-ink-900 rounded-lg px-3.5 py-2 font-medium hover:bg-ink-700 dark:hover:bg-ink-dark-900 disabled:opacity-40 transition-colors"
          >
            <Sparkles className="w-3.5 h-3.5" /> {syncing ? "Syncing…" : "Sync from Shopify"}
          </button>
        </div>
      </div>
      {syncing && syncProgress && (
        <p className="text-xs text-teal-700 dark:text-teal -mt-4 mb-6 font-mono">{syncProgress}…</p>
      )}
      {syncError && (
        <div className="rounded-xl2 bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-sm px-4 py-3 mb-6 flex items-start gap-2.5">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <div className="flex-1">
            <p>{syncMessage}</p>
            <button onClick={() => handleSync()} className="mt-2 text-xs font-medium underline hover:text-rose-900 dark:hover:text-rose transition-colors">
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
                <p className="font-mono text-[10px] text-ink-400 dark:text-ink-dark-400 mb-1.5">{r.source}</p>
                <p className="text-xs text-ink-600 dark:text-ink-dark-600 leading-relaxed">{r.content}</p>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="mt-8">
        <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-1 flex items-center gap-2">
          <PackageSearch className="w-4 h-4 text-teal" /> Test product knowledge
        </h2>
        <p className="text-xs text-ink-600 dark:text-ink-dark-600 mb-3 leading-relaxed">
          Ask a product question the way a customer would. Shows the retrieved chunks, the live
          Shopify inventory (real-time, not the sync-time snapshot), and the draft the full
          pipeline would produce.
        </p>
        <form onSubmit={runProductTest} className="flex gap-2 mb-4">
          <input
            value={testQuestion}
            onChange={(e) => setTestQuestion(e.target.value)}
            placeholder="Is the linen shirt machine washable, and is medium in stock?"
            className="flex-1 rounded-lg border border-line dark:border-line-dark bg-surface dark:bg-surface-dark px-3 py-2.5 text-sm text-ink-900 dark:text-ink-dark-900 placeholder:text-ink-400 dark:placeholder:text-ink-dark-400 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
          />
          <button type="submit" disabled={testing}
            className="flex items-center gap-1.5 text-xs bg-teal text-white rounded-lg px-4 py-2.5 font-medium hover:bg-teal-700 disabled:opacity-40 transition-colors">
            <PackageSearch className="w-3.5 h-3.5" /> {testing ? "Testing…" : "Test"}
          </button>
        </form>
        {testError && <p className="text-xs text-rose-700 dark:text-rose mb-3">{testError}</p>}

        {testChunks && (
          <div className="space-y-2 mb-4">
            {testChunks.length === 0 && (
              <p className="text-sm text-ink-400 dark:text-ink-dark-400">No chunks retrieved — sync the catalog first.</p>
            )}
            {testChunks.map((r, i) => (
              <div key={i} className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 p-4">
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-sm font-medium text-ink-900 dark:text-ink-dark-900">{r.title}</span>
                  <span className="font-mono text-[11px] text-teal-700 dark:text-teal">{Math.round(r.score * 100)}% match</span>
                </div>
                <p className="font-mono text-[10px] text-ink-400 dark:text-ink-dark-400 mb-1.5">{r.source}</p>
                <p className="text-xs text-ink-600 dark:text-ink-dark-600 leading-relaxed">{r.content}</p>
              </div>
            ))}
          </div>
        )}

        {testStock && (
          <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 p-4 mb-4">
            <p className="text-xs font-medium text-ink-900 dark:text-ink-dark-900 mb-2">
              Live inventory — {testStock.title}
            </p>
            <table className="w-full text-xs">
              <tbody>
                {testStock.variants.map((v, i) => (
                  <tr key={i} className="border-b border-line/50 dark:border-line-dark/50 last:border-0">
                    <td className="py-1.5 text-ink-700 dark:text-ink-dark-700">{v.variant}</td>
                    <td className="py-1.5 font-mono text-ink-500 dark:text-ink-dark-500 text-right">
                      {v.quantity == null ? "–" : v.quantity}
                    </td>
                    <td className="py-1.5 text-right">
                      <span
                        className={`font-mono text-[10px] px-1.5 py-0.5 rounded ${
                          v.status === "in_stock"
                            ? "bg-teal-100 text-teal-700 dark:bg-teal/20 dark:text-teal"
                            : v.status === "low_stock"
                              ? "bg-gold-100 text-gold-700 dark:bg-gold/20 dark:text-gold"
                              : "bg-rose-100 text-rose-700 dark:bg-rose/20 dark:text-rose"
                        }`}
                      >
                        {v.status.replace("_", " ")}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {testDraft && (
          <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 p-4">
            <div className="flex items-center gap-2 mb-2">
              <CheckCircle2 className="w-4 h-4 text-teal" />
              <p className="text-xs font-medium text-ink-900 dark:text-ink-dark-900">Draft the pipeline would produce</p>
              <span className="font-mono text-[11px] text-teal-700 dark:text-teal ml-auto">
                {testDraft.suggestion.confidence != null ? `${Math.round(testDraft.suggestion.confidence * 100)}% confidence` : ""}
              </span>
            </div>
            <p className="text-[11px] text-ink-400 dark:text-ink-dark-400 mb-2 font-mono">
              classified as {testDraft.classification.category} / {testDraft.classification.priority} priority
              {testDraft.kb_used ? " · grounded in knowledge base" : " · no KB match"}
            </p>
            <p className="text-sm text-ink-700 dark:text-ink-dark-700 leading-relaxed whitespace-pre-wrap">
              {testDraft.suggestion.suggested_response}
            </p>
            {testDraft.suggestion.requires_human_review && (
              <p className="text-[11px] text-gold-700 dark:text-gold mt-2">
                Flagged for human review before sending.
              </p>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
