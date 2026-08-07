import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import {
  PriorityBadge,
  CategoryBadge,
  SentimentBadge,
  SenderBadge,
  AutoSentBadge,
} from "../Badges";

describe("PriorityBadge", () => {
  it("renders priority text", () => {
    render(<PriorityBadge priority="high" />);
    expect(screen.getByText("high")).toBeInTheDocument();
  });

  it("renders dash for null priority", () => {
    render(<PriorityBadge priority={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("applies gold tone for high priority", () => {
    const { container } = render(<PriorityBadge priority="high" />);
    expect(container.querySelector(".bg-gold-100")).toBeInTheDocument();
  });

  it("applies rose tone for critical priority", () => {
    const { container } = render(<PriorityBadge priority="critical" />);
    expect(container.querySelector(".bg-rose-100")).toBeInTheDocument();
  });

  it("applies neutral tone for normal priority", () => {
    const { container } = render(<PriorityBadge priority="normal" />);
    expect(container.querySelector("[class*='bg-ink-900']")).toBeInTheDocument();
  });
});

describe("CategoryBadge", () => {
  it("renders category with underscore replaced by space", () => {
    render(<CategoryBadge category="order_status" />);
    expect(screen.getByText("order status")).toBeInTheDocument();
  });

  it("renders uncategorized for null", () => {
    render(<CategoryBadge category={null} />);
    expect(screen.getByText("uncategorized")).toBeInTheDocument();
  });
});

describe("SentimentBadge", () => {
  it("renders sentiment text", () => {
    render(<SentimentBadge sentiment="positive" />);
    expect(screen.getByText("positive")).toBeInTheDocument();
  });

  it("returns null for null sentiment", () => {
    const { container } = render(<SentimentBadge sentiment={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("applies rose tone for very_negative", () => {
    const { container } = render(<SentimentBadge sentiment="very_negative" />);
    expect(container.querySelector(".bg-rose-100")).toBeInTheDocument();
  });

  it("applies teal tone for very_positive", () => {
    const { container } = render(<SentimentBadge sentiment="very_positive" />);
    expect(container.querySelector(".bg-teal-100")).toBeInTheDocument();
  });
});

describe("SenderBadge", () => {
  it("renders Customer for customer sender", () => {
    render(<SenderBadge sender="customer" />);
    expect(screen.getByText("Customer")).toBeInTheDocument();
  });

  it("renders Human agent for agent sender", () => {
    render(<SenderBadge sender="agent" />);
    expect(screen.getByText("Human agent")).toBeInTheDocument();
  });

  it("renders AI for ai sender", () => {
    render(<SenderBadge sender="ai" />);
    expect(screen.getByText("AI")).toBeInTheDocument();
  });
});

describe("AutoSentBadge", () => {
  it("renders Auto-sent when true", () => {
    render(<AutoSentBadge autoSent={true} />);
    expect(screen.getByText("Auto-sent")).toBeInTheDocument();
  });

  it("renders Awaiting review when false", () => {
    render(<AutoSentBadge autoSent={false} />);
    expect(screen.getByText("Awaiting review")).toBeInTheDocument();
  });

  it("returns null for undefined", () => {
    const { container } = render(<AutoSentBadge autoSent={undefined} />);
    expect(container.firstChild).toBeNull();
  });
});
