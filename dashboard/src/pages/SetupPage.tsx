import { useEffect, useState } from "react";
import {
  AlertCircle, ArrowLeft, ArrowRight, BookOpen, Check, CheckCircle2, ChevronDown,
  HelpCircle, Image as ImageIcon, Link2, Loader2, MessageSquareText, Music,
  Package, PlayCircle, ShieldCheck, ShoppingBag, Sparkles,
} from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";
import type { BrandVoice, SetupStatus, SetupTestResult } from "../lib/types";
import type { View } from "../components/Sidebar";
import { ConfidenceBar } from "../components/ConfidenceBar";

// Paste your Loom URL for the Shopify app walkthrough here — the video link
// appears automatically inside the "I need help creating the Shopify app" panel.
const SHOPIFY_SETUP_VIDEO_URL = "";

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

function Screenshot({ file, alt, caption }: { file: string; alt: string; caption: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div className="rounded-xl2 border border-dashed border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-4 py-4 mb-4 flex items-center gap-3">
        <ImageIcon className="w-4 h-4 text-ink-400 dark:text-ink-dark-400 shrink-0" />
        <div className="min-w-0">
          <p className="text-xs text-ink-600 dark:text-ink-dark-600">{caption}</p>
          <p className="text-[11px] font-mono text-ink-400 dark:text-ink-dark-400 truncate">
            Screenshot placeholder — drop the image at public/screenshots/{file}
          </p>
        </div>
      </div>
    );
  }
  return (
    <img
      src={`/screenshots/${file}`}
      alt={alt}
      onError={() => setFailed(true)}
      className="rounded-xl2 border border-line dark:border-line-dark mb-4 w-full"
    />
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
  const [helpOpen, setHelpOpen] = useState(false);
  const [finished, setFinished] = useState(false);
  const [health, setHealth] = useState<{ auto_send_enabled: boolean } | null>(null);

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
    api.health(connection).then((h) => setHealth(h)).catch(() => setHealth(null));
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

  if (finished) {
    return (
      <div className="max-w-3xl">
        <div className="rounded-xl2 border border-line dark:border-line-dark bg-surface dark:bg-surface-dark shadow-panel p-8 text-center">
          <div className="mx-auto w-12 h-12 rounded-full bg-teal/10 flex items-center justify-center mb-4">
            <CheckCircle2 className="w-6 h-6 text-teal" />
          </div>
          <p className="font-mono text-[11px] tracking-[0.18em] uppercase text-ink-400 dark:text-ink-dark-400 mb-1">
            Setup complete
          </p>
          <h1 className="font-display text-3xl text-ink-900 dark:text-ink-dark-900">You're ready</h1>
          <p className="text-sm text-ink-600 dark:text-ink-dark-600 mt-2 max-w-md mx-auto leading-relaxed">
            Your store is connected, your policies are imported, and the agent has its first draft.
            It's now standing by in your dashboard.
          </p>

          {health && !health.auto_send_enabled && (
            <div className="inline-flex items-center gap-2 rounded-full bg-teal/10 text-teal-700 dark:text-teal text-sm font-medium px-4 py-2.5 mt-5">
              <ShieldCheck className="w-4 h-4" />
              Review mode is active — you approve every draft
            </div>
          )}
          {health && health.auto_send_enabled && (
            <div className="inline-flex items-center gap-2 rounded-full bg-gold-100 dark:bg-gold/15 text-gold-700 dark:text-gold text-sm font-medium px-4 py-2.5 mt-5">
              <ShieldCheck className="w-4 h-4" />
              Auto-send is on — refunds and complaints still need your approval
            </div>
          )}

          <div className="grid grid-cols-3 gap-3 mt-7 text-left">
            <div className="rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-3.5 py-3">
              <p className="text-[11px] font-mono uppercase tracking-wider text-ink-400 dark:text-ink-dark-400 mb-1">Store</p>
              <p className="text-sm font-medium text-ink-900 dark:text-ink-dark-900 truncate">
                {status.shopify.domain ?? "Connected"}
              </p>
            </div>
            <div className="rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-3.5 py-3">
              <p className="text-[11px] font-mono uppercase tracking-wider text-ink-400 dark:text-ink-dark-400 mb-1">Knowledge</p>
              <p className="text-sm font-medium text-ink-900 dark:text-ink-dark-900">{chunkCount} chunks</p>
            </div>
            <div className="rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-3.5 py-3">
              <p className="text-[11px] font-mono uppercase tracking-wider text-ink-400 dark:text-ink-dark-400 mb-1">Brand voice</p>
              <p className="text-sm font-medium text-ink-900 dark:text-ink-dark-900 truncate">
                {voice.store_name || "Set"}
              </p>
            </div>
          </div>

          <div className="text-left rounded-xl2 border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-5 py-4 mt-5">
            <p className="text-[11px] font-mono uppercase tracking-wider text-ink-400 dark:text-ink-dark-400 mb-3">What happens next</p>
            <ol className="space-y-2.5 text-sm text-ink-700 dark:text-ink-dark-700 list-decimal pl-4 marker:text-teal marker:text-xs">
              <li>
                New customer emails land in the <span className="font-medium text-ink-900 dark:text-ink-dark-900">Tickets</span> tab —
                each one with an AI draft, a confidence score, and a review flag.
              </li>
              <li>
                Read the draft, edit it if you want, then approve it — or leave it. Nothing reaches a
                customer until you say so.
              </li>
              <li>
                Want different wording? Change the tone any time in Setup → Brand voice. When you're
                comfortable, you can turn on auto-send in Settings.
              </li>
            </ol>
          </div>

          <div className="flex items-center justify-center gap-3 mt-7">
            <button onClick={() => onNavigate("tickets")} className={primaryBtn}>
              Open Tickets <ArrowRight className="w-4 h-4" />
            </button>
            <button onClick={() => setFinished(false)} className={secondaryBtn}>
              Back to the test
            </button>
          </div>
        </div>
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
            The agent reads real orders from your store so it never has to guess where a package is.
            You'll create a one-time "custom app" in Shopify — about two minutes, no code, and it
            cannot change anything inside your store.
          </p>

          <ol className="space-y-2 text-sm text-ink-700 dark:text-ink-dark-700 list-decimal pl-5 mb-4 marker:text-teal marker:text-xs">
            <li>
              In your Shopify admin, go to <span className="font-medium">Settings → Apps and sales channels → Develop apps</span>.
            </li>
            <li>
              Click <span className="font-medium">Create custom app</span> and name it anything you'll
              recognize — "Support Agent" works.
            </li>
            <li>
              When asked for API permissions, search <span className="font-mono text-xs">read</span> and
              enable <span className="font-medium">read_orders</span> and{" "}
              <span className="font-medium">read_products</span>, then install the app.
            </li>
            <li>
              Shopify shows an <span className="font-medium">Admin API access token</span> once — it
              starts with <span className="font-mono text-xs">shpat_</span>. Copy it into the field below.
            </li>
          </ol>

          <Screenshot
            file="step-1-shopify-app.png"
            alt="Where to find Develop apps in the Shopify admin settings"
            caption="In the Shopify admin: Settings → Apps and sales channels → Develop apps"
          />

          <div className="mb-4">
            <button
              type="button"
              onClick={() => setHelpOpen(!helpOpen)}
              className="inline-flex items-center gap-2 text-sm font-medium text-teal hover:underline"
            >
              <HelpCircle className="w-4 h-4" />
              I need help creating the Shopify app
              <ChevronDown className={`w-4 h-4 transition-transform ${helpOpen ? "rotate-180" : ""}`} />
            </button>
            {helpOpen && (
              <div className="mt-3 rounded-lg bg-bg dark:bg-bg-dark border border-line dark:border-line-dark px-4 py-3.5 space-y-2.5 text-[13px] text-ink-700 dark:text-ink-dark-700">
                <p>This is the fiddliest part of setup. The three places people get stuck:</p>
                <ul className="space-y-2 list-disc pl-4 marker:text-teal">
                  <li>
                    <span className="font-medium">No "Create custom app" button?</span> On the Develop
                    apps screen, click <span className="font-medium">Allow custom app development</span>{" "}
                    first — Shopify asks once per store.
                  </li>
                  <li>
                    <span className="font-medium">Which permissions?</span> Just{" "}
                    <span className="font-medium">read_orders</span> and{" "}
                    <span className="font-medium">read_products</span>. If the knowledge import can't
                    find your policies later, also enable any store-content read permission.
                  </li>
                  <li>
                    <span className="font-medium">Where is the token?</span> After you click{" "}
                    <span className="font-medium">Install</span>, look under "Admin API access token"
                    and click <span className="font-medium">Reveal</span>. Copy it right away —
                    Shopify only shows it once.
                  </li>
                </ul>
                {SHOPIFY_SETUP_VIDEO_URL ? (
                  <a
                    href={SHOPIFY_SETUP_VIDEO_URL}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1.5 text-teal hover:underline font-medium"
                  >
                    <PlayCircle className="w-4 h-4" /> Watch the 2-minute setup walkthrough
                  </a>
                ) : (
                  <p className="text-xs text-ink-400 dark:text-ink-dark-400">
                    A short video walkthrough is on the way — the steps above cover everything.
                  </p>
                )}
              </div>
            )}
          </div>

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
              <p className="text-xs text-ink-400 dark:text-ink-dark-400 mt-1">Your store's web address.</p>
            </div>
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Admin API access token</label>
              <input type="password" value={accessToken} onChange={(e) => setAccessToken(e.target.value)}
                placeholder="shpat_…" className={inputCls} required />
              <p className="text-xs text-ink-400 dark:text-ink-dark-400 mt-1">
                The secret you copied in step 4 above. It is stored only in your own connection — never shared.
              </p>
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
            One click pulls your store's own words — refund policy, shipping policy, product
            descriptions — so the agent quotes your policies instead of guessing at them.
          </p>

          <ol className="space-y-2 text-sm text-ink-700 dark:text-ink-dark-700 list-decimal pl-5 mb-4 marker:text-teal marker:text-xs">
            <li>Make sure step 1 says <span className="font-medium">Connected</span> above.</li>
            <li>
              Click <span className="font-medium">Import from Shopify</span> and give it a few
              seconds.
            </li>
            <li>
              When it finishes, the count below tells you how many pieces of your store the agent can
              now cite. You can add more anytime.
            </li>
          </ol>

          <Screenshot
            file="step-2-knowledge-import.png"
            alt="The Import from Shopify button and the chunk count"
            caption="The Import button — your policies and products are pulled in automatically"
          />

          {!status.google_key_set && (
            <div className="rounded-lg bg-gold-100 dark:bg-gold/15 text-gold-700 dark:text-gold text-sm px-3.5 py-2.5 mb-4">
              Setup note for your developer: GOOGLE_API_KEY isn't set on the server yet, so the
              import will fail. Add it to <span className="font-mono text-xs">.env</span> first.
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
            This is how every draft reply is written — the customer should never be able to tell a
            human didn't write it. Three quick fields, and you can change them any time.
          </p>
          <form onSubmit={saveVoice} className="space-y-3.5">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Store name</label>
                <input value={voice.store_name} onChange={(e) => setVoice({ ...voice, store_name: e.target.value })}
                  placeholder="Northwind Supply" className={inputCls} />
                <p className="text-xs text-ink-400 dark:text-ink-dark-400 mt-1">
                  The name customers see — your store name is fine.
                </p>
              </div>
              <div>
                <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">How should replies sound?</label>
                <select value={voice.tone} onChange={(e) => setVoice({ ...voice, tone: e.target.value as BrandVoice["tone"] })} className={inputCls}>
                  <option value="friendly">Friendly — warm, helpful small-business owner</option>
                  <option value="professional">Professional — polished, no slang</option>
                  <option value="casual">Casual — relaxed, conversational</option>
                </select>
                <p className="text-xs text-ink-400 dark:text-ink-dark-400 mt-1">
                  Pick the closest match — each option shows how it writes.
                </p>
              </div>
            </div>
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Sign-off</label>
              <input value={voice.sign_off} onChange={(e) => setVoice({ ...voice, sign_off: e.target.value })}
                placeholder="Thanks! — The Northwind team" className={inputCls} />
              <p className="text-xs text-ink-400 dark:text-ink-dark-400 mt-1">
                How every reply ends — you'll see a live preview below the button.
              </p>
            </div>
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Support email (when a human is needed)</label>
              <input type="email" value={voice.support_email} onChange={(e) => setVoice({ ...voice, support_email: e.target.value })}
                placeholder="help@yourstore.com" className={inputCls} />
              <p className="text-xs text-ink-400 dark:text-ink-dark-400 mt-1">
                Where conversations go when the agent isn't sure — usually your normal support inbox.
              </p>
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
            Ask what a real customer would ask. The agent classifies it, looks up real order and
            policy context, and drafts a reply — for your eyes only, nothing goes out.
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
              <div className="rounded-xl2 border border-teal/30 bg-teal/5 px-5 py-4 flex items-start gap-3.5">
                <div className="w-7 h-7 rounded-full bg-teal text-white flex items-center justify-center shrink-0 mt-0.5">
                  <Check className="w-4 h-4" strokeWidth={3} />
                </div>
                <div>
                  <p className="text-sm font-semibold text-ink-900 dark:text-ink-dark-900">
                    This is only a preview. Nothing was sent to any customer.
                  </p>
                  <p className="text-xs text-ink-600 dark:text-ink-dark-600 mt-1 leading-relaxed">
                    Nothing was saved either — run it as many times as you like. Drafts in the
                    dashboard work the same way: they wait for your approval before anything can be
                    sent.
                    {testResult.suggestion.requires_human_review &&
                      " This one would be flagged for review, too."}
                  </p>
                </div>
              </div>
              <button onClick={() => setFinished(true)} className={primaryBtn}>
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
