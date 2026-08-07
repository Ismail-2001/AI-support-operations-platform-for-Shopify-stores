import { useEffect, useState } from "react";
import { ArrowLeft, Send, Sparkles, ChevronDown, MessageSquare } from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";
import type { TicketMessage, TicketWithSuggestion, TraceEntry } from "../lib/types";
import { CategoryBadge, PriorityBadge, SentimentBadge, SenderBadge } from "../components/Badges";
import { ConfidenceBar } from "../components/ConfidenceBar";
import { RefundApprovalPanel } from "../components/RefundApprovalPanel";
import { TraceViewer } from "../components/TraceViewer";
import { CustomerInfoPanel } from "../components/CustomerInfoPanel";
import { TicketDetailSkeleton } from "../components/Skeleton";
import { useToast } from "../components/Toast";

export function TicketDetailPage({ connection, ticketId, onBack }: { connection: Connection; ticketId: string; onBack: () => void }) {
  const [ticket, setTicket] = useState<TicketWithSuggestion | null>(null);
  const [thread, setThread] = useState<TicketMessage[]>([]);
  const [traces, setTraces] = useState<TraceEntry[]>([]);
  const [draft, setDraft] = useState("");
  const [sendViaGorgias, setSendViaGorgias] = useState(true);
  const [sendState, setSendState] = useState<"idle" | "sending" | "sent">("idle");
  const [showTrace, setShowTrace] = useState(false);
  const [note, setNote] = useState("");
  const [noteState, setNoteState] = useState<"idle" | "saving" | "saved">("idle");
  const { toast } = useToast();

  async function load() {
    try {
      const [t, th] = await Promise.all([api.getTicket(connection, ticketId), api.getThread(connection, ticketId)]);
      setTicket(t);
      setThread(th.messages);
      setDraft(t.suggestion?.suggested_response ?? "");
      try {
        const tr = await api.getTrace(connection, ticketId);
        setTraces(tr.traces);
      } catch {
        /* trace optional */
      }
    } catch {
      toast("error", "Failed to load ticket details");
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line
  }, [ticketId]);

  async function handleSend() {
    setSendState("sending");
    try {
      await api.respond(connection, ticketId, draft, sendViaGorgias && !!ticket?.gorgias_ticket_id);
      setSendState("sent");
      toast("success", "Reply sent successfully");
      load();
    } catch {
      setSendState("idle");
      toast("error", "Failed to send reply");
    }
  }

  async function handleAddNote() {
    if (!note.trim()) return;
    setNoteState("saving");
    try {
      setNoteState("saved");
      setNote("");
      toast("success", "Note added");
      setTimeout(() => setNoteState("idle"), 2000);
    } catch {
      setNoteState("idle");
      toast("error", "Failed to add note");
    }
  }

  if (!ticket) return <TicketDetailSkeleton />;

  return (
    <div className="max-w-6xl">
      <button onClick={onBack} className="flex items-center gap-1.5 text-xs text-ink-600 dark:text-ink-dark-600 hover:text-ink-900 dark:hover:text-ink-dark-900 mb-5">
        <ArrowLeft className="w-3.5 h-3.5" /> Back to tickets
      </button>

      <div className="grid grid-cols-[1fr_360px] gap-6 items-start">
        {/* ── Conversation thread ── */}
        <div className="min-w-0">
          <header className="mb-5">
            <p className="font-mono text-[11px] tracking-[0.18em] uppercase text-ink-400 dark:text-ink-dark-400 mb-1">{ticket.id}</p>
            <h1 className="font-display text-2xl text-ink-900 dark:text-ink-dark-900 mb-2">{ticket.subject}</h1>
            <div className="flex items-center gap-2">
              <PriorityBadge priority={ticket.priority} />
              <CategoryBadge category={ticket.category} />
              <SentimentBadge sentiment={ticket.sentiment} />
            </div>
          </header>

          <div className="space-y-3 mb-6">
            {thread.map((m) => (
              <div
                key={m.id}
                className={`rounded-xl2 border p-4 ${
                  m.sender_type === "customer"
                    ? "bg-surface dark:bg-surface-dark border-line dark:border-line-dark"
                    : "bg-violet-100/30 dark:bg-violet/10 border-violet/20 dark:border-violet/20"
                }`}
              >
                <div className="flex items-center justify-between mb-2">
                  <SenderBadge sender={m.sender_type} />
                  <span className="font-mono text-[11px] text-ink-400 dark:text-ink-dark-400">{new Date(m.created_at).toLocaleString()}</span>
                </div>
                <p className="text-sm text-ink-900 dark:text-ink-dark-900 whitespace-pre-wrap leading-relaxed">{m.content}</p>
              </div>
            ))}
          </div>

          {/* Internal Notes Section */}
          <div className="mb-6">
            <div className="flex items-center gap-2 mb-3">
              <MessageSquare className="w-4 h-4 text-ink-400 dark:text-ink-dark-400" />
              <span className="font-mono text-[11px] tracking-[0.14em] uppercase text-ink-400 dark:text-ink-dark-400">Internal Notes</span>
            </div>
            <div className="bg-gold-100/30 dark:bg-gold/5 border border-gold/20 dark:border-gold/20 rounded-xl2 p-4">
              <textarea
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Add a private note (only visible to your team)…"
                rows={3}
                className="w-full bg-transparent text-sm text-ink-900 dark:text-ink-dark-900 outline-none resize-none placeholder:text-ink-400 dark:placeholder:text-ink-dark-400"
              />
              <div className="flex justify-end mt-2">
                <button
                  onClick={handleAddNote}
                  disabled={noteState === "saving" || !note.trim()}
                  className="text-xs bg-gold text-white px-3 py-1.5 rounded-lg font-medium hover:bg-gold-700 disabled:opacity-40 transition-colors"
                >
                  {noteState === "saving" ? "Saving…" : noteState === "saved" ? "Saved" : "Add note"}
                </button>
              </div>
            </div>
          </div>

          <button
            onClick={() => setShowTrace(!showTrace)}
            className="flex items-center gap-1.5 text-xs text-ink-400 dark:text-ink-dark-400 hover:text-ink-600 dark:hover:text-ink-dark-600 mb-3"
          >
            <ChevronDown className={`w-3.5 h-3.5 transition-transform ${showTrace ? "rotate-180" : ""}`} />
            {showTrace ? "Hide" : "Show"} pipeline trace
          </button>
          {showTrace && <TraceViewer traces={traces} />}
        </div>

        {/* ── Decision panel ── */}
        <div className="sticky top-6 space-y-4">
          <CustomerInfoPanel ticket={ticket} />

          {ticket.suggestion && (
            <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5">
              <div className="flex items-center gap-2 mb-3">
                <Sparkles className="w-3.5 h-3.5 text-violet" />
                <span className="font-mono text-[11px] tracking-[0.14em] uppercase text-ink-400 dark:text-ink-dark-400">AI confidence</span>
              </div>
              <ConfidenceBar value={ticket.suggestion.confidence} size="lg" />
              <p className="text-xs text-ink-600 dark:text-ink-dark-600 mt-3 leading-relaxed">{ticket.suggestion.reasoning}</p>

              {ticket.suggestion.requires_human_review && (
                <div className="mt-3 text-[11px] text-gold-700 dark:text-gold bg-gold-100 dark:bg-gold/20 rounded-lg px-2.5 py-1.5 inline-block">
                  Requires human review
                </div>
              )}
            </div>
          )}

          {ticket.suggestion?.suggested_action && ticket.suggestion.suggested_action.type !== "none" && (
            <RefundApprovalPanel
              connection={connection}
              ticketId={ticket.id}
              action={ticket.suggestion.suggested_action}
              onApproved={load}
            />
          )}

          <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5">
            <label className="block font-mono text-[11px] tracking-[0.14em] uppercase text-ink-400 dark:text-ink-dark-400 mb-2">Reply</label>
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={8}
              className="w-full rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-3 py-2.5 text-sm text-ink-900 dark:text-ink-dark-900 leading-relaxed outline-none focus:border-teal focus:ring-2 focus:ring-teal/20 resize-none"
            />
            {ticket.gorgias_ticket_id && (
              <label className="flex items-center gap-2 mt-2.5 text-xs text-ink-600 dark:text-ink-dark-600">
                <input type="checkbox" checked={sendViaGorgias} onChange={(e) => setSendViaGorgias(e.target.checked)} className="accent-teal" />
                Send to customer via Gorgias
              </label>
            )}
            <button
              onClick={handleSend}
              disabled={sendState === "sending" || !draft.trim()}
              className="w-full mt-3 flex items-center justify-center gap-2 bg-ink-900 dark:bg-white text-white dark:text-ink-900 rounded-lg py-2.5 text-sm font-medium hover:bg-ink-700 dark:hover:bg-ink-dark-900 disabled:opacity-40 transition-colors"
            >
              <Send className="w-3.5 h-3.5" />
              {sendState === "sent" ? "Sent" : sendState === "sending" ? "Sending…" : "Send reply"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
