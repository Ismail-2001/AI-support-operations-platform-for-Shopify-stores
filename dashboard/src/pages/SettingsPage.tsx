import { useState } from "react";
import { Eye, EyeOff, RefreshCw, Check, ExternalLink } from "lucide-react";
import { useTheme } from "../lib/ThemeProvider";
import type { Connection } from "../lib/api";

interface Props {
  connection: Connection;
  health: { shopify_connected: boolean; gorgias_connected: boolean; auto_send_enabled: boolean } | null;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-8">
      <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-3">{title}</h2>
      <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5">{children}</div>
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-3 border-b border-line dark:border-line-dark last:border-0">
      <span className="text-sm text-ink-700 dark:text-ink-dark-700">{label}</span>
      {children}
    </div>
  );
}

function Badge({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs font-medium px-2 py-1 rounded-full ${
      ok ? "bg-teal-100 text-teal-700 dark:bg-teal/20 dark:text-teal" : "bg-gold-100 text-gold-700 dark:bg-gold/20 dark:text-gold"
    }`}>
      <span className={`w-1.5 h-1.5 rounded-full ${ok ? "bg-teal" : "bg-gold"}`} />
      {label}
    </span>
  );
}

export function SettingsPage({ connection, health }: Props) {
  const { theme, toggle } = useTheme();
  const [showKey, setShowKey] = useState(false);
  const [copied, setCopied] = useState(false);

  function copyKey() {
    navigator.clipboard.writeText(connection.apiKey);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div className="max-w-3xl">
      <header className="mb-6">
        <p className="font-mono text-[11px] tracking-[0.18em] uppercase text-ink-400 dark:text-ink-dark-400 mb-1">Configuration</p>
        <h1 className="font-display text-3xl text-ink-900 dark:text-ink-dark-900">Settings</h1>
      </header>

      <Section title="Connection">
        <Field label="API base URL">
          <span className="text-sm font-mono text-ink-600 dark:text-ink-dark-600">{connection.baseUrl}</span>
        </Field>
        <Field label="API key">
          <div className="flex items-center gap-2">
            <span className="text-sm font-mono text-ink-600 dark:text-ink-dark-600">
              {showKey ? connection.apiKey : "••••••••" + connection.apiKey.slice(-4)}
            </span>
            <button onClick={() => setShowKey(!showKey)} className="text-ink-400 dark:text-ink-dark-400 hover:text-ink-700 dark:hover:text-ink-dark-700 transition-colors">
              {showKey ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            </button>
            <button onClick={copyKey} className="text-ink-400 dark:text-ink-dark-400 hover:text-ink-700 dark:hover:text-ink-dark-700 transition-colors">
              {copied ? <Check className="w-3.5 h-3.5 text-teal" /> : <RefreshCw className="w-3.5 h-3.5" />}
            </button>
          </div>
        </Field>
      </Section>

      <Section title="Integrations">
        <Field label="Shopify">
          <Badge ok={!!health?.shopify_connected} label={health?.shopify_connected ? "Connected" : "Not configured"} />
        </Field>
        <Field label="Gorgias">
          <Badge ok={!!health?.gorgias_connected} label={health?.gorgias_connected ? "Connected" : "Not configured"} />
        </Field>
        <Field label="Sending mode">
          <Badge
            ok={!health?.auto_send_enabled}
            label={health?.auto_send_enabled ? "Auto-send — on" : "Review mode — you approve each reply"}
          />
        </Field>
      </Section>

      <Section title="Appearance">
        <Field label="Theme">
          <button
            onClick={toggle}
            className="flex items-center gap-2 text-sm text-ink-600 dark:text-ink-dark-600 hover:text-ink-900 dark:hover:text-ink-dark-900 transition-colors px-3 py-1.5 rounded-lg border border-line dark:border-line-dark"
          >
            {theme === "dark" ? "Dark" : "Light"}
            <RefreshCw className="w-3 h-3" />
          </button>
        </Field>
      </Section>

      <Section title="Shortcuts">
        <Field label="Search / Command palette">
          <kbd className="text-xs font-mono text-ink-400 dark:text-ink-dark-400 bg-bg dark:bg-bg-dark px-2 py-1 rounded border border-line dark:border-line-dark">
            Ctrl + K
          </kbd>
        </Field>
        <Field label="Close palette / Modal">
          <kbd className="text-xs font-mono text-ink-400 dark:text-ink-dark-400 bg-bg dark:bg-bg-dark px-2 py-1 rounded border border-line dark:border-line-dark">
            Esc
          </kbd>
        </Field>
      </Section>

      <Section title="About">
        <Field label="Version">
          <span className="text-sm font-mono text-ink-600 dark:text-ink-dark-600">1.0.0</span>
        </Field>
        <Field label="Documentation">
          <a
            href="https://github.com/Ismail-2001/customer-support-ai-employee"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-teal hover:text-teal-700 transition-colors"
          >
            GitHub <ExternalLink className="w-3 h-3" />
          </a>
        </Field>
      </Section>
    </div>
  );
}
