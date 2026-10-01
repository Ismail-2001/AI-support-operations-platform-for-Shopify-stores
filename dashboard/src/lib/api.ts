import type {
  BrandVoice, CalibrationReport, CostReport, KnowledgeBaseStatus, QualityStats,
  SetupShopifyResult, SetupStatus, SetupTestResult, SupportAnalytics, TicketMessage,
  TicketWithSuggestion, TraceEntry,
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

async function request<T>(conn: Connection, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${conn.baseUrl.replace(/\/$/, "")}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": conn.apiKey,
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export const api = {
  health: (conn: Connection) => request<{ status: string; shopify_connected: boolean; gorgias_connected: boolean; auto_send_enabled: boolean }>(conn, "/support/health"),

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

  approveRefund: (conn: Connection, id: string, amount: number, reason: string, idempotencyKey: string) =>
    request(conn, `/support/tickets/${id}/actions/refund`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ amount, reason, notify_customer: true }),
    }),

  updateTicket: (conn: Connection, id: string, updates: { status?: string; priority?: string }) =>
    request(conn, `/support/tickets/${id}`, { method: "PATCH", body: JSON.stringify(updates) }),

  getAnalytics: (conn: Connection) => request<SupportAnalytics>(conn, "/support/analytics"),
  getQuality: (conn: Connection) => request<QualityStats>(conn, "/support/analytics/quality"),
  getCalibration: (conn: Connection) => request<CalibrationReport>(conn, "/support/analytics/calibration"),
  getCosts: (conn: Connection, days = 14) => request<CostReport>(conn, `/support/analytics/costs?days=${days}`),

  kbStatus: (conn: Connection) => request<KnowledgeBaseStatus>(conn, "/support/knowledge-base"),
  kbIngest: (conn: Connection, source: string, title: string, content: string) =>
    request(conn, "/support/knowledge-base", { method: "POST", body: JSON.stringify({ source, title, content }) }),
  kbSyncShopify: (conn: Connection) =>
    request<{ status: string; total_chunks: number }>(conn, "/support/knowledge-base/sync-shopify", { method: "POST" }),
  kbSearch: (conn: Connection, query: string) =>
    request<{ query: string; results: { source: string; title: string; content: string; score: number }[] }>(
      conn, "/support/knowledge-base/search", { method: "POST", body: JSON.stringify({ query, top_k: 5 }) }
    ),

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
};
