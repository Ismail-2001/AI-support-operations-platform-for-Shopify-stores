import { render, screen, act } from "@testing-library/react";
import { describe, it, expect, beforeEach } from "vitest";
import { ThemeProvider, useTheme } from "../../lib/ThemeProvider";

function TestComponent() {
  const { theme, toggle, set } = useTheme();
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button onClick={toggle}>Toggle</button>
      <button onClick={() => set("dark")}>Set Dark</button>
      <button onClick={() => set("light")}>Set Light</button>
    </div>
  );
}

describe("ThemeProvider", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.classList.remove("dark");
  });

  it("provides light theme by default", () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );
    expect(screen.getByTestId("theme")).toHaveTextContent("light");
  });

  it("toggles theme", () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );
    act(() => {
      screen.getByText("Toggle").click();
    });
    expect(screen.getByTestId("theme")).toHaveTextContent("dark");
  });

  it("adds dark class to html element", () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );
    act(() => {
      screen.getByText("Toggle").click();
    });
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("removes dark class when toggling back to light", () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );
    act(() => {
      screen.getByText("Toggle").click();
    });
    act(() => {
      screen.getByText("Toggle").click();
    });
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("persists theme to localStorage", () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );
    act(() => {
      screen.getByText("Toggle").click();
    });
    expect(localStorage.getItem("cs-agent-theme")).toBe("dark");
  });

  it("restores theme from localStorage", () => {
    localStorage.setItem("cs-agent-theme", "dark");
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );
    expect(screen.getByTestId("theme")).toHaveTextContent("dark");
  });

  it("set() sets theme directly", () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );
    act(() => {
      screen.getByText("Set Dark").click();
    });
    expect(screen.getByTestId("theme")).toHaveTextContent("dark");
    act(() => {
      screen.getByText("Set Light").click();
    });
    expect(screen.getByTestId("theme")).toHaveTextContent("light");
  });
});
