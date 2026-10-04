import { useEffect, useState } from "react";
import { useConnection } from "./lib/useConnection";
import { ThemeProvider } from "./lib/ThemeProvider";
import { api } from "./lib/api";
import { ConnectScreen } from "./components/ConnectScreen";
import { Sidebar, type View } from "./components/Sidebar";
import { ToastProvider } from "./components/Toast";
import { CommandPalette } from "./components/CommandPalette";
import { TicketsPage } from "./pages/TicketsPage";
import { TicketDetailPage } from "./pages/TicketDetailPage";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { KnowledgeBasePage } from "./pages/KnowledgeBasePage";
import { SettingsPage } from "./pages/SettingsPage";
import { SetupPage } from "./pages/SetupPage";

type Health = { shopify_connected: boolean; gorgias_connected: boolean; auto_send_enabled: boolean; storage_persistent: boolean } | null;

export default function App() {
  const { connection, setConnection } = useConnection();
  const [view, setView] = useState<View>("tickets");
  const [openTicketId, setOpenTicketId] = useState<string | null>(null);
  const [health, setHealth] = useState<Health>(null);
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);

  useEffect(() => {
    if (connection) api.health(connection).then(setHealth).catch(() => setHealth(null));
  }, [connection]);

  // First load of an unconfigured instance lands the user on the setup wizard.
  useEffect(() => {
    if (!connection) return;
    api.setupStatus(connection)
      .then((s) => { if (!s.setup_complete) setView("setup"); })
      .catch(() => { /* status endpoint unavailable — keep default view */ });
    // eslint-disable-line react-hooks/exhaustive-deps
  }, [connection]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setCommandPaletteOpen((p) => !p);
      }
      if (e.key === "Escape") setCommandPaletteOpen(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  function navigateTo(v: View) {
    setView(v);
    setOpenTicketId(null);
  }

  if (!connection) {
    return (
      <ThemeProvider>
        <ConnectScreen onConnect={setConnection} />
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider>
      <ToastProvider>
        <div className="flex bg-bg dark:bg-bg-dark min-h-screen font-sans transition-colors">
          <Sidebar
            view={view}
            onNavigate={navigateTo}
            onDisconnect={() => setConnection(null)}
            health={health}
          />
          <main className="flex-1 px-8 py-7 overflow-x-hidden">
            {view === "tickets" && !openTicketId && (
              <TicketsPage connection={connection} onOpenTicket={setOpenTicketId} />
            )}
            {view === "tickets" && openTicketId && (
              <TicketDetailPage connection={connection} ticketId={openTicketId} onBack={() => setOpenTicketId(null)} />
            )}
            {view === "analytics" && <AnalyticsPage connection={connection} />}
            {view === "knowledge-base" && <KnowledgeBasePage connection={connection} />}
            {view === "setup" && <SetupPage connection={connection} onNavigate={navigateTo} />}
            {view === "settings" && <SettingsPage connection={connection} health={health} />}
          </main>
          <CommandPalette
            open={commandPaletteOpen}
            onClose={() => setCommandPaletteOpen(false)}
            onNavigate={navigateTo}
            onOpenTicket={(id) => { setView("tickets"); setOpenTicketId(id); }}
            connection={connection}
          />
        </div>
      </ToastProvider>
    </ThemeProvider>
  );
}
