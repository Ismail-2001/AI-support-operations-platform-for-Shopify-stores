import { useEffect, useRef, useState } from "react";
import { Search, Inbox, BarChart3, BookOpen, Settings } from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";
import type { TicketWithSuggestion } from "../lib/types";

interface Props {
  open: boolean;
  onClose: () => void;
  onNavigate: (v: "tickets" | "analytics" | "knowledge-base" | "settings") => void;
  onOpenTicket: (id: string) => void;
  connection: Connection;
}

export function CommandPalette({ open, onClose, onNavigate, onOpenTicket, connection }: Props) {
  const [query, setQuery] = useState("");
  const [tickets, setTickets] = useState<TicketWithSuggestion[]>([]);
  const [searching, setSearching] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setTickets([]);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  useEffect(() => {
    if (!query.trim()) {
      setTickets([]);
      return;
    }
    const timer = setTimeout(() => {
      setSearching(true);
      api
        .listTickets(connection)
        .then((res) => {
          const q = query.toLowerCase();
          setTickets(
            res.tickets.filter(
              (t) =>
                t.customer_name?.toLowerCase().includes(q) ||
                t.customer_email.toLowerCase().includes(q) ||
                t.subject.toLowerCase().includes(q) ||
                t.id.toLowerCase().includes(q)
            )
          );
        })
        .catch(() => setTickets([]))
        .finally(() => setSearching(false));
    }, 200);
    return () => clearTimeout(timer);
  }, [query, connection]);

  function handleSelect(action: () => void) {
    onClose();
    action();
  }

  if (!open) return null;

  const navItems = [
    { id: "tickets" as const, label: "Tickets", icon: Inbox },
    { id: "analytics" as const, label: "Analytics", icon: BarChart3 },
    { id: "knowledge-base" as const, label: "Knowledge base", icon: BookOpen },
    { id: "settings" as const, label: "Settings", icon: Settings },
  ];

  const filteredNav = navItems.filter((n) => !query || n.label.toLowerCase().includes(query.toLowerCase()));

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[15vh]" onClick={onClose}>
      <div className="absolute inset-0 bg-black/40 dark:bg-black/60" />
      <div
        className="relative w-full max-w-lg bg-surface dark:bg-[#162230] border border-line dark:border-[#253546] rounded-xl2 shadow-panel-dark overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 px-4 py-3 border-b border-line dark:border-[#253546]">
          <Search className="w-4 h-4 text-ink-400 dark:text-ink-dark-400 shrink-0" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search tickets, pages, actions..."
            className="flex-1 bg-transparent text-sm text-ink-900 dark:text-ink-dark-900 placeholder:text-ink-400 dark:placeholder:text-ink-dark-400 outline-none"
          />
          <kbd className="hidden sm:inline text-[10px] font-mono text-ink-400 dark:text-ink-dark-400 bg-bg dark:bg-[#0F171E] px-1.5 py-0.5 rounded border border-line dark:border-[#253546]">
            ESC
          </kbd>
        </div>

        <div className="max-h-80 overflow-y-auto p-2">
          {filteredNav.length > 0 && (
            <div className="mb-2">
              <p className="text-[10px] font-mono uppercase tracking-wider text-ink-400 dark:text-ink-dark-400 px-2 py-1.5">
                Pages
              </p>
              {filteredNav.map((item) => (
                <button
                  key={item.id}
                  onClick={() => handleSelect(() => onNavigate(item.id))}
                  className="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm text-ink-700 dark:text-ink-dark-700 hover:bg-ink-900/5 dark:hover:bg-white/5 transition-colors"
                >
                  <item.icon className="w-4 h-4 text-ink-400 dark:text-ink-dark-400" />
                  {item.label}
                </button>
              ))}
            </div>
          )}

          {query.trim() && (
            <div>
              <p className="text-[10px] font-mono uppercase tracking-wider text-ink-400 dark:text-ink-dark-400 px-2 py-1.5">
                Tickets
              </p>
              {searching && (
                <p className="text-sm text-ink-400 dark:text-ink-dark-400 px-3 py-2">Searching...</p>
              )}
              {!searching && tickets.length === 0 && (
                <p className="text-sm text-ink-400 dark:text-ink-dark-400 px-3 py-2">No tickets found</p>
              )}
              {tickets.map((t) => (
                <button
                  key={t.id}
                  onClick={() => handleSelect(() => onOpenTicket(t.id))}
                  className="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm text-ink-700 dark:text-ink-dark-700 hover:bg-ink-900/5 dark:hover:bg-white/5 transition-colors text-left"
                >
                  <div className="flex-1 min-w-0">
                    <p className="truncate font-medium">{t.customer_name || t.customer_email}</p>
                    <p className="truncate text-xs text-ink-400 dark:text-ink-dark-400">{t.subject}</p>
                  </div>
                  {t.suggestion && (
                    <span
                      className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${
                        t.suggestion.confidence >= 0.8
                          ? "bg-teal-100 text-teal-700 dark:bg-teal/20 dark:text-teal"
                          : "bg-gold-100 text-gold-700 dark:bg-gold/20 dark:text-gold"
                      }`}
                    >
                      {Math.round(t.suggestion.confidence * 100)}%
                    </span>
                  )}
                </button>
              ))}
            </div>
          )}

          {!query.trim() && (
            <p className="text-sm text-ink-400 dark:text-ink-dark-400 text-center py-6">
              Type to search tickets or navigate to pages
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
