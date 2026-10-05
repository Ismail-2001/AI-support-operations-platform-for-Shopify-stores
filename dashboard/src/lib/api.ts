import type {
  AutoSendReport, BrandVoice, CalibrationReport, CostReport, KbSyncStatus, KnowledgeBaseStatus,
  LiveStockVariant, NormalizedSubscription, QualityStats, ReturnEligibility, ReturnLabelResult,
  ReturnRates,
  RoiAssumptions, RoiReport, SetupShopifyResult, SetupStatus,
  SetupTestResult, StoreRecord, StoreSummary, SupportAnalytics, ThresholdSetting, TicketMessage,
  TicketOrder, TicketSubscriptions, TicketWithSuggestion, TraceEntry, WidgetConfig, WidgetSettings,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export interface Connection {
  baseUrl: string;
  apiKey: string;
}

// Active store for the X-Store-Id header. Persisted so a refresh keeps the
// operator on the same store; the backend ignores it on registry endpoints.
const STORE_KEY = "cs_selected_store";

let selectedStoreId: string | null = (() => {
  try {
    return window.localStorage.getItem(STORE_KEY);
  } catch {
    return null;
  }
})();

export function getSelectedStore(): string | null {
  return selectedStoreId;
}

export function setSelectedStore(id: string | null): void {
  selectedStoreId = id;
  try {
    if (id) window.localStorage.setItem(STORE_KEY, id);
    else window.localStorage.removeItem(STORE_KEY);
  } catch {
    /* storage unavailable (private mode) - header still works this session */
  }
}

async function request<T>(conn: Connection, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${conn.baseUrl.replace(/\/$/, "")}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": conn.apiKey,
      ...(selectedStoreId ? { "X-Store-Id": selectedStoreId } : {}),
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.message || body.detail || JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export const api = {
  health: (conn: Connection) => request<{ status: string; shopify_connected: boolean; gorgias_connected: boolean; auto_send_enabled: boolean; storage_persistent: boolean }>(conn, "/support/health"),

  listTickets: (conn: Connection, params?: { status?: string; category?: string; priority?: string; page?: number }) => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.category) q.set("category", params.category);
    if (params?.priority) q.set("priority", params.priority);
    if (params?.page) q.set("page", String(params.page));
    q.set("limit", "50");
    return request<{ tickets: TicketWithSuggestion[]; total: number }>(conn, `/support/tickets?${q.toString()}`);
  },

  getTicket: (conn: Connection, id: string) =>
    request<TicketWithSuggestion>(conn, `/support/tickets/${id}`),

  getThread: (conn: Connection, id: string) =>
    request<{ ticket_id: string; messages: TicketMessage[] }>(conn, `/support/tickets/${id}/messages`),

  getTrace: (conn: Connection, id: string) =>
    request<{ ticket_id: string; trace_count: number; traces: TraceEntry[] }>(conn, `/support/tickets/${id}/trace`),

  respond: (conn: Connection, id: string, response: string, sendViaGorgias: boolean) =>
    request(conn, `/support/tickets/${id}/respond`, {
      method: "POST",
      body: JSON.stringify({ response, send_via_gorgias: sendViaGorgias }),
    }),

  approveRefund: (
    conn: Connection,
    id: string,
    amount: number,
    reason: string,
    idempotencyKey: string,
    refundLineItems?: { line_item_id: number; quantity: number }[],
  ) =>
    request(conn, `/support/tickets/${id}/actions/refund`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        amount,
        reason,
        notify_customer: true,
        ...(refundLineItems && refundLineItems.length > 0
          ? { refund_line_items: refundLineItems }
          : {}),
      }),
    }),

  approveResend: (conn: Connection, id: string, reason: string, idempotencyKey: string) =>
    request(conn, `/support/tickets/${id}/actions/resend-order`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ reason, notify_customer: true }),
    }),

  approveCancel: (conn: Connection, id: string, reason: string, notifyCustomer: boolean, idempotencyKey: string) =>
    request(conn, `/support/tickets/${id}/actions/cancel`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ reason, notify_customer: notifyCustomer }),
    }),

  approveEditAddress: (
    conn: Connection,
    id: string,
    address: Record<string, string>,
    reason: string,
    idempotencyKey: string,
  ) =>
    request<{ previous_address: Record<string, string>; address: Record<string, string> }>(
      conn,
      `/support/tickets/${id}/actions/edit-address`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify({ address, reason }),
      },
    ),

  getTicketOrder: (conn: Connection, id: string) => request<TicketOrder>(conn, `/support/tickets/${id}/order`),

  getSubscriptions: (conn: Connection, id: string) =>
    request<TicketSubscriptions>(conn, `/support/tickets/${id}/subscriptions`),

  approveSubscriptionAction: (
    conn: Connection,
    id: string,
    body: {
      subscription_id: string;
      operation: string;
      provider?: string;
      reason: string;
      address?: Record<string, string>;
      frequency?: { unit?: string | number; count?: number | string };
    },
    idempotencyKey: string,
  ) =>
    request<{ subscription: NormalizedSubscription; operation: string; replayed: boolean }>(
      conn,
      `/support/tickets/${id}/actions/subscription`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify(body),
      },
    ),

  getReturnEligibility: (conn: Connection, id: string) =>
    request<ReturnEligibility>(conn, `/support/tickets/${id}/return-eligibility`),

  /** Live carrier rates for the ticket's order - the operator sees the actual
   * cost (billed to their ShipEngine account) before approving the purchase. */
  getReturnRates: (conn: Connection, id: string) =>
    request<ReturnRates>(conn, `/support/tickets/${id}/return-rates`),

  approveReturnLabel: (
    conn: Connection,
    id: string,
    body: { rma_number?: string; reason?: string; rate_id?: string },
    idempotencyKey: string,
  ) =>
    request<{ label: ReturnLabelResult; replayed: boolean }>(
      conn,
      `/support/tickets/${id}/actions/return-label`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify(body),
      },
    ),

  updateTicket: (conn: Connection, id: string, updates: { status?: string; priority?: string }) =>
    request(conn, `/support/tickets/${id}`, { method: "PATCH", body: JSON.stringify(updates) }),

  getAnalytics: (conn: Connection) => request<SupportAnalytics>(conn, "/support/analytics"),
  getQuality: (conn: Connection) => request<QualityStats>(conn, "/support/analytics/quality"),
  getCalibration: (conn: Connection) => request<CalibrationReport>(conn, "/support/analytics/calibration"),
  getCosts: (conn: Connection, days = 14) => request<CostReport>(conn, `/support/analytics/costs?days=${days}`),
  getAutoSendReport: (conn: Connection) => request<AutoSendReport>(conn, "/support/analytics/auto-send"),
  getThresholds: (conn: Connection) => request<{ thresholds: ThresholdSetting[] }>(conn, "/support/automation/thresholds"),
  updateThreshold: (conn: Connection, category: string, minConfidence: number | null) =>
    request<{ category: string; min_confidence: number; source: string }>(
      conn,
      "/support/automation/thresholds",
      { method: "PUT", body: JSON.stringify({ category, min_confidence: minConfidence }) },
    ),

  kbStatus: (conn: Connection) => request<KnowledgeBaseStatus>(conn, "/support/knowledge-base"),
  kbIngest: (conn: Connection, source: string, title: string, content: string) =>
    request(conn, "/support/knowledge-base", { method: "POST", body: JSON.stringify({ source, title, content }) }),
  /** Starts the background catalog sync and waits for it to finish, reporting
   * progress via onProgress (called on every poll). Throws ApiError if the job
   * errors or takes > ~4 minutes. */
  kbSyncShopify: async (
    conn: Connection,
    opts?: { force?: boolean; onProgress?: (s: KbSyncStatus) => void },
  ): Promise<KbSyncStatus> => {
    const force = opts?.force ?? false;
    await request(conn, `/support/knowledge-base/sync-shopify${force ? "?force=true" : ""}`, { method: "POST" });
    for (let i = 0; i < 360; i++) {
      const s = await request<KbSyncStatus>(conn, "/support/knowledge-base/sync-status");
      opts?.onProgress?.(s);
      if (s.status === "error") throw new ApiError(500, s.error || "Sync failed");
      if (s.status === "idle" && s.started_at) return s;
      await new Promise((r) => setTimeout(r, 650));
    }
    throw new ApiError(408, "Sync is taking longer than expected — reload to check its status.");
  },
  kbSyncStatus: (conn: Connection) => request<KbSyncStatus>(conn, "/support/knowledge-base/sync-status"),
  kbLiveStock: (conn: Connection, handle: string) =>
    request<{ handle: string; title: string; variants: LiveStockVariant[]; checked_at: string }>(
      conn, `/support/knowledge-base/live-stock?handle=${encodeURIComponent(handle)}`,
    ),
  kbSearch: (conn: Connection, query: string) =>
    request<{ query: string; results: { source: string; title: string; content: string; score: number }[] }>(
      conn, "/support/knowledge-base/search", { method: "POST", body: JSON.stringify({ query, top_k: 5 }) }
    ),

  getRoi: (conn: Connection, days: string) => request<RoiReport>(conn, `/support/analytics/roi?days=${days}`),
  updateRoiSettings: (conn: Connection, settings: RoiAssumptions) =>
    request<{ assumptions: RoiAssumptions }>(conn, "/support/analytics/roi/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),

  getWidgetSettings: (conn: Connection) => request<WidgetSettings>(conn, "/support/widget"),
  updateWidgetConfig: (conn: Connection, config: Partial<WidgetConfig>) =>
    request<{ config: WidgetConfig }>(conn, "/support/widget", {
      method: "PUT",
      body: JSON.stringify(config),
    }),
  rotateWidgetKey: (conn: Connection) => request<{ key: string }>(conn, "/support/widget/key", { method: "POST" }),

  setupStatus: (conn: Connection) => request<SetupStatus>(conn, "/support/setup"),

  setupShopify: (conn: Connection, shopDomain: string, accessToken: string) =>
    request<SetupShopifyResult>(conn, "/support/setup/shopify", {
      method: "POST",
      body: JSON.stringify({ shop_domain: shopDomain, access_token: accessToken }),
    }),

  setupVoice: (conn: Connection, voice: BrandVoice) =>
    request<{ voice: BrandVoice }>(conn, "/support/setup/voice", {
      method: "PUT",
      body: JSON.stringify(voice),
    }),

  setupTest: (conn: Connection, question: string) =>
    request<SetupTestResult>(conn, "/support/setup/test", {
      method: "POST",
      body: JSON.stringify({ question }),
    }),

  listStores: (conn: Connection) => request<{ stores: StoreRecord[] }>(conn, "/support/stores"),

  getStoreSummary: (conn: Connection) =>
    request<StoreSummary>(conn, "/support/stores/summary"),

  createStore: (
    conn: Connection,
    input: { name: string; shop_domain?: string; shopify_access_token?: string },
  ) =>
    request<{ store: StoreRecord }>(conn, "/support/stores", {
      method: "POST",
      body: JSON.stringify(input),
    }),

  updateStore: (
    conn: Connection,
    id: string,
    input: { name?: string; shop_domain?: string; shopify_access_token?: string },
  ) =>
    request<{ store: StoreRecord }>(conn, `/support/stores/${id}`, {
      method: "PATCH",
      body: JSON.stringify(input),
    }),

  deleteStore: (conn: Connection, id: string) =>
    request<{ deleted: boolean; store_id: string }>(conn, `/support/stores/${id}`, {
      method: "DELETE",
    }),
};
