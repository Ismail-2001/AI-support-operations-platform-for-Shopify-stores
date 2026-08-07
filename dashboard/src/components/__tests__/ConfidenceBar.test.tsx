import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ConfidenceBar } from "../ConfidenceBar";

describe("ConfidenceBar", () => {
  it("renders percentage label", () => {
    render(<ConfidenceBar value={0.85} />);
    expect(screen.getByText("85%")).toBeInTheDocument();
  });

  it("rounds percentage correctly", () => {
    render(<ConfidenceBar value={0.847} />);
    expect(screen.getByText("85%")).toBeInTheDocument();
  });

  it("applies teal color for high confidence (>=0.85)", () => {
    const { container } = render(<ConfidenceBar value={0.9} />);
    const bar = container.querySelector(".bg-teal");
    expect(bar).toBeInTheDocument();
  });

  it("applies gold color for medium confidence (0.6-0.85)", () => {
    const { container } = render(<ConfidenceBar value={0.7} />);
    const bar = container.querySelector(".bg-gold");
    expect(bar).toBeInTheDocument();
  });

  it("applies rose color for low confidence (<0.6)", () => {
    const { container } = render(<ConfidenceBar value={0.3} />);
    const bar = container.querySelector(".bg-rose");
    expect(bar).toBeInTheDocument();
  });

  it("hides label when showLabel is false", () => {
    render(<ConfidenceBar value={0.85} showLabel={false} />);
    expect(screen.queryByText("85%")).not.toBeInTheDocument();
  });

  it("applies sm size classes", () => {
    const { container } = render(<ConfidenceBar value={0.85} size="sm" />);
    const track = container.querySelector(".h-1\\.5");
    expect(track).toBeInTheDocument();
  });

  it("applies lg size classes", () => {
    const { container } = render(<ConfidenceBar value={0.85} size="lg" />);
    const track = container.querySelector(".h-2\\.5");
    expect(track).toBeInTheDocument();
  });

  it("renders calibration tick at 85%", () => {
    const { container } = render(<ConfidenceBar value={0.5} />);
    const tick = container.querySelector(".left-\\[85\\%\\]");
    expect(tick).toBeInTheDocument();
  });
});
