import { useEffect, useState } from "react";
import { Check, CreditCard, PauseCircle, Repeat, ShieldAlert, SkipForward, XCircle } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { Connection } from "../lib/api";
import type { NormalizedSubscription, SuggestedAction, SubscriptionOperation } from "../lib/types";

type PanelState = "idle" | "confirming" | "submitting" | "done" | "error";

const OPERATION_LABELS: Record<SubscriptionOperation, string> = {
  pause: "Pause subscription",
  skip: "Skip next order",
  cancel: "Cancel subscription",
  update_address: "Update shipping address",
  change_frequency: "Change delivery frequency",
};

const OPERATION_ICONS: Record<SubscriptionOperation, typeof PauseCircle> = {
  pause: PauseCircle,
  skip: SkipForward,
  cancel: XCircle,
  update_address: Repeat,
  change_frequency: Repeat,
};

const FREQUENCY_UNITS = ["day", "week", "month"];

const ADDRESS_FIELDS: { key: string; label: string; required?: boolean }[] = [
  { key: "address1", label: "Address line 1", required: true },
  { key: "address2", label: "Address line 2" },
  { key: "city", label: "City", required: true },
  { key: "state", label: "State / Province" },
  { key: "zip", label: "ZIP / Postal code", required: true },
  { key: "country", label: "Country", required: true },
  { key: "name", label: "Recipient name" },
];

export function SubscriptionApprovalPanel({
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
  const operation = action.subscription_operation;
  const subscriptionId = action.subscription_id ?? "";
  const Icon = operation ? OPERATION_ICONS[operation] : ShieldAlert;

  const [subs, setSubs] = useState<NormalizedSubscription[]>([]);
  const [configured, setConfigured] = useState(true);
  const [provider, setProvider] = useState<string | null>(action.subscription_provider ?? null);
  const [loadError, setLoadError] = useState("");
  const [reason, setReason] = useState(action.reason ?? "");
  const [state, setState] = useState<PanelState>("idle");
  const [error, setError] = useState("");
  const [after, setAfter] = useState<NormalizedSubscription | null>(null);

  const [unit, setUnit] = useState(String(action.frequency?.unit ?? "month"));
  const [count, setCount] = useState(String(action.frequency?.count ?? 1));
  const [addr, setAddr] = useState<Record<string, string>>({
    address1: action.address?.address1 ?? "",
    address2: action.address?.address2 ?? "",
    city: action.address?.city ?? "",
    state: action.address?.state ?? action.address?.province ?? "",
    zip: action.address?.zip ?? "",
    country: action.address?.country ?? "",
    name: action.address?.name ?? "",
  });

  useEffect(() => {
    api
      .getSubscriptions(connection, ticketId)
      .then((res) => {
        setSubs(res.subscriptions);
        setConfigured(res.configured);
        if (res.provider) setProvider(res.provider);
      })
      .catch((e) => setLoadError(e instanceof ApiError ? e.message : "Subscription lookup unavailable"));
  }, [connection, ticketId]);

  const sub = subs.find((s) => s.id === subscriptionId) ?? null;

  const missingRequired =
    operation === "update_address"
      ? ADDRESS_FIELDS.filter((f) => f.required && !addr[f.key]?.trim()).map((f) => f.label)
      : [];
  const freqCount = Number(count);
  const freqInvalid =
    operation === "change_frequency" &&
    (!FREQUENCY_UNITS.includes(unit) || !Number.isFinite(freqCount) || freqCount < 1 || freqCount > 60);
  const canSubmit =
    !!operation && !!subscriptionId && configured && missingRequired.length === 0 && !freqInvalid &&
    reason.trim().length > 0 && state !== "submitting";

  async function submit() {
    setState("submitting");
    setError("");
    try {
      const idempotencyKey = crypto.randomUUID();
      const res = await api.approveSubscriptionAction(
        connection,
        ticketId,
        {
          subscription_id: subscriptionId,
          operation: operation as string,
          ...(provider ? { provider } : {}),
          reason,
          ...(operation === "update_address" ? { address: addr } : {}),
          ...(operation === "change_frequency" ? { frequency: { unit, count: freqCount } } : {}),
        },
        idempotencyKey,
      );
      setAfter(res.subscription ?? null);
      setState("done");
      onApproved();
    } catch (err) {
      setState("error");
      setError(
        err instanceof ApiError
          ? err.message
          : "Subscription action failed — safe to retry, nothing was changed.",
      );
    }
  }

  if (action.type !== "subscription_action") return null;

  const opLabel = operation ? OPERATION_LABELS[operation] : "Subscription change";

  return (
    <div className="rounded-xl2 border-2 border-gold/40 bg-gold-100/40 dark:bg-gold/5 dark:border-gold/30 p-4">
      <div className="flex items-center gap-2 mb-3">
        <ShieldAlert className="w-4 h-4 text-gold-700 dark:text-gold" />
        <span className="text-sm font-semibold text-gold-700 dark:text-gold">AI suggests: {opLabel}</span>
        {provider && (
          <span className="text-[10px] uppercase tracking-[0.1em] px-1.5 py-0.5 rounded bg-surface/80 dark:bg-white/[0.06] text-ink-500 dark:text-ink-dark-500">
            {provider}
          </span>
        )}
      </div>

      {state === "done" ? (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm text-teal-700 dark:text-teal">
            <Check className="w-4 h-4" /> {opLabel} applied.
          </div>
          {after && (
            <div className="text-xs text-ink-600 dark:text-ink-dark-600 grid gap-1">
              <p>
                <span className="font-medium">Status:</span> {after.status}
              </p>
              {after.next_charge_date && (
                <p>
                  <span className="font-medium">Next charge:</span> {String(after.next_charge_date).slice(0, 10)}
                </p>
              )}
              {after.frequency_unit && (
                <p>
                  <span className="font-medium">Frequency:</span> every {after.frequency_count} {after.frequency_unit}
                </p>
              )}
            </div>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          {!operation && (
            <div className="rounded-lg bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-xs px-3 py-2">
              The AI did not specify which subscription operation to run — nothing can be approved.
            </div>
          )}
          {!subscriptionId && operation && (
            <div className="rounded-lg bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-xs px-3 py-2">
              The AI did not identify which subscription to change.
            </div>
          )}

          {loadError && (
            <p className="text-[11px] text-ink-500 dark:text-ink-dark-500">
              Subscription details unavailable ({loadError}) — the approval below still verifies
              everything server-side before anything changes.
            </p>
          )}

          {!configured && (
            <div className="rounded-lg bg-rose-100 dark:bg-rose/15 text-rose-700 dark:text-rose text-xs px-3 py-2">
              No subscription app (Recharge / Skio) is connected — connect one in Settings before
              managing subscriptions.
            </div>
          )}

          {configured && sub && (
            <div className="rounded-lg bg-surface/70 dark:bg-white/[0.04] border border-gold/20 px-3 py-2 text-xs text-ink-700 dark:text-ink-dark-700 space-y-1">
              <p className="font-mono text-[11px] uppercase tracking-[0.1em] text-ink-400 dark:text-ink-dark-400">
                {sub.id}
              </p>
              <p className="font-medium">{sub.title}</p>
              <p>
                Status: <span className="font-medium">{sub.status}</span>
                {sub.quantity != null && (
                  <span className="text-ink-400 dark:text-ink-dark-400"> · qty {sub.quantity}</span>
                )}
                {sub.price && <span className="text-ink-400 dark:text-ink-dark-400"> · ${sub.price}</span>}
              </p>
              <p>
                Next charge: <span className="font-medium">{sub.next_charge_date?.slice(0, 10) ?? "—"}</span>
                {sub.frequency_unit && (
                  <span className="text-ink-400 dark:text-ink-dark-400">
                    {" "}
                    · every {sub.frequency_count} {sub.frequency_unit}
                  </span>
                )}
              </p>
            </div>
          )}

          {configured && subscriptionId && !sub && !loadError && (
            <p className="text-[11px] text-ink-500 dark:text-ink-dark-500">
              Subscription {subscriptionId} is not in this customer's current list — the server will
              confirm it exists before anything changes.
            </p>
          )}

          {operation === "change_frequency" && (
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">
                  Every
                </label>
                <input
                  type="number"
                  min={1}
                  max={60}
                  value={count}
                  onChange={(e) => setCount(e.target.value)}
                  className="w-full rounded-lg border border-line dark:border-line-dark bg-surface dark:bg-surface-dark px-2.5 py-2 text-sm text-ink-900 dark:text-ink-dark-900 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
                />
              </div>
              <div>
                <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">
                  Unit
                </label>
                <select
                  value={unit}
                  onChange={(e) => setUnit(e.target.value)}
                  className="w-full rounded-lg border border-line dark:border-line-dark bg-surface dark:bg-surface-dark px-2.5 py-2 text-sm text-ink-900 dark:text-ink-dark-900 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
                >
                  {FREQUENCY_UNITS.map((u) => (
                    <option key={u} value={u}>
                      {u}(s)
                    </option>
                  ))}
                </select>
              </div>
              {freqInvalid && (
                <p className="col-span-2 text-[11px] text-rose-700 dark:text-rose">
                  Count must be 1-60 and the unit day, week, or month.
                </p>
              )}
            </div>
          )}

          {operation === "update_address" && (
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
              Reason
            </label>
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="w-full rounded-lg border border-line dark:border-line-dark bg-surface dark:bg-surface-dark px-2.5 py-2 text-sm text-ink-900 dark:text-ink-dark-900 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
            />
          </div>

          <p className="text-[11px] text-ink-500 dark:text-ink-dark-500 leading-relaxed flex items-start gap-1">
            <Icon className="w-3 h-3 inline mt-0.5 shrink-0" />
            {operation === "pause" && "Pushes the next charge out — the subscription resumes automatically."}
            {operation === "skip" && "Skips the next scheduled order; billing continues after that."}
            {operation === "cancel" && "Cancels the subscription permanently. This cannot be undone."}
            {operation === "update_address" && "Changes where future subscription orders are shipped."}
            {operation === "change_frequency" && "Changes how often future orders are billed and shipped."}
          </p>

          {error && (
            <div className="rounded-lg bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-xs px-3 py-2">
              {error}
            </div>
          )}

          {state === "confirming" ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-ink-600 dark:text-ink-dark-600 flex-1">
                {operation === "cancel"
                  ? "Cancel this subscription for real? This cannot be undone."
                  : `Apply “${opLabel}” to subscription ${subscriptionId}?`}
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
              disabled={!canSubmit}
              className="w-full text-xs py-2 rounded-lg bg-gold text-white font-medium hover:bg-gold-700 disabled:opacity-40 transition-colors"
            >
              {state === "submitting" ? "Processing…" : `Approve ${opLabel.toLowerCase()}`}
            </button>
          )}

          {operation === "cancel" && (
            <p className="text-[11px] text-ink-500 dark:text-ink-dark-500 flex items-center gap-1">
              <CreditCard className="w-3 h-3" /> Billing stops immediately after cancellation.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
