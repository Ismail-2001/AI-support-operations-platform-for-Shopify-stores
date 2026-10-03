import { useEffect, useState } from "react";
import {
  AlertCircle, ArrowLeft, ArrowRight, BookOpen, Check, Eye, Link2, Loader2,
  MessageSquareText, Music, Package, ShoppingBag, Sparkles,
} from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";
import type { BrandVoice, SetupStatus, SetupTestResult } from "../lib/types";
import type { View } from "../components/Sidebar";
import { ConfidenceBar } from "../components/ConfidenceBar";

const STEPS = [
  { id: "shopify", label: "Store", icon: Link2, blurb: "Connect the Shopify store" },
  { id: "policies", label: "Knowledge", icon: Package, blurb: "Import policies & products" },
  { id: "voice", label: "Brand voice", icon: Music, blurb: "How replies should sound" },
  { id: "test", label: "Test", icon: MessageSquareText, blurb: "Ask a question, get a draft" },
] as const;

const SAMPLE_QUESTIONS = [
  "Hi, I ordered last week and still no tracking number — where is my order?",
  "What is your return policy? The jacket I bought doesn't fit.",
  "Can I change the shipping address on my order?",
];

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-6">
      {children}
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-xl2 bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-sm px-4 py-3 mb-5 flex items-start gap-2.5">
      <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
      <p className="flex-1">{message}</p>
    </div>
  );
}

const inputCls =
  "w-full rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-3 py-2.5 text-sm text-ink-900 dark:text-ink-dark-900 placeholder:text-ink-400 dark:placeholder:text-ink-dark-400 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20";
const primaryBtn =
  "flex items-center gap-1.5 text-sm bg-teal text-white rounded-lg px-4 py-2.5 font-medium hover:bg-teal-700 disabled:opacity-40 transition-colors";
const secondaryBtn =
  "flex items-center gap-1.5 text-sm border border-line dark:border-line-dark text-ink-700 dark:text-ink-dark-700 rounded-lg px-4 py-2.5 font-medium hover:bg-bg dark:hover:bg-bg-dark transition-colors";

export function SetupPage({ connection, onNavigate }: { connection: Connection; onNavigate: (v: View) => void }) {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [step, setStep] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // Step 1 — store
  const [shopDomain, setShopDomain] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [shopInfo, setShopInfo] = useState<{ shop_name: string | null; domain: string } | null>(null);

  // Step 2 — knowledge
  const [syncMsg, setSyncMsg] = useState("");

  // Step 3 — voice
  const [voice, setVoice] = useState<BrandVoice>({
    store_name: "", tone: "friendly", sign_off: "", support_email: "",
  });

  // Step 4 — test
  const [question, setQuestion] = useState("");
  const [testResult, setTestResult] = useState<SetupTestResult | null>(null);

  function refresh() {
    api.setupStatus(connection).then((s) => {
      setStatus(s);
      setVoice((v) => (v.store_name || v.sign_off || v.support_email ? v : s.voice));
    }).catch(() => setStatus(null));
  }
  useEffect(() => {
    api.setupStatus(connection).then((s) => {
      setStatus(s);
      setVoice(s.voice);
      setShopDomain(s.shopify.domain ?? "");
      const firstIncomplete = ["shopify", "policies", "voice", "test"].findIndex(
        (k) => !s.steps[k as keyof typeof s.steps]
      );
      setStep(firstIncomplete === -1 ? 3 : firstIncomplete);
    }).catch(() => setStatus(null));
    // eslint-disable-line react-hooks/exhaustive-deps
  }, [connection]);

  const shopifyReady = status?.shopify.connected ?? false;
  const chunkCount = status?.knowledge_base.chunk_count ?? 0;

  async function connectStore(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      const res = await api.setupShopify(connection, shopDomain, accessToken);
      setShopInfo({ shop_name: res.shop_name, domain: res.domain });
      setShopDomain(res.domain);
      refresh();
      setStep(1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not connect — check the domain and token.");
    } finally {
      setBusy(false);
    }
  }

  async function importPolicies() {
    setBusy(true); setError(""); setSyncMsg("");
    try {
      const res = await api.kbSyncShopify(connection);
      setSyncMsg(`Imported ${res.total_chunks} chunks — policies, products, everything the agent can cite.`);
      refresh();
    } catch {
      setError("Import failed — make sure the store is connected in step 1 and the Google API key is set.");
    } finally {
      setBusy(false);
    }
  }

  async function saveVoice(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      await api.setupVoice(connection, voice);
      refresh();
      setStep(3);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save brand voice.");
    } finally {
      setBusy(false);
    }
  }

  async function runTest(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      const res = await api.setupTest(connection, question);
      setTestResult(res);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Test failed — is the LLM provider configured?");
    } finally {
      setBusy(false);
    }
  }

  if (!status) {
    return (
      <div className="flex items-center gap-2 text-sm text-ink-400 dark:text-ink-dark-400 mt-10">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading setup status…
      </div>
    );
  }

  return (
    <div className="max-w-3xl">
      <header className="mb-6">
        <p className="font-mono text-[11px] tracking-[0.18em] uppercase text-ink-400 dark:text-ink-dark-400 mb-1">Onboarding</p>
        <h1 className="font-display text-3xl text-ink-900 dark:text-ink-dark-900">Set up your store</h1>
        <p className="text-sm text-ink-600 dark:text-ink-dark-600 mt-1.5">
          Four steps — connect, import knowledge, set the voice, and see a live draft.
        </p>
      </header>

      {error && <ErrorBanner message={error} />}

      {/* Step indicator */}
      <div className="grid grid-cols-4 gap-2 mb-6">
        {STEPS.map((s, i) => {
          const done = status.steps[s.id];
          const active = i === step;
          return (
            <button
              key={s.id}
              onClick={() => setStep(i)}
              className={`text-left rounded-xl border px-3 py-2.5 transition-colors ${
                active
                  ? "border-teal bg-teal/5 dark:bg-teal/10"
                  : "border-line dark:border-line-dark hover:border-ink-400 dark:hover:border-ink-dark-400"
              }`}
            >
              <span className="flex items-center gap-1.5">
                {done ? (
                  <Check className="w-3.5 h-3.5 text-teal shrink-0" />
                ) : (
                  <s.icon className="w-3.5 h-3.5 text-ink-400 dark:text-ink-dark-400 shrink-0" />
                )}
                <span className={`text-xs font-medium ${active ? "text-teal-700 dark:text-teal" : "text-ink-700 dark:text-ink-dark-700"}`}>
                  {i + 1}. {s.label}
                </span>
              </span>
              <span className="block text-[11px] text-ink-400 dark:text-ink-dark-400 mt-0.5 truncate">{s.blurb}</span>
            </button>
          );
        })}
      </div>

      {/* Step 1 — Shopify */}
      {step === 0 && (
        <Card>
          <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-1">Connect Shopify</h2>
          <p className="text-sm text-ink-600 dark:text-ink-dark-600 mb-4">
            The agent reads real orders from your store — never guesses. Create a custom app in
            your Shopify admin (<span className="font-mono text-xs">Settings → Apps → Develop apps</span>) with
            the <span className="font-mono text-xs">read_orders</span> scope and paste its Admin API token.
          </p>
          {shopifyReady && (
            <div className="rounded-lg bg-teal-50 dark:bg-teal/10 text-teal-700 dark:text-teal text-sm px-3.5 py-2.5 mb-4 flex items-center gap-2">
              <Check className="w-4 h-4" />
              Connected{status.shopify.domain ? ` — ${status.shopify.domain}` : ""}
              {shopInfo?.shop_name ? ` (${shopInfo.shop_name})` : ""}
            </div>
          )}
          <form onSubmit={connectStore} className="space-y-3">
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Shop domain</label>
              <input value={shopDomain} onChange={(e) => setShopDomain(e.target.value)}
                placeholder="your-store.myshopify.com" className={inputCls} required />
            </div>
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Admin API access token</label>
              <input type="password" value={accessToken} onChange={(e) => setAccessToken(e.target.value)}
                placeholder="shpat_…" className={inputCls} required />
            </div>
            <button type="submit" disabled={busy || !shopDomain.trim() || !accessToken.trim()} className={primaryBtn}>
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Link2 className="w-4 h-4" />}
              {busy ? "Validating…" : shopifyReady ? "Reconnect" : "Validate & connect"}
            </button>
          </form>
        </Card>
      )}

      {/* Step 2 — Knowledge */}
      {step === 1 && (
        <Card>
          <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-1">Import your knowledge</h2>
          <p className="text-sm text-ink-600 dark:text-ink-dark-600 mb-4">
            Pulls your store's policies (Settings → Policies) and product descriptions straight from
            Shopify, chunks them, and makes them citable in replies. You can add more later.
          </p>
          {!status.google_key_set && (
            <div className="rounded-lg bg-gold-100 dark:bg-gold/15 text-gold-700 dark:text-gold text-sm px-3.5 py-2.5 mb-4">
              GOOGLE_API_KEY isn't set — embeddings won't work, so import will fail. Add it to .env first.
            </div>
          )}
          <div className="flex items-center justify-between mb-4">
            <div>
              <p className="text-sm font-medium text-ink-900 dark:text-ink-dark-900">{chunkCount} chunks indexed</p>
              <p className="text-xs text-ink-400 dark:text-ink-dark-400">Policies, FAQs & products the agent can cite.</p>
            </div>
            <button onClick={importPolicies} disabled={busy || !shopifyReady} className={primaryBtn}>
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              {busy ? "Importing…" : "Import from Shopify"}
            </button>
          </div>
          {syncMsg && <p className="text-xs text-teal-700 dark:text-teal mb-4">{syncMsg}</p>}
          {!shopifyReady && (
            <p className="text-xs text-ink-400 dark:text-ink-dark-400 mb-4">Finish step 1 to connect the store first.</p>
          )}
          <p className="text-xs text-ink-600 dark:text-ink-dark-600">
            Have FAQs the store doesn't have in Shopify?{" "}
            <button onClick={() => onNavigate("knowledge-base")} className="text-teal hover:underline font-medium">
              Add them in Knowledge base →
            </button>
          </p>
        </Card>
      )}

      {/* Step 3 — Voice */}
      {step === 2 && (
        <Card>
          <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-1">Brand voice</h2>
          <p className="text-sm text-ink-600 dark:text-ink-dark-600 mb-4">
            Every draft reply is written in this voice — the customer should never be able to tell a
            human didn't write it.
          </p>
          <form onSubmit={saveVoice} className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Store name</label>
                <input value={voice.store_name} onChange={(e) => setVoice({ ...voice, store_name: e.target.value })}
                  placeholder="Northwind Supply" className={inputCls} />
              </div>
              <div>
                <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Tone</label>
                <select value={voice.tone} onChange={(e) => setVoice({ ...voice, tone: e.target.value as BrandVoice["tone"] })} className={inputCls}>
                  <option value="friendly">Friendly — warm, helpful small-business owner</option>
                  <option value="professional">Professional — polished, no slang</option>
                  <option value="casual">Casual — relaxed, conversational</option>
                </select>
              </div>
            </div>
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Sign-off</label>
              <input value={voice.sign_off} onChange={(e) => setVoice({ ...voice, sign_off: e.target.value })}
                placeholder="Thanks! — The Northwind team" className={inputCls} />
            </div>
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Support email (when a human is needed)</label>
              <input type="email" value={voice.support_email} onChange={(e) => setVoice({ ...voice, support_email: e.target.value })}
                placeholder="help@yourstore.com" className={inputCls} />
            </div>
            <div className="rounded-lg bg-bg dark:bg-bg-dark border border-line dark:border-line-dark px-4 py-3">
              <p className="text-[11px] font-mono uppercase tracking-wider text-ink-400 dark:text-ink-dark-400 mb-1">Every draft will end with</p>
              <p className="text-sm text-ink-800 dark:text-ink-dark-800">
                {voice.sign_off.trim() ||
                  (voice.store_name.trim() ? `Thanks! — The ${voice.store_name.trim()} team` : "Thanks! — The team")}
              </p>
            </div>
            <button type="submit" disabled={busy} className={primaryBtn}>
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
              Save voice
            </button>
          </form>
        </Card>
      )}

      {/* Step 4 — Test */}
      {step === 3 && (
        <Card>
          <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-1">Test it live</h2>
          <p className="text-sm text-ink-600 dark:text-ink-dark-600 mb-4">
            Ask what a real customer would ask. The agent classifies it, pulls real order/KB context,
            and drafts a reply — nothing is saved, this is a preview.
          </p>
          <div className="flex flex-wrap gap-2 mb-3">
            {SAMPLE_QUESTIONS.map((q) => (
              <button key={q} onClick={() => setQuestion(q)}
                className="text-xs text-ink-600 dark:text-ink-dark-600 border border-line dark:border-line-dark rounded-full px-3 py-1.5 hover:border-teal hover:text-teal transition-colors">
                {q.length > 52 ? q.slice(0, 52) + "…" : q}
              </button>
            ))}
          </div>
          <form onSubmit={runTest} className="space-y-3">
            <textarea value={question} onChange={(e) => setQuestion(e.target.value)} rows={3}
              placeholder="Hi, where is my order #1002?" className={inputCls + " resize-none"} required />
            <button type="submit" disabled={busy || question.trim().length < 5} className={primaryBtn}>
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              {busy ? "Thinking…" : "Generate draft"}
            </button>
          </form>

          {testResult && (
            <div className="mt-5 border-t border-line dark:border-line-dark pt-5 space-y-3.5">
              <div className="rounded-xl2 border border-line dark:border-line-dark bg-bg dark:bg-bg-dark p-5">
                <div className="flex items-center justify-between gap-3 flex-wrap mb-4">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[11px] font-mono uppercase tracking-wider text-ink-400 dark:text-ink-dark-400">Draft result</span>
                    <span className="text-xs font-medium bg-teal/10 text-teal-700 dark:text-teal rounded-full px-2.5 py-1">
                      {testResult.classification.category}
                    </span>
                    <span className="text-xs font-medium bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-full px-2.5 py-1">
                      {testResult.classification.priority} priority
                    </span>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-[11px] font-mono uppercase tracking-wider text-ink-400 dark:text-ink-dark-400">Confidence</span>
                    <span className="font-display text-2xl text-ink-900 dark:text-ink-dark-900 leading-none">
                      {Math.round(testResult.suggestion.confidence * 100)}%
                    </span>
                    <div className="w-28">
                      <ConfidenceBar value={testResult.suggestion.confidence} showLabel={false} />
                    </div>
                  </div>
                </div>

                <div className="flex items-center gap-2 flex-wrap mb-4 pb-4 border-b border-line dark:border-line-dark">
                  <span className="text-[11px] font-mono uppercase tracking-wider text-ink-400 dark:text-ink-dark-400 mr-1">Grounded in</span>
                  {testResult.order_context_used && (
                    <span className="inline-flex items-center gap-1.5 text-xs font-medium bg-teal/10 text-teal-700 dark:text-teal rounded-full px-2.5 py-1">
                      <ShoppingBag className="w-3 h-3" /> Real Shopify order data
                    </span>
                  )}
                  {testResult.kb_used && (
                    <span className="inline-flex items-center gap-1.5 text-xs font-medium bg-teal/10 text-teal-700 dark:text-teal rounded-full px-2.5 py-1">
                      <BookOpen className="w-3 h-3" /> Your knowledge base
                    </span>
                  )}
                  {!testResult.order_context_used && !testResult.kb_used && (
                    <span className="text-xs text-ink-400 dark:text-ink-dark-400">
                      General reply — no matching order or policy found
                    </span>
                  )}
                </div>

                <p className="text-[11px] font-mono uppercase tracking-wider text-ink-400 dark:text-ink-dark-400 mb-2">Drafted reply</p>
                <p className="text-sm text-ink-800 dark:text-ink-dark-800 whitespace-pre-wrap leading-relaxed">
                  {testResult.suggestion.suggested_response}
                </p>
              </div>
              <div className="flex items-start gap-2.5 rounded-lg bg-teal/5 border border-teal/20 px-4 py-3.5">
                <Eye className="w-4 h-4 text-teal shrink-0 mt-0.5" strokeWidth={2} />
                <p className="text-xs text-ink-700 dark:text-ink-dark-700 leading-relaxed">
                  This is a preview only — nothing was sent to any customer.
                  {testResult.suggestion.requires_human_review && " Every draft requires your approval before it can be sent."}
                </p>
              </div>
              <button onClick={() => onNavigate("tickets")} className={primaryBtn}>
                Finish setup <ArrowRight className="w-4 h-4" />
              </button>
            </div>
          )}
        </Card>
      )}

      {/* Back / skip nav */}
      <div className="flex items-center justify-between mt-5">
        <button onClick={() => setStep(Math.max(0, step - 1))} disabled={step === 0} className={secondaryBtn}>
          <ArrowLeft className="w-4 h-4" /> Back
        </button>
        {step < 3 && (
          <button onClick={() => setStep(step + 1)} className={secondaryBtn}>
            {step === 0 && !shopifyReady ? "Skip for now" : "Continue"} <ArrowRight className="w-4 h-4" />
          </button>
        )}
      </div>
    </div>
  );
}
