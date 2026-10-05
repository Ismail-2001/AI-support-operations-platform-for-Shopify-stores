import { useEffect, useState } from "react";
import { Check, PackageCheck, ShieldAlert, Truck } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { Connection } from "../lib/api";
import type { ReturnEligibility, ReturnLabelResult, ReturnRate, SuggestedAction } from "../lib/types";

type PanelState = "idle" | "confirming" | "submitting" | "done" | "error";

export function ReturnApprovalPanel({
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
  const [eligibility, setEligibility] = useState<ReturnEligibility | null>(null);
  const [loadError, setLoadError] = useState("");
  const [rma, setRma] = useState("");
  const [reason, setReason] = useState(action.reason ?? "");
  const [state, setState] = useState<PanelState>("idle");
  const [error, setError] = useState("");
  const [label, setLabel] = useState<ReturnLabelResult | null>(null);
  const [rates, setRates] = useState<ReturnRate[]>([]);
  const [ratesError, setRatesError] = useState("");
  const [ratesLoading, setRatesLoading] = useState(false);
  const [ratesFetched, setRatesFetched] = useState(false);
  const [selectedRateId, setSelectedRateId] = useState<string | null>(null);

  useEffect(() => {
    api
      .getReturnEligibility(connection, ticketId)
      .then(setEligibility)
      .catch((e) => setLoadError(e instanceof ApiError ? e.message : "Eligibility check unavailable"));
  }, [connection, ticketId]);

  const eligible = eligibility?.eligible ?? false;
  const providerConfigured = eligibility?.label_provider.configured ?? false;
  const missingSettings = eligibility?.label_provider.missing_settings ?? [];

  // Live carrier rates (real money) are shown before the operator approves.
  useEffect(() => {
    if (!eligible || !providerConfigured) return;
    let cancelled = false;
    setRatesLoading(true);
    api
      .getReturnRates(connection, ticketId)
      .then((res) => {
        if (cancelled) return;
        setRates(res.rates ?? []);
        setSelectedRateId(res.cheapest_rate_id ?? res.rates?.[0]?.rate_id ?? null);
        setRatesFetched(true);
      })
      .catch((e) => {
        if (cancelled) return;
        setRatesError(
          e instanceof ApiError ? e.message : "Live rate lookup failed",
        );
        setRatesFetched(true);
      })
      .finally(() => {
        if (!cancelled) setRatesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [connection, ticketId, eligible, providerConfigured]);

  const selectedRate = rates.find((r) => r.rate_id === selectedRateId) ?? null;
  const blocked = !eligibility || !eligible || !providerConfigured;
  // Rates definitively came back empty: the purchase would fail anyway, so block early.
  const ratesEmptyKnown =
    ratesFetched && !ratesLoading && !ratesError && eligible && providerConfigured && rates.length === 0;
  const canSubmit = !blocked && !ratesEmptyKnown && state !== "submitting";

  async function submit() {
    setState("submitting");
    setError("");
    try {
      const idempotencyKey = crypto.randomUUID();
      const res = await api.approveReturnLabel(
        connection,
        ticketId,
        {
          ...(rma.trim() ? { rma_number: rma.trim() } : {}),
          ...(reason.trim() ? { reason } : {}),
          ...(selectedRateId ? { rate_id: selectedRateId } : {}),
        },
        idempotencyKey,
      );
      setLabel(res.label ?? null);
      setState("done");
      onApproved();
    } catch (err) {
      setState("error");
      setError(
        err instanceof ApiError
          ? err.message
          : "Return label failed — safe to retry, no label was purchased.",
      );
    }
  }

  if (action.type !== "return_label") return null;

  return (
    <div className="rounded-xl2 border-2 border-gold/40 bg-gold-100/40 dark:bg-gold/5 dark:border-gold/30 p-4">
      <div className="flex items-center gap-2 mb-3">
        <ShieldAlert className="w-4 h-4 text-gold-700 dark:text-gold" />
        <span className="text-sm font-semibold text-gold-700 dark:text-gold">
          AI suggests: Create prepaid return label
        </span>
      </div>

      {state === "done" ? (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm text-teal-700 dark:text-teal">
            <Check className="w-4 h-4" /> Return label created.
          </div>
          {label && (
            <div className="text-xs text-ink-600 dark:text-ink-dark-600 grid gap-1">
              <p>
                <span className="font-medium">Tracking:</span> {label.tracking_number ?? "—"}
                {label.carrier && (
                  <span className="text-ink-400 dark:text-ink-dark-400"> · {label.carrier}</span>
                )}
                {label.cost_usd != null && (
                  <span className="text-ink-400 dark:text-ink-dark-400"> · ${label.cost_usd.toFixed(2)}</span>
                )}
              </p>
              {label.label_url && (
                <a
                  href={label.label_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-teal-700 dark:text-teal underline underline-offset-2"
                >
                  Open label PDF
                </a>
              )}
            </div>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          {loadError && (
            <div className="rounded-lg bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-xs px-3 py-2">
              Eligibility check failed ({loadError}) — cannot approve a label without it.
            </div>
          )}

          {eligibility && !eligible && (
            <div className="rounded-lg bg-rose-100 dark:bg-rose/15 text-rose-700 dark:text-rose text-xs px-3 py-2">
              {eligibility.reason}
            </div>
          )}

          {eligible && !providerConfigured && (
            <div className="rounded-lg bg-rose-100 dark:bg-rose/15 text-rose-700 dark:text-rose text-xs px-3 py-2">
              Return shipping is not configured
              {missingSettings.length > 0 && ` (missing: ${missingSettings.join(", ")})`} — fill these
              in Settings before buying labels.
            </div>
          )}

          {eligibility && eligible && (
            <div className="rounded-lg bg-surface/70 dark:bg-white/[0.04] border border-gold/20 px-3 py-2 text-xs text-ink-700 dark:text-ink-dark-700 space-y-1">
              <p className="font-mono text-[11px] uppercase tracking-[0.1em] text-ink-400 dark:text-ink-dark-400">
                {eligibility.order_id ?? "no order"}
              </p>
              <p>
                Return window: <span className="font-medium">{eligibility.window_days} days</span>
                {eligibility.last_return_date && (
                  <span className="text-ink-400 dark:text-ink-dark-400">
                    {" "}
                    · through {eligibility.last_return_date}
                  </span>
                )}
              </p>
              <p>
                <Truck className="w-3 h-3 inline -mt-0.5 mr-1" />
                {eligibility.line_items.length} item(s) eligible for return
              </p>
              {action.return_line_items && action.return_line_items.length > 0 && (
                <p>
                  <span className="text-ink-400 dark:text-ink-dark-400">AI requested: </span>
                  {action.return_line_items
                    .map((li) => `${li.title ?? `item ${li.line_item_id}`} ×${li.quantity ?? 1}`)
                    .join(", ")}
                </p>
              )}
            </div>
          )}

          {eligible && providerConfigured && (
            <div className="rounded-lg bg-surface/70 dark:bg-white/[0.04] border border-gold/20 px-3 py-2 text-xs text-ink-700 dark:text-ink-dark-700 space-y-1.5">
              <p className="font-mono text-[11px] uppercase tracking-[0.1em] text-ink-400 dark:text-ink-dark-400">
                Carrier cost before purchase
              </p>
              {ratesLoading && (
                <p className="text-ink-500 dark:text-ink-dark-500">Fetching live rates…</p>
              )}
              {ratesError && (
                <p className="text-amber-700 dark:text-amber">
                  Rates unavailable ({ratesError}) — approving buys the cheapest available rate
                  server-side.
                </p>
              )}
              {!ratesLoading && !ratesError && rates.length === 0 && (
                <p className="text-rose-700 dark:text-rose">
                  No carrier rates available for this route — approving is blocked until rates show
                  up.
                </p>
              )}
              {rates.map((rate) => (
                <label
                  key={rate.rate_id}
                  className="flex items-center gap-2 cursor-pointer rounded px-1.5 py-1 hover:bg-ink-900/5 dark:hover:bg-white/5"
                >
                  <input
                    type="radio"
                    name="return-rate"
                    value={rate.rate_id}
                    checked={selectedRateId === rate.rate_id}
                    onChange={() => setSelectedRateId(rate.rate_id)}
                    className="accent-teal"
                  />
                  <span className="font-medium">${rate.amount.toFixed(2)}</span>
                  <span>
                    {rate.carrier} · {rate.service}
                  </span>
                  {rate.rate_id === (rates[0]?.rate_id ?? "") && rates.length > 1 && (
                    <span className="text-[10px] uppercase tracking-wide text-ink-400 dark:text-ink-dark-400">
                      cheapest
                    </span>
                  )}
                </label>
              ))}
              {rates.length > 0 && (
                <p className="text-[11px] text-ink-400 dark:text-ink-dark-400">
                  Billed to your carrier account when you confirm.
                </p>
              )}
            </div>
          )}

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">
                RMA number (optional)
              </label>
              <input
                value={rma}
                onChange={(e) => setRma(e.target.value)}
                placeholder={`T-${ticketId.slice(0, 12)}`}
                className="w-full rounded-lg border border-line dark:border-line-dark bg-surface dark:bg-surface-dark px-2.5 py-2 text-sm text-ink-900 dark:text-ink-dark-900 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
              />
            </div>
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
          </div>

          <p className="text-[11px] text-ink-500 dark:text-ink-dark-500 leading-relaxed flex items-start gap-1">
            <PackageCheck className="w-3 h-3 inline mt-0.5 shrink-0" />
            Purchases a real shipping label (costs money) and moves the ticket to “awaiting
            customer”. Send the label link to the customer in your reply.
          </p>

          {error && (
            <div className="rounded-lg bg-rose-100 dark:bg-rose/20 text-rose-700 dark:text-rose text-xs px-3 py-2">
              {error}
            </div>
          )}

          {state === "confirming" ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-ink-600 dark:text-ink-dark-600 flex-1">
                {selectedRate
                  ? `Buy this label for ${selectedRate.carrier} at $${selectedRate.amount.toFixed(2)}? This charges your carrier account.`
                  : "Buy a real return label now? This charges your carrier account."}
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
              {state === "submitting"
                ? "Processing…"
                : blocked
                  ? "Return label unavailable"
                  : ratesEmptyKnown
                    ? "No carrier rates"
                    : "Approve return label"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
