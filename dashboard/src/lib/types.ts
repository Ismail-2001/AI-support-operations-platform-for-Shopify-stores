export type TicketCategory =
  | "order_status" | "shipping" | "returns" | "refund"
  | "product_question" | "complaint" | "technical" | "other";

export type TicketPriority = "low" | "normal" | "high" | "urgent" | "critical";
export type TicketStatus = "open" | "in_progress" | "awaiting_customer" | "resolved" | "closed";
export type Sentiment = "very_negative" | "negative" | "neutral" | "positive" | "very_positive";
export type TicketChannel = "email" | "chat" | "gorgias" | "social" | "phone";
export type MessageSender = "customer" | "agent" | "ai";
export type ActionType = "refund" | "resend_order" | "none";

export interface SuggestedAction {
  type: ActionType;
  order_id?: string | null;
  amount?: number | null;
  reason?: string | null;
  requires_approval: boolean;
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
