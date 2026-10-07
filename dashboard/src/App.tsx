import { useEffect, useState } from "react";
import { useConnection } from "./lib/useConnection";
import { ThemeProvider } from "./lib/ThemeProvider";
import { api, getSelectedStore, setSelectedStore, UNAUTHORIZED_EVENT } from "./lib/api";
import { ConnectScreen } from "./components/ConnectScreen";
import { Sidebar, type View } from "./components/Sidebar";
import { ToastProvider, useToast } from "./components/Toast";
import { CommandPalette } from "./components/CommandPalette";
import { TicketsPage } from "./pages/TicketsPage";
import { TicketDetailPage } from "./pages/TicketDetailPage";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { KnowledgeBasePage } from "./pages/KnowledgeBasePage";
import { WidgetPage } from "./pages/WidgetPage";
import { RoiPage } from "./pages/RoiPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SetupPage } from "./pages/SetupPage";
import { StoresPage } from "./pages/StoresPage";
import type { StoreRecord } from "./lib/types";

type Health = { shopify_connected: boolean; gorgias_connected: boolean; auto_send_enabled: boolean; storage_persistent: boolean } | null;

/**
 * Surfaces API-key rejections once, centrally. A dead key can fail every
 * request in a burst (health, stores, page loads), so throttle to one toast
 * per few seconds instead of flooding. Lives under ToastProvider.
 */
function UnauthorizedListener() {
  const { toast } = useToast();
  useEffect(() => {
    let lastShownAt = 0;
    const handler = (e: Event) => {
      const now = Date.now();
      if (now - lastShownAt < 3000) return;
      lastShownAt = now;
      const detail = (e as CustomEvent<string>).detail;
      toast(
        "error",
        `API key rejected${detail ? `: ${detail}` : ""} — use Disconnect and reconnect with a valid key.`,
      );
    };
    window.addEventListener(UNAUTHORIZED_EVENT, handler);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, handler);
  }, [toast]);
  return null;
}

export default function App() {
  const { connection, setConnection } = useConnection();
  const [view, setView] = useState<View>("tickets");
  const [openTicketId, setOpenTicketId] = useState<string | null>(null);
  const [health, setHealth] = useState<Health>(null);
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  const [stores, setStores] = useState<StoreRecord[]>([]);
  const [storesVersion, setStoresVersion] = useState(0);
  // Active store -> X-Store-Id on every API call; also keys <main> so a switch
  // remounts the current page and refetches inside the new store's context.
  const [activeStore, setActiveStore] = useState<string | null>(() => getSelectedStore());

  useEffect(() => {
    if (connection) api.health(connection).then(setHealth).catch(() => setHealth(null));
  }, [connection]);

  useEffect(() => {
    if (!connection) return;
    api
      .listStores(connection)
      .then((r) => setStores(r.stores))
      .catch(() => setStores([]));
  }, [connection, storesVersion]);

  function activateStore(id: string | null) {
    setSelectedStore(id);
    setActiveStore(id);
    setOpenTicketId(null);
  }

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
        <UnauthorizedListener />
        <div className="flex bg-bg dark:bg-bg-dark min-h-screen font-sans transition-colors">
          <Sidebar
            view={view}
            onNavigate={navigateTo}
            onDisconnect={() => setConnection(null)}
            health={health}
            stores={stores}
            selectedStore={activeStore}
            onSelectStore={activateStore}
          />
          <main key={activeStore ?? "default"} className="flex-1 px-8 py-7 overflow-x-hidden">
            {view === "tickets" && !openTicketId && (
              <TicketsPage connection={connection} onOpenTicket={setOpenTicketId} />
            )}
            {view === "tickets" && openTicketId && (
              <TicketDetailPage connection={connection} ticketId={openTicketId} onBack={() => setOpenTicketId(null)} />
            )}
            {view === "analytics" && <AnalyticsPage connection={connection} />}
            {view === "roi" && <RoiPage connection={connection} />}
            {view === "knowledge-base" && <KnowledgeBasePage connection={connection} />}
            {view === "widget" && <WidgetPage connection={connection} />}
            {view === "setup" && <SetupPage connection={connection} onNavigate={navigateTo} />}
            {view === "stores" && (
              <StoresPage
                connection={connection}
                selectedStore={activeStore}
                onActivateStore={activateStore}
                onStoresChanged={() => setStoresVersion((v) => v + 1)}
              />
            )}
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
