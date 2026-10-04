import { useEffect, useState } from "react";
import { ShieldAlert, Check, Package } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { Connection } from "../lib/api";
import type { SuggestedAction, TicketOrder } from "../lib/types";

export function RefundApprovalPanel({
  connection, ticketId, action, onApproved,
}: {
  connection: Connection;
  ticketId: string;
  action: SuggestedAction;
  onApproved: () => void;
}) {
  const isRefund = action.type === "refund";
  const [amount, setAmount] = useState(action.amount ?? 0);
  const [reason, setReason] = useState(action.reason ?? "");
  const [state, setState] = useState<"idle" | "confirming" | "submitting" | "done" | "error">("idle");
  const [error, setError] = useState("");
  const [order, setOrder] = useState<TicketOrder | null>(null);
  const [selected, setSelected] = useState<Record<number, number>>({});

  useEffect(() => {
    if (!isRefund) return;
    api
      .getTicketOrder(connection, ticketId)
      .then(setOrder)
      .catch(() => {
        /* order details are a preview — the server re-verifies everything */
      });
  }, [connection, ticketId, isRefund]);

  if (action.type === "none") return null;

  const refundable = order?.refundable;
  const overCap = isRefund && refundable != null && amount > refundable;
  const lineItems = Object.entries(selected).map(([id, quantity]) => ({
    line_item_id: Number(id),
    quantity,
  }));

  function toggleItem(id: number, qty: number) {
    setSelected((prev) => {
      const next = { ...prev };
      if (id in next) delete next[id];
      else next[id] = Math.max(1, Math.min(qty, 1));
      return next;
    });
  }

  function setQty(id: number, qty: number) {
    setSelected((prev) => ({ ...prev, [id]: Math.max(1, qty) }));
  }

  async function submit() {
    setState("submitting");
    setError("");
    try {
      const idempotencyKey = crypto.randomUUID();
      if (isRefund) {
        await api.approveRefund(connection, ticketId, amount, reason, idempotencyKey, lineItems);
      } else {
        await api.approveResend(connection, ticketId, reason, idempotencyKey);
      }
      setState("done");
      onApproved();
    } catch (err) {
      setState("error");
      setError(
        err instanceof ApiError
          ? err.message
          : isRefund
            ? "Refund failed — nothing was charged twice, safe to retry."
            : "Resend failed — safe to retry, no duplicate order was created.",
      );
    }
  }

  const canSubmit = isRefund ? amount > 0 && !overCap : amount >= 0;

  return (
    <div className="rounded-xl2 border-2 border-gold/40 bg-gold-100/40 dark:bg-gold/5 dark:border-gold/30 p-4">
      <div className="flex items-center gap-2 mb-3">
        <ShieldAlert className="w-4 h-4 text-gold-700 dark:text-gold" />
        <span className="text-sm font-semibold text-gold-700 dark:text-gold">
          AI suggests: {action.type === "refund" ? "Refund" : "Resend order"}
        </span>
      </div>

      {state === "done" ? (
        <div className="flex items-center gap-2 text-sm text-teal-700 dark:text-teal">
          <Check className="w-4 h-4" /> Approved and processed.
        </div>
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Amount</label>
              <input
                type="number" step="0.01" value={amount}
                onChange={(e) => setAmount(parseFloat(e.target.value) || 0)}
                className="w-full rounded-lg border border-line dark:border-line-dark bg-surface dark:bg-surface-dark px-2.5 py-2 text-sm font-mono text-ink-900 dark:text-ink-dark-900 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
              />
            </div>
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Order</label>
              <input
                disabled value={action.order_id ?? "—"}
                className="w-full rounded-lg border border-line dark:border-line-dark bg-ink-900/[0.03] dark:bg-white/[0.03] px-2.5 py-2 text-sm font-mono text-ink-400 dark:text-ink-dark-400"
              />
            </div>
          </div>

          {isRefund && order && (
            <div className="rounded-lg bg-surface/70 dark:bg-white/[0.04] border border-gold/20 px-3 py-2 space-y-2">
              <p className="text-[11px] text-ink-600 dark:text-ink-dark-600">
                Refundable: <span className="font-mono font-semibold">${order.refundable.toFixed(2)}</span>
                <span className="text-ink-400 dark:text-ink-dark-400">
                  {" "}of ${order.total_price.toFixed(2)} total
                  {order.already_refunded > 0 ? ` · $${order.already_refunded.toFixed(2)} already refunded` : ""}
                </span>
              </p>
              {order.line_items.length > 0 && (
                <div className="space-y-1.5">
                  <p className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.1em] text-ink-400 dark:text-ink-dark-400">
                    <Package className="w-3 h-3" /> Scope to items (optional — partial refund)
                  </p>
                  {order.line_items.map((li) => {
                    const checked = li.id in selected;
                    return (
                      <label key={li.id} className="flex items-center gap-2 text-xs text-ink-700 dark:text-ink-dark-700">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleItem(li.id, li.quantity)}
                          className="accent-teal"
                        />
                        <span className="flex-1 truncate">
                          {li.title}
                          <span className="text-ink-400 dark:text-ink-dark-400"> ×{li.quantity} · ${li.price}</span>
                        </span>
                        {checked && (
                          <input
                            type="number"
                            min={1}
                            max={li.quantity}
                            value={selected[li.id]}
                            onChange={(e) => setQty(li.id, parseInt(e.target.value, 10) || 1)}
                            className="w-14 rounded border border-line dark:border-line-dark bg-surface dark:bg-surface-dark px-1.5 py-1 text-xs font-mono text-ink-900 dark:text-ink-dark-900 outline-none"
                          />
                        )}
                      </label>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {overCap && (
            <div className="rounded-lg bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-xs px-3 py-2">
              ${amount.toFixed(2)} exceeds the ${refundable?.toFixed(2)} still refundable on this order.
            </div>
          )}

          <div>
            <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Reason</label>
            <input
              value={reason} onChange={(e) => setReason(e.target.value)}
              className="w-full rounded-lg border border-line dark:border-line-dark bg-surface dark:bg-surface-dark px-2.5 py-2 text-sm text-ink-900 dark:text-ink-dark-900 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
            />
          </div>

          {error && <div className="rounded-lg bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-xs px-3 py-2">{error}</div>}

          {state === "confirming" ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-ink-600 dark:text-ink-dark-600 flex-1">
                {isRefund
                  ? `Refund ${amount.toFixed(2)}${lineItems.length ? ` (scoped to ${lineItems.length} line item(s))` : ""}? This charges the real order.`
                  : "Create and complete a replacement order? This is a real order."}
              </span>
              <button onClick={() => setState("idle")} className="text-xs px-3 py-1.5 rounded-lg text-ink-600 dark:text-ink-dark-600 hover:bg-ink-900/5 dark:hover:bg-white/5">Cancel</button>
              <button onClick={submit} className="text-xs px-3 py-1.5 rounded-lg bg-rose text-white hover:bg-rose-700 font-medium">
                {isRefund ? "Confirm refund" : "Confirm resend"}
              </button>
            </div>
          ) : (
            <button
              onClick={() => setState("confirming")}
              disabled={!canSubmit}
              className="w-full text-xs py-2 rounded-lg bg-gold text-white font-medium hover:bg-gold-700 disabled:opacity-40 transition-colors"
            >
              {state === "submitting" ? "Processing…" : "Approve & send to Shopify"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
