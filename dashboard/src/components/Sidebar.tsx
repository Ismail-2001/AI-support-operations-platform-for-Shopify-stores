import { Inbox, BarChart3, BookOpen, Radio, LogOut, Sun, Moon, Settings, Wand2, MessageSquare, TrendingUp, Store } from "lucide-react";
import { useTheme } from "../lib/ThemeProvider";
import type { StoreRecord } from "../lib/types";

export type View =
  | "tickets"
  | "analytics"
  | "knowledge-base"
  | "widget"
  | "roi"
  | "settings"
  | "setup"
  | "stores";

export function Sidebar({
  view, onNavigate, onDisconnect, health, stores, selectedStore, onSelectStore,
}: {
  view: View;
  onNavigate: (v: View) => void;
  onDisconnect: () => void;
  health: { shopify_connected: boolean; gorgias_connected: boolean; auto_send_enabled: boolean } | null;
  stores?: StoreRecord[];
  selectedStore?: string | null;
  onSelectStore?: (id: string | null) => void;
}) {
  const { theme, toggle } = useTheme();
  const isDark = theme === "dark";

  const items: { id: View; label: string; icon: typeof Inbox }[] = [
    { id: "tickets", label: "Tickets", icon: Inbox },
    { id: "analytics", label: "Analytics", icon: BarChart3 },
    { id: "roi", label: "ROI & impact", icon: TrendingUp },
    { id: "knowledge-base", label: "Knowledge base", icon: BookOpen },
    { id: "widget", label: "Chat widget", icon: MessageSquare },
    { id: "setup", label: "Setup", icon: Wand2 },
    { id: "stores", label: "Stores", icon: Store },
    { id: "settings", label: "Settings", icon: Settings },
  ];

  return (
    <aside className={`w-60 shrink-0 flex flex-col h-screen sticky top-0 transition-colors ${
      isDark ? "bg-[#0B1018] text-white/90" : "bg-ink-900 text-white/90"
    }`}>
      <div className={`flex items-center gap-2.5 px-5 h-16 border-b ${isDark ? "border-white/8" : "border-white/10"}`}>
        <Radio className="w-4.5 h-4.5 text-teal" strokeWidth={2.25} />
        <span className="font-mono text-[11px] tracking-[0.18em] uppercase text-white/60">Support Console</span>
      </div>

      <div className={`px-4 py-3 border-b space-y-2.5 ${isDark ? "border-white/8" : "border-white/10"}`}>
        <div className="text-[10px] uppercase tracking-[0.14em] text-white/40">Active store</div>
        <select
          aria-label="Active store"
          value={selectedStore ?? ""}
          onChange={(e) => onSelectStore?.(e.target.value || null)}
          className="w-full bg-white/8 border border-white/10 rounded-lg px-2 py-1.5 text-xs text-white/90 outline-none focus:border-teal"
        >
          <option value="" className="text-black">Default store</option>
          {(stores ?? []).map((s) => (
            <option key={s.id} value={s.id} className="text-black">
              {s.name}
            </option>
          ))}
        </select>
      </div>

      <nav className="flex-1 px-3 py-5 space-y-1">
        {items.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => onNavigate(id)}
            className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors ${
              view === id
                ? `${isDark ? "bg-white/8" : "bg-white/10"} text-white`
                : `text-white/60 hover:${isDark ? "bg-white/4" : "bg-white/5"} hover:text-white/90`
            }`}
          >
            <Icon className="w-4 h-4" strokeWidth={2} />
            {label}
          </button>
        ))}
      </nav>

      <div className={`px-5 py-4 border-t space-y-2.5 ${isDark ? "border-white/8" : "border-white/10"}`}>
        <button
          onClick={toggle}
          className="w-full flex items-center gap-2 text-xs text-white/50 hover:text-white/80 transition-colors"
        >
          {isDark ? <Sun className="w-3.5 h-3.5" /> : <Moon className="w-3.5 h-3.5" />}
          {isDark ? "Light mode" : "Dark mode"}
        </button>
        <div className="flex items-center gap-2 text-xs text-white/50">
          <span className={`w-1.5 h-1.5 rounded-full ${health?.auto_send_enabled ? "bg-teal" : "bg-gold"}`} />
          Auto-send: {health?.auto_send_enabled ? "On" : "Off (Review mode)"}
        </div>
        <div className="flex items-center gap-2 text-xs text-white/50">
          <span className={`w-1.5 h-1.5 rounded-full ${health?.shopify_connected ? "bg-teal" : "bg-gold"}`} />
          Shopify: {health?.shopify_connected ? "Connected" : "Not connected"}
        </div>
        <div className="flex items-center gap-2 text-xs text-white/50">
          <span className={`w-1.5 h-1.5 rounded-full ${health?.gorgias_connected ? "bg-teal" : "bg-white/30"}`} />
          Gorgias: {health?.gorgias_connected ? "Connected" : "Not configured"}
        </div>
        <button
          onClick={onDisconnect}
          className="w-full flex items-center gap-2 pt-2 text-xs text-white/40 hover:text-white/70 transition-colors"
        >
          <LogOut className="w-3.5 h-3.5" /> Disconnect
        </button>
      </div>
    </aside>
  );
}
