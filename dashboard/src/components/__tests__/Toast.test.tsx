import { render, screen, act } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ToastProvider, useToast } from "../Toast";

function TestComponent() {
  const { toast } = useToast();
  return (
    <div>
      <button onClick={() => toast("success", "It worked!")}>Success</button>
      <button onClick={() => toast("error", "Something broke")}>Error</button>
      <button onClick={() => toast("info", "FYI")}>Info</button>
    </div>
  );
}

describe("Toast", () => {
  it("throws when used outside provider", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<TestComponent />)).toThrow("useToast must be used within ToastProvider");
    consoleError.mockRestore();
  });

  it("shows success toast", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );
    act(() => {
      screen.getByText("Success").click();
    });
    expect(screen.getByText("It worked!")).toBeInTheDocument();
  });

  it("shows error toast", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );
    act(() => {
      screen.getByText("Error").click();
    });
    expect(screen.getByText("Something broke")).toBeInTheDocument();
  });

  it("shows info toast", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );
    act(() => {
      screen.getByText("Info").click();
    });
    expect(screen.getByText("FYI")).toBeInTheDocument();
  });

  it("auto-dismisses after 4 seconds", async () => {
    vi.useFakeTimers();
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );
    act(() => {
      screen.getByText("Success").click();
    });
    expect(screen.getByText("It worked!")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(screen.queryByText("It worked!")).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("dismisses on close button click", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );
    act(() => {
      screen.getByText("Success").click();
    });
    const closeButtons = screen.getAllByRole("button");
    const dismissButton = closeButtons[closeButtons.length - 1];
    act(() => {
      dismissButton.click();
    });
    expect(screen.queryByText("It worked!")).not.toBeInTheDocument();
  });
});
