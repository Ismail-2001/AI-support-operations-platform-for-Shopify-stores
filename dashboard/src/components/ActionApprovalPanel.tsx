import { useEffect, useState } from "react";
import { Ban, Check, ShieldAlert } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { Connection } from "../lib/api";
import type { SuggestedAction, TicketOrder } from "../lib/types";

type PanelState = "idle" | "confirming" | "submitting" | "done" | "error";

const ADDRESS_FIELDS: { key: string; label: string; required?: boolean }[] = [
  { key: "address1", label: "Address line 1", required: true },
  { key: "address2", label: "Address line 2" },
  { key: "city", label: "City", required: true },
  { key: "province", label: "State / Province" },
  { key: "zip", label: "ZIP / Postal code", required: true },
  { key: "country", label: "Country", required: true },
  { key: "name", label: "Recipient name" },
];

function formatAddress(addr: Record<string, string> | null | undefined): string {
  if (!addr) return "—";
  const line = [addr.address1, addr.address2].filter(Boolean).join(", ");
  const tail = [addr.city, addr.province, addr.zip].filter(Boolean).join(" ");
  const parts = [line, tail, addr.country].filter(Boolean);
  return parts.length ? parts.join(", ") : "—";
}

export function ActionApprovalPanel({
  connection,
  ticketId,
  action,
  onApproved,
}: {
  connection: Connection;
  ticketId: string;
  action: SuggestedAction;
  onApproved: () => void;
}) {
  const isCancel = action.type === "cancel_order";
  const isEditAddress = action.type === "edit_address";

  const [order, setOrder] = useState<TicketOrder | null>(null);
  const [orderError, setOrderError] = useState("");
  const [reason, setReason] = useState(action.reason ?? "");
  const [state, setState] = useState<PanelState>("idle");
  const [error, setError] = useState("");
  const [after, setAfter] = useState<{ previous: Record<string, string>; next: Record<string, string> } | null>(null);
  const [addr, setAddr] = useState<Record<string, string>>({
    address1: action.address?.address1 ?? "",
    address2: action.address?.address2 ?? "",
    city: action.address?.city ?? "",
    province: action.address?.province ?? "",
    zip: action.address?.zip ?? "",
    country: action.address?.country ?? "",
    name: action.address?.name ?? "",
  });

  useEffect(() => {
    api
      .getTicketOrder(connection, ticketId)
      .then(setOrder)
      .catch((e) => setOrderError(e instanceof ApiError ? e.message : "Order lookup unavailable"));
  }, [connection, ticketId]);

  const missingRequired = ADDRESS_FIELDS.filter((f) => f.required && !addr[f.key]?.trim()).map((f) => f.label);
  const orderBlocked = isCancel
    ? !!order?.cancelled_at || (order?.fulfillment_status != null && order.fulfillment_status !== "unfulfilled")
    : !!order?.cancelled_at;
  const blockedReason = order?.cancelled_at
    ? "This order is already cancelled."
    : order?.fulfillment_status && order.fulfillment_status !== "unfulfilled" && order.fulfillment_status !== "partial"
      ? "This order has shipped — it can no longer be changed. Offer a return or refund instead."
      : null;
  const canSubmit = (isCancel || missingRequired.length === 0) && !orderBlocked && reason.trim().length > 0;

  async function submit() {
    setState("submitting");
    setError("");
    try {
      const idempotencyKey = crypto.randomUUID();
      if (isCancel) {
        await api.approveCancel(connection, ticketId, reason, true, idempotencyKey);
        setAfter({ previous: {}, next: {} });
      } else {
        const res = await api.approveEditAddress(connection, ticketId, addr, reason, idempotencyKey);
        setAfter({ previous: res.previous_address ?? {}, next: res.address ?? addr });
      }
      setState("done");
      onApproved();
    } catch (err) {
      setState("error");
      setError(
        err instanceof ApiError
          ? err.message
          : isCancel
            ? "Cancel failed — safe to retry, nothing was cancelled."
            : "Address update failed — safe to retry, the address was not changed.",
      );
    }
  }

  if (!isCancel && !isEditAddress) return null;

  return (
    <div className="rounded-xl2 border-2 border-gold/40 bg-gold-100/40 dark:bg-gold/5 dark:border-gold/30 p-4">
      <div className="flex items-center gap-2 mb-3">
        <ShieldAlert className="w-4 h-4 text-gold-700 dark:text-gold" />
        <span className="text-sm font-semibold text-gold-700 dark:text-gold">
          AI suggests: {isCancel ? "Cancel order" : "Edit shipping address"}
        </span>
      </div>

      {state === "done" ? (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm text-teal-700 dark:text-teal">
            <Check className="w-4 h-4" /> {isCancel ? "Order cancelled." : "Address updated."}
          </div>
          {isEditAddress && after && (
            <div className="text-xs text-ink-600 dark:text-ink-dark-600 grid gap-1">
              <p>
                <span className="font-medium">Before:</span> {formatAddress(after.previous)}
              </p>
              <p>
                <span className="font-medium">After:</span> {formatAddress(after.next)}
              </p>
            </div>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          {orderError && (
            <p className="text-[11px] text-ink-500 dark:text-ink-dark-500">
              Order details unavailable ({orderError}) — the approval below still verifies
              everything server-side before anything changes.
            </p>
          )}

          {order && (
            <div className="rounded-lg bg-surface/70 dark:bg-white/[0.04] border border-gold/20 px-3 py-2 text-xs text-ink-700 dark:text-ink-dark-700 space-y-1">
              <p className="font-mono text-[11px] uppercase tracking-[0.1em] text-ink-400 dark:text-ink-dark-400">
                {order.order_name || order.order_id}
              </p>
              {isCancel ? (
                <p>
                  Fulfillment: <span className="font-medium">{order.fulfillment_status || "unfulfilled"}</span>{" "}
                  · ${order.total_price.toFixed(2)}
                  <span className="text-ink-400 dark:text-ink-dark-400"> · {order.line_items.length} item(s)</span>
                </p>
              ) : (
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <p className="text-[10px] uppercase tracking-[0.1em] text-ink-400 dark:text-ink-dark-400 mb-0.5">
                      Current
                    </p>
                    <p className="leading-snug">{formatAddress(order.shipping_address)}</p>
                  </div>
                  <div>
                    <p className="text-[10px] uppercase tracking-[0.1em] text-teal-700 dark:text-teal mb-0.5">
                      Requested
                    </p>
                    <p className="leading-snug">{formatAddress(addr)}</p>
                  </div>
                </div>
              )}
            </div>
          )}

          {orderBlocked && blockedReason && (
            <div className="rounded-lg bg-rose-100 dark:bg-rose/15 text-rose-700 dark:text-rose text-xs px-3 py-2">
              {blockedReason}
            </div>
          )}

          {isEditAddress && (
            <div className="grid grid-cols-2 gap-2">
              {ADDRESS_FIELDS.map((f) => (
                <div key={f.key} className={f.key === "address2" || f.key === "name" ? "col-span-2" : ""}>
                  <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">
                    {f.label}
                    {f.required && <span className="text-rose"> *</span>}
                  </label>
                  <input
                    value={addr[f.key] ?? ""}
                    onChange={(e) => setAddr({ ...addr, [f.key]: e.target.value })}
                    className="w-full rounded-lg border border-line dark:border-line-dark bg-surface dark:bg-surface-dark px-2.5 py-2 text-sm text-ink-900 dark:text-ink-dark-900 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
                  />
                </div>
              ))}
              {missingRequired.length > 0 && (
                <p className="col-span-2 text-[11px] text-rose-700 dark:text-rose">
                  Required: {missingRequired.join(", ")}
                </p>
              )}
            </div>
          )}

          <div>
            <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">
              {isCancel ? "Reason" : "Reason for the change"}
            </label>
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="w-full rounded-lg border border-line dark:border-line-dark bg-surface dark:bg-surface-dark px-2.5 py-2 text-sm text-ink-900 dark:text-ink-dark-900 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
            />
          </div>

          {isCancel && (
            <p className="text-[11px] text-ink-500 dark:text-ink-dark-500 leading-relaxed">
              <Ban className="w-3 h-3 inline -mt-0.5 mr-1" />
              Cancels the real order in Shopify and restocks its inventory. This cannot be undone.
            </p>
          )}

          {error && (
            <div className="rounded-lg bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-xs px-3 py-2">{error}</div>
          )}

          {state === "confirming" ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-ink-600 dark:text-ink-dark-600 flex-1">
                {isCancel
                  ? "Cancel this order for real? This cannot be undone."
                  : `Update the shipping address to ${formatAddress(addr)}?`}
              </span>
              <button
                onClick={() => setState("idle")}
                className="text-xs px-3 py-1.5 rounded-lg text-ink-600 dark:text-ink-dark-600 hover:bg-ink-900/5 dark:hover:bg-white/5"
              >
                Cancel
              </button>
              <button
                onClick={submit}
                disabled={!canSubmit}
                className="text-xs px-3 py-1.5 rounded-lg bg-gold text-white hover:bg-gold-700 font-medium disabled:opacity-40"
              >
                Confirm
              </button>
            </div>
          ) : (
            <button
              onClick={() => setState("confirming")}
              disabled={!canSubmit || state === "submitting"}
              className="w-full text-xs py-2 rounded-lg bg-gold text-white font-medium hover:bg-gold-700 disabled:opacity-40 transition-colors"
            >
              {state === "submitting"
                ? "Processing…"
                : isCancel
                  ? "Approve cancel"
                  : "Approve address change"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
