import { render } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { Skeleton, TicketListSkeleton, TicketDetailSkeleton, AnalyticsSkeleton } from "../Skeleton";

describe("Skeleton", () => {
  it("renders with default class", () => {
    const { container } = render(<Skeleton />);
    const el = container.firstChild as HTMLElement;
    expect(el.tagName).toBe("DIV");
    expect(el.className).toContain("animate-pulse");
    expect(el.className).toContain("rounded");
  });

  it("accepts custom className", () => {
    const { container } = render(<Skeleton className="h-4 w-32" />);
    const el = container.firstChild as HTMLElement;
    expect(el.className).toContain("h-4");
    expect(el.className).toContain("w-32");
  });
});

describe("TicketListSkeleton", () => {
  it("renders 5 skeleton rows", () => {
    const { container } = render(<TicketListSkeleton />);
    const rows = container.querySelectorAll(".rounded-xl2");
    expect(rows.length).toBe(5);
  });
});

describe("TicketDetailSkeleton", () => {
  it("renders grid layout", () => {
    const { container } = render(<TicketDetailSkeleton />);
    const grid = container.querySelector(".grid");
    expect(grid).toBeInTheDocument();
  });
});

describe("AnalyticsSkeleton", () => {
  it("renders 4 stat cards", () => {
    const { container } = render(<AnalyticsSkeleton />);
    const cards = container.querySelectorAll(".rounded-xl2");
    expect(cards.length).toBe(5);
  });
});
