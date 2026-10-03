import { useEffect, useState, useMemo } from "react";
import { Inbox, RefreshCw, Download, Command } from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";
import type { TicketWithSuggestion } from "../lib/types";
import { CategoryBadge, PriorityBadge, AutoSentBadge } from "../components/Badges";
import { ConfidenceBar } from "../components/ConfidenceBar";
import { SearchInput } from "../components/SearchInput";
import { TicketListSkeleton } from "../components/Skeleton";
import { useToast } from "../components/Toast";

function timeAgo(iso: string) {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function exportToCsv(tickets: TicketWithSuggestion[]) {
  const headers = ["ID", "Customer", "Email", "Subject", "Category", "Priority", "Status", "Auto-sent", "Confidence", "Created"];
  const rows = tickets.map((t) => [
    t.id,
    t.customer_name || "",
    t.customer_email,
    t.subject,
    t.category || "",
    t.priority || "",
    t.status,
    t.auto_sent ? "Yes" : "No",
    t.suggestion?.confidence?.toString() || "",
    t.created_at,
  ]);
  const csv = [headers, ...rows].map((r) => r.map((c) => `"${c.replace(/"/g, '""')}"`).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `tickets-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

export function TicketsPage({ connection, onOpenTicket }: { connection: Connection; onOpenTicket: (id: string) => void }) {
  const [tickets, setTickets] = useState<TicketWithSuggestion[] | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const { toast } = useToast();

  async function load() {
    try {
      const res = await api.listTickets(connection, statusFilter ? { status: statusFilter } : undefined);
      setTickets(res.tickets);
    } catch {
      setError("Couldn't load tickets. Check the connection in the sidebar.");
      toast("error", "Failed to load tickets");
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line
  }, [statusFilter]);

  const filtered = useMemo(() => {
    if (!tickets || !search.trim()) return tickets;
    const q = search.toLowerCase();
    return tickets.filter(
      (t) =>
        t.customer_name?.toLowerCase().includes(q) ||
        t.customer_email.toLowerCase().includes(q) ||
        t.subject.toLowerCase().includes(q) ||
        t.id.toLowerCase().includes(q)
    );
  }, [tickets, search]);

  const filters = [
    { id: "", label: "All" },
    { id: "open", label: "Open" },
    { id: "in_progress", label: "In progress" },
    { id: "resolved", label: "Resolved" },
  ];

  return (
    <div className="max-w-5xl">
      <header className="flex items-end justify-between mb-6">
        <div>
          <p className="font-mono text-[11px] tracking-[0.18em] uppercase text-ink-400 dark:text-ink-dark-400 mb-1">Inbox</p>
          <h1 className="font-display text-3xl text-ink-900 dark:text-ink-dark-900">Tickets</h1>
        </div>
        <div className="flex items-center gap-2">
          {tickets && tickets.length > 0 && (
            <button
              onClick={() => {
                exportToCsv(tickets);
                toast("success", "CSV exported successfully");
              }}
              className="flex items-center gap-1.5 text-xs text-ink-600 dark:text-ink-dark-600 hover:text-ink-900 dark:hover:text-ink-dark-900 px-3 py-2 rounded-lg hover:bg-ink-900/5 dark:hover:bg-white/5 transition-colors"
            >
              <Download className="w-3.5 h-3.5" /> Export CSV
            </button>
          )}
          <button onClick={load} className="flex items-center gap-1.5 text-xs text-ink-600 dark:text-ink-dark-600 hover:text-ink-900 dark:hover:text-ink-dark-900 px-3 py-2 rounded-lg hover:bg-ink-900/5 dark:hover:bg-white/5 transition-colors">
            <RefreshCw className="w-3.5 h-3.5" /> Refresh
          </button>
        </div>
      </header>

      <div className="flex items-center gap-3 mb-5">
        <div className="flex items-center gap-1">
          {filters.map((f) => (
            <button
              key={f.id}
              onClick={() => setStatusFilter(f.id)}
              className={`px-3 py-1.5 rounded-full text-xs font-medium transition-colors ${
                statusFilter === f.id
                  ? "bg-ink-900 text-white dark:bg-white dark:text-ink-900"
                  : "text-ink-600 dark:text-ink-dark-600 hover:bg-ink-900/5 dark:hover:bg-white/5"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex-1 max-w-xs relative">
          <SearchInput value={search} onChange={setSearch} placeholder="Search tickets…" />
          <div className="absolute right-2.5 top-1/2 -translate-y-1/2 hidden sm:flex items-center gap-0.5 pointer-events-none">
            <kbd className="text-[10px] font-mono text-ink-400 dark:text-ink-dark-400 bg-bg dark:bg-bg-dark px-1 py-0.5 rounded border border-line dark:border-line-dark">
              <Command className="w-2.5 h-2.5 inline" />
            </kbd>
            <kbd className="text-[10px] font-mono text-ink-400 dark:text-ink-dark-400 bg-bg dark:bg-bg-dark px-1 py-0.5 rounded border border-line dark:border-line-dark">
              K
            </kbd>
          </div>
        </div>
      </div>

      {error && <div className="rounded-lg bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-sm px-4 py-3 mb-4">{error}</div>}

      {tickets === null && !error && <TicketListSkeleton />}

      {tickets && filtered && filtered.length === 0 && (
        <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 py-16 text-center">
          <Inbox className="w-8 h-8 text-ink-400 dark:text-ink-dark-400 mx-auto mb-3" strokeWidth={1.5} />
          <p className="text-sm text-ink-600 dark:text-ink-dark-600">{search ? "No tickets match your search." : "No tickets here yet."}</p>
          <p className="text-xs text-ink-400 dark:text-ink-dark-400 mt-1">
            {search ? "Try a different search term." : "New customer messages will appear here as they come in."}
          </p>
        </div>
      )}

      {filtered && filtered.length > 0 && (
        <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel overflow-hidden divide-y divide-line dark:divide-line-dark">
          {filtered.map((t) => (
            <button
              key={t.id}
              onClick={() => onOpenTicket(t.id)}
              className="w-full text-left px-5 py-4 hover:bg-ink-900/[0.02] dark:hover:bg-white/[0.02] transition-colors grid grid-cols-[1fr_auto] gap-4 items-center"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-medium text-ink-900 dark:text-ink-dark-900 truncate">{t.customer_name || t.customer_email}</span>
                  <PriorityBadge priority={t.priority} />
                  <CategoryBadge category={t.category} />
                  <AutoSentBadge autoSent={t.auto_sent} />
                </div>
                <p className="text-sm text-ink-600 dark:text-ink-dark-600 truncate">{t.subject}</p>
              </div>
              <div className="flex items-center gap-6 shrink-0">
                {t.suggestion ? (
                  <div className="w-28"><ConfidenceBar value={t.suggestion.confidence} size="sm" /></div>
                ) : (
                  <div className="w-28 text-center text-xs text-ink-400 dark:text-ink-dark-400" title="No draft generated yet">–</div>
                )}
                <span className="font-mono text-xs text-ink-400 dark:text-ink-dark-400 w-16 text-right">{timeAgo(t.created_at)}</span>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
