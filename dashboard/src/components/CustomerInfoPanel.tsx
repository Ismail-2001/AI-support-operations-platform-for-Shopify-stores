import { User, Mail, ShoppingCart, Clock } from "lucide-react";
import type { TicketWithSuggestion } from "../lib/types";

function InfoRow({ icon: Icon, label, value }: { icon: typeof User; label: string; value: string | null | undefined }) {
  return (
    <div className="flex items-center gap-3 py-2">
      <Icon className="w-4 h-4 text-ink-400 dark:text-ink-dark-400 shrink-0" />
      <div className="min-w-0">
        <p className="text-[11px] font-medium text-ink-400 dark:text-ink-dark-400 uppercase tracking-wide">{label}</p>
        <p className="text-sm text-ink-900 dark:text-ink-dark-900 truncate">{value || "—"}</p>
      </div>
    </div>
  );
}

export function CustomerInfoPanel({ ticket }: { ticket: TicketWithSuggestion }) {
  const createdAt = new Date(ticket.created_at);
  const timeAgo = getTimeAgo(createdAt);

  return (
    <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5">
      <div className="flex items-center gap-2 mb-4">
        <div className="w-10 h-10 rounded-full bg-ink-900/[0.05] dark:bg-white/[0.05] flex items-center justify-center">
          <User className="w-5 h-5 text-ink-400 dark:text-ink-dark-400" />
        </div>
        <div>
          <p className="text-sm font-medium text-ink-900 dark:text-ink-dark-900">{ticket.customer_name || "Unknown"}</p>
          <p className="text-[11px] text-ink-400 dark:text-ink-dark-400">{ticket.channel}</p>
        </div>
      </div>

      <div className="space-y-1 divide-y divide-line dark:divide-line-dark">
        <InfoRow icon={Mail} label="Email" value={ticket.customer_email} />
        <InfoRow icon={ShoppingCart} label="Order" value={ticket.order_id || ticket.order_number} />
        <InfoRow icon={Clock} label="Created" value={timeAgo} />
      </div>

      {ticket.shop_domain && (
        <div className="mt-4 pt-3 border-t border-line dark:border-line-dark">
          <p className="text-[11px] text-ink-400 dark:text-ink-dark-400">
            Shop: <span className="text-ink-600 dark:text-ink-dark-600 font-mono">{ticket.shop_domain}</span>
          </p>
        </div>
      )}
    </div>
  );
}

function getTimeAgo(date: Date): string {
  const diffMs = Date.now() - date.getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}
