import type { TicketCategory, TicketPriority, Sentiment, MessageSender } from "../lib/types";

function Chip({ children, tone }: { children: React.ReactNode; tone: "gold" | "teal" | "rose" | "violet" | "neutral" }) {
  const tones: Record<string, string> = {
    gold: "bg-gold-100 text-gold-700 dark:bg-gold/20 dark:text-gold",
    teal: "bg-teal-100 text-teal-700 dark:bg-teal/20 dark:text-teal",
    rose: "bg-rose-100 text-rose-700 dark:bg-rose/20 dark:text-rose",
    violet: "bg-violet-100 text-violet-700 dark:bg-violet/20 dark:text-violet",
    neutral: "bg-ink-900/[0.05] text-ink-600 dark:bg-white/[0.05] dark:text-ink-dark-600",
  };
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium tracking-wide ${tones[tone]}`}>
      {children}
    </span>
  );
}

const PRIORITY_TONE: Record<TicketPriority, "gold" | "teal" | "rose" | "neutral"> = {
  low: "neutral", normal: "neutral", high: "gold", urgent: "rose", critical: "rose",
};

export function PriorityBadge({ priority }: { priority?: TicketPriority | null }) {
  if (!priority) return <Chip tone="neutral">—</Chip>;
  return <Chip tone={PRIORITY_TONE[priority]}>{priority}</Chip>;
}

export function CategoryBadge({ category }: { category?: TicketCategory | null }) {
  if (!category) return <Chip tone="neutral">uncategorized</Chip>;
  return <Chip tone="neutral">{category.replace("_", " ")}</Chip>;
}

const SENTIMENT_TONE: Record<Sentiment, "gold" | "teal" | "rose" | "neutral"> = {
  very_negative: "rose", negative: "rose", neutral: "neutral", positive: "teal", very_positive: "teal",
};

export function SentimentBadge({ sentiment }: { sentiment?: Sentiment | null }) {
  if (!sentiment) return null;
  return <Chip tone={SENTIMENT_TONE[sentiment]}>{sentiment.replace("_", " ")}</Chip>;
}

export function SenderBadge({ sender }: { sender: MessageSender }) {
  const map = { customer: ["neutral", "Customer"], agent: ["gold", "Human agent"], ai: ["violet", "AI"] } as const;
  const [tone, label] = map[sender];
  return <Chip tone={tone}>{label}</Chip>;
}

export function AutoSentBadge({ autoSent }: { autoSent?: boolean }) {
  if (autoSent === undefined) return null;
  return autoSent
    ? <Chip tone="violet">Auto-sent</Chip>
    : <Chip tone="gold">Awaiting review</Chip>;
}
