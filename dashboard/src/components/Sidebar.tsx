import { Inbox, BarChart3, BookOpen, Radio, LogOut, Sun, Moon, Settings, Wand2 } from "lucide-react";
import { useTheme } from "../lib/ThemeProvider";

export type View = "tickets" | "analytics" | "knowledge-base" | "settings" | "setup";

export function Sidebar({
  view, onNavigate, onDisconnect, health,
}: {
  view: View;
  onNavigate: (v: View) => void;
  onDisconnect: () => void;
  health: { shopify_connected: boolean; gorgias_connected: boolean; auto_send_enabled: boolean } | null;
}) {
  const { theme, toggle } = useTheme();
  const isDark = theme === "dark";

  const items: { id: View; label: string; icon: typeof Inbox }[] = [
    { id: "tickets", label: "Tickets", icon: Inbox },
    { id: "analytics", label: "Analytics", icon: BarChart3 },
    { id: "knowledge-base", label: "Knowledge base", icon: BookOpen },
    { id: "setup", label: "Setup", icon: Wand2 },
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
          Auto-send {health?.auto_send_enabled ? "on" : "off"}
        </div>
        <div className="flex items-center gap-2 text-xs text-white/50">
          <span className={`w-1.5 h-1.5 rounded-full ${health?.shopify_connected ? "bg-teal" : "bg-gold"}`} />
          Shopify {health?.shopify_connected ? "connected" : "off"}
        </div>
        <div className="flex items-center gap-2 text-xs text-white/50">
          <span className={`w-1.5 h-1.5 rounded-full ${health?.gorgias_connected ? "bg-teal" : "bg-gold"}`} />
          Gorgias {health?.gorgias_connected ? "connected" : "off"}
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
