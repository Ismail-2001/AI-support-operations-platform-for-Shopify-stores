import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Sidebar } from "../Sidebar";
import { ThemeProvider } from "../../lib/ThemeProvider";

const health = {
  shopify_connected: true,
  gorgias_connected: false,
  auto_send_enabled: true,
};

function renderSidebar(props: Partial<React.ComponentProps<typeof Sidebar>> = {}) {
  return render(
    <ThemeProvider>
      <Sidebar
        view="tickets"
        onNavigate={vi.fn()}
        onDisconnect={vi.fn()}
        health={health}
        {...props}
      />
    </ThemeProvider>
  );
}

describe("Sidebar", () => {
  it("renders navigation items", () => {
    renderSidebar();
    expect(screen.getByText("Tickets")).toBeInTheDocument();
    expect(screen.getByText("Analytics")).toBeInTheDocument();
    expect(screen.getByText("Knowledge base")).toBeInTheDocument();
    expect(screen.getByText("Settings")).toBeInTheDocument();
  });

  it("shows brand name", () => {
    renderSidebar();
    expect(screen.getByText("Support Console")).toBeInTheDocument();
  });

  it("shows health status indicators", () => {
    renderSidebar();
    expect(screen.getByText(/Shopify: Connected/)).toBeInTheDocument();
    expect(screen.getByText(/Gorgias: Not configured/)).toBeInTheDocument();
    expect(screen.getByText(/Auto-send: On/)).toBeInTheDocument();
  });

  it("shows disconnect button", () => {
    renderSidebar();
    expect(screen.getByText("Disconnect")).toBeInTheDocument();
  });

  it("shows theme toggle", () => {
    renderSidebar();
    expect(screen.getByText(/mode$/)).toBeInTheDocument();
  });
});
