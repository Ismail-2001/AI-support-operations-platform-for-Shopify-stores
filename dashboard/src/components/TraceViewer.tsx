import { useState } from "react";
import { ChevronDown, Activity } from "lucide-react";
import type { TraceEntry } from "../lib/types";

export function TraceViewer({ traces }: { traces: TraceEntry[] }) {
  const [open, setOpen] = useState<number | null>(null);
  if (traces.length === 0) return null;

  return (
    <div className="border border-line dark:border-line-dark rounded-xl2 bg-surface dark:bg-surface-dark overflow-hidden">
      <div className="px-4 py-3 border-b border-line dark:border-line-dark flex items-center gap-2">
        <Activity className="w-3.5 h-3.5 text-ink-400 dark:text-ink-dark-400" />
        <span className="font-mono text-[11px] tracking-[0.14em] uppercase text-ink-400 dark:text-ink-dark-400">Pipeline trace</span>
      </div>
      <div className="divide-y divide-line dark:divide-line-dark">
        {traces.map((t, i) => (
          <div key={i}>
            <button
              onClick={() => setOpen(open === i ? null : i)}
              className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-ink-900/[0.02] dark:hover:bg-white/[0.02] transition-colors"
            >
              <div className="flex items-center gap-3">
                <span className="font-mono text-xs text-ink-900 dark:text-ink-dark-900">{t.stage}</span>
                {t.model && <span className="font-mono text-[11px] text-ink-400 dark:text-ink-dark-400">{t.model}</span>}
              </div>
              <div className="flex items-center gap-3 text-[11px] font-mono text-ink-400 dark:text-ink-dark-400">
                {t.latency_ms != null && <span>{Math.round(t.latency_ms)}ms</span>}
                {t.cost_usd != null && t.cost_usd > 0 && <span>${t.cost_usd.toFixed(6)}</span>}
                <ChevronDown className={`w-3.5 h-3.5 transition-transform ${open === i ? "rotate-180" : ""}`} />
              </div>
            </button>
            {open === i && (
              <div className="px-4 pb-4 space-y-3">
                <div>
                  <p className="text-[10px] uppercase tracking-wide text-ink-400 dark:text-ink-dark-400 mb-1">Input</p>
                  <pre className="text-[11px] font-mono bg-bg dark:bg-bg-dark rounded-lg p-3 overflow-x-auto whitespace-pre-wrap break-words text-ink-700 dark:text-ink-dark-700">
                    {JSON.stringify(t.input_summary, null, 2)}
                  </pre>
                </div>
                <div>
                  <p className="text-[10px] uppercase tracking-wide text-ink-400 dark:text-ink-dark-400 mb-1">Output</p>
                  <pre className="text-[11px] font-mono bg-bg dark:bg-bg-dark rounded-lg p-3 overflow-x-auto whitespace-pre-wrap break-words text-ink-700 dark:text-ink-dark-700">
                    {JSON.stringify(t.output_summary, null, 2)}
                  </pre>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
