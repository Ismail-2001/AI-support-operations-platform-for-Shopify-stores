export type TicketCategory =
  | "order_status" | "shipping" | "returns" | "refund"
  | "product_question" | "complaint" | "technical" | "subscription" | "other";

export type TicketPriority = "low" | "normal" | "high" | "urgent" | "critical";
export type TicketStatus = "open" | "in_progress" | "awaiting_customer" | "resolved" | "closed";
export type Sentiment = "very_negative" | "negative" | "neutral" | "positive" | "very_positive";
export type TicketChannel = "email" | "chat" | "gorgias" | "social" | "phone";
export type MessageSender = "customer" | "agent" | "ai";
export type ActionType =
  | "refund" | "resend_order" | "cancel_order" | "edit_address"
  | "subscription_action" | "return_label" | "none";

export type SubscriptionOperation =
  | "pause" | "skip" | "cancel" | "update_address" | "change_frequency";

export interface SuggestedAction {
  type: ActionType;
  order_id?: string | null;
  amount?: number | null;
  reason?: string | null;
  address?: Record<string, string> | null;
  refund_line_items?: { line_item_id: number; quantity: number }[] | null;
  subscription_id?: string | null;
  subscription_provider?: string | null;
  subscription_operation?: SubscriptionOperation | null;
  frequency?: { unit?: string | number; count?: number | string } | null;
  return_line_items?: { line_item_id?: number; quantity?: number; title?: string }[] | null;
  requires_approval: boolean;
}

export interface NormalizedSubscription {
  id: string;
  provider: "recharge" | "skio";
  status: string;
  title: string;
  quantity?: number | null;
  price?: string | null;
  next_charge_date?: string | null;
  frequency_unit?: string | null;
  frequency_count?: number | null;
  address?: Record<string, string> | null;
  email?: string | null;
}

export interface TicketSubscriptions {
  ticket_id: string;
  email: string;
  configured: boolean;
  provider: string | null;
  subscriptions: NormalizedSubscription[];
}

export interface ReturnEligibility {
  ticket_id: string;
  order_id: string | null;
  eligible: boolean;
  reason: string | null;
  window_days: number;
  last_return_date: string | null;
  line_items: {
    line_item_id?: number;
    title?: string;
    variant_title?: string | null;
    quantity?: number;
    sku?: string | null;
  }[];
  label_provider: {
    provider: string;
    configured: boolean;
    missing_settings: string[];
  };
}

export interface ReturnLabelResult {
  label_id?: string | null;
  label_url?: string | null;
  tracking_number?: string | null;
  tracking_url?: string | null;
  carrier?: string | null;
  service_code?: string | null;
  cost_usd?: number | null;
  status?: string | null;
  rma_number?: string | null;
}

export interface OrderLineItem {
  id: number;
  title: string;
  variant_title?: string | null;
  quantity: number;
  price: string;
}

export interface TicketOrder {
  ticket_id: string;
  order_id: string;
  order_name?: string | null;
  total_price: number;
  currency?: string | null;
  financial_status?: string | null;
  fulfillment_status?: string | null;
  cancelled_at?: string | null;
  shipping_address: Record<string, string>;
  line_items: OrderLineItem[];
  already_refunded: number;
  refundable: number;
}

export interface ThresholdSetting {
  category: string;
  min_confidence: number;
  source: "settings" | "runtime_override";
  env_default: number;
}

export interface AutoSendCategoryReport {
  current_threshold: number;
  suggested_threshold: number;
  recommendation: string;
  reviewed_samples: number;
  edited_samples: number;
  edit_rate: number | null;
}

export interface AutoSendReport {
  auto_send_enabled: boolean;
  categories: Record<string, AutoSendCategoryReport>;
  blocked_categories: string[];
  daily_cost_cap_usd: number;
  min_samples_for_recommendation: number;
  absolute_min_threshold: number;
}

export interface Ticket {
  id: string;
  shop_domain?: string | null;
  customer_email: string;
  customer_name?: string | null;
  subject: string;
  body: string;
  channel: TicketChannel;
  order_id?: string | null;
  order_number?: string | null;
  gorgias_ticket_id?: string | null;
  status: TicketStatus;
  category?: TicketCategory | null;
  priority?: TicketPriority | null;
  sentiment?: Sentiment | null;
  created_at: string;
}

export interface ResponseSuggestion {
  ticket_id: string;
  suggested_response: string;
  confidence: number;
  reasoning: string;
  requires_human_review: boolean;
  follow_up_questions: string[];
  suggested_action?: SuggestedAction | null;
}

export interface TicketWithSuggestion extends Ticket {
  suggestion?: ResponseSuggestion | null;
  auto_sent?: boolean;
}

export interface TicketMessage {
  id: number;
  ticket_id: string;
  sender_type: MessageSender;
  content: string;
  created_at: string;
}

export interface SupportAnalytics {
  total_tickets: number;
  open_tickets: number;
  first_contact_resolution_rate?: number | null;
  category_breakdown: Record<string, number>;
  priority_breakdown: Record<string, number>;
  channel_breakdown: Record<string, number>;
  sentiment_distribution: Record<string, number>;
}

export interface QualityStats {
  total_ai_drafts_sent: number;
  edited_before_send: number;
  overall_edit_rate: number | null;
  by_category: Record<string, { total: number; edited: number; avg_similarity: number; edit_rate: number }>;
}

export interface CalibrationBucket {
  count: number;
  edit_rate: number | null;
}

export interface CalibrationReport {
  buckets: Record<string, CalibrationBucket>;
  interpretation: string;
  sample_size_warning: string | null;
}

export interface CostReport {
  today_usd: number;
  by_day: { date: string; cost_usd: number; calls: number }[];
  by_stage: { stage: string; cost_usd: number; calls: number }[];
}

export interface TraceEntry {
  stage: string;
  model: string | null;
  input_summary: Record<string, unknown>;
  output_summary: Record<string, unknown>;
  latency_ms: number | null;
  tokens_input: number | null;
  tokens_output: number | null;
  cost_usd: number | null;
  created_at: string;
}

export interface KnowledgeBaseStatus {
  chunk_count: number;
}

export interface BrandVoice {
  store_name: string;
  tone: "friendly" | "professional" | "casual";
  sign_off: string;
  support_email: string;
}

export interface SetupStatus {
  shopify: { connected: boolean; domain: string | null };
  knowledge_base: { chunk_count: number };
  voice: BrandVoice;
  voice_set: boolean;
  google_key_set: boolean;
  test_done: boolean;
  steps: { shopify: boolean; policies: boolean; voice: boolean; test: boolean };
  setup_complete: boolean;
}

export interface SetupShopifyResult {
  connected: boolean;
  domain: string;
  shop_name: string | null;
  shop_email: string | null;
  currency: string | null;
}

export interface SetupTestResult {
  classification: {
    category: string;
    priority: string;
    sentiment: string;
    reasoning: string;
  };
  suggestion: ResponseSuggestion;
  order_context_used: boolean;
  kb_used?: boolean;
}

// ── Week 3-4: chat widget, ROI dashboard, product knowledge ──

export interface KbSyncStatus {
  status: "idle" | "scheduled" | "running" | "error";
  force: boolean;
  products_seen: number;
  products_updated: number;
  products_skipped: number;
  products_failed: number;
  policies_updated: number;
  chunks_added: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface LiveStockVariant {
  variant: string;
  quantity: number | null;
  status: "in_stock" | "low_stock" | "out_of_stock" | "unknown";
}

export interface RoiAssumptions {
  minutes_per_auto_sent: number;
  minutes_per_draft: number;
  hourly_rate_usd: number;
}

export interface RoiSeriesPoint {
  date: string;
  tickets: number;
  auto_sent: number;
  hours_saved: number;
  labor_saved_usd: number;
  llm_cost_usd: number;
  net_usd: number;
}

export interface RoiReport {
  window: {
    total: number;
    auto_sent: number;
    responded: number;
    edited: number;
    drafts_reviewed: number;
    llm_cost_usd: number;
  };
  assumptions: RoiAssumptions;
  hours_saved: number;
  labor_saved_usd: number;
  net_savings_usd: number;
  roi_percent: number | null;
  cost_per_ticket_usd: number | null;
  draft_edit_rate: number;
  by_channel: Record<string, number>;
  category_tickets: Record<string, number>;
  category_edits: Record<string, { total: number; edited: number }>;
  series: RoiSeriesPoint[];
  days: string;
  since: string | null;
}

export interface WidgetConfig {
  enabled: boolean;
  title: string;
  greeting: string;
  welcome_message: string;
  color: string;
  logo_url: string;
  show_confidence: boolean;
}

export interface WidgetSettings {
  key: string;
  config: WidgetConfig;
}
