import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ConnectScreen } from "../ConnectScreen";

describe("ConnectScreen", () => {
  it("renders connection form", () => {
    render(<ConnectScreen onConnect={vi.fn()} />);
    expect(screen.getByText("Connect to your agent")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("https://your-instance.onrender.com")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Your X-API-Key")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /connect/i })).toBeInTheDocument();
  });

  it("disables connect button when fields are empty", () => {
    render(<ConnectScreen onConnect={vi.fn()} />);
    const btn = screen.getByRole("button", { name: /connect/i });
    expect(btn).toBeDisabled();
  });

  it("shows default localhost URL", () => {
    render(<ConnectScreen onConnect={vi.fn()} />);
    const input = screen.getByPlaceholderText("https://your-instance.onrender.com") as HTMLInputElement;
    expect(input.value).toBe("http://localhost:8001");
  });

  it("has privacy notice", () => {
    render(<ConnectScreen onConnect={vi.fn()} />);
    expect(screen.getByText(/Stored only in this browser/)).toBeInTheDocument();
  });
});
