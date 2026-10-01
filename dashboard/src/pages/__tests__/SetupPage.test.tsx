import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SetupPage } from "../SetupPage";
import { api } from "../../lib/api";
import type { SetupStatus } from "../../lib/types";

vi.mock("../../lib/api", () => ({
  api: {
    setupStatus: vi.fn(),
    setupShopify: vi.fn(),
    setupVoice: vi.fn(),
    setupTest: vi.fn(),
    kbSyncShopify: vi.fn(),
  },
}));

const conn = { baseUrl: "http://localhost:8001", apiKey: "test-key" };

function makeStatus(overrides: Partial<SetupStatus> = {}): SetupStatus {
  return {
    shopify: { connected: false, domain: null },
    knowledge_base: { chunk_count: 0 },
    voice: { store_name: "", tone: "friendly", sign_off: "", support_email: "" },
    voice_set: false,
    google_key_set: true,
    test_done: false,
    steps: { shopify: false, policies: false, voice: false, test: false },
    setup_complete: false,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("SetupPage", () => {
  it("shows loading state before status arrives", () => {
    vi.mocked(api.setupStatus).mockReturnValue(new Promise(() => {}));
    render(<SetupPage connection={conn} onNavigate={vi.fn()} />);
    expect(screen.getByText(/Loading setup status/i)).toBeInTheDocument();
  });

  it("lands on step 1 (store) when nothing is configured", async () => {
    vi.mocked(api.setupStatus).mockResolvedValue(makeStatus());
    render(<SetupPage connection={conn} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Connect Shopify")).toBeInTheDocument());
    expect(screen.getByPlaceholderText("your-store.myshopify.com")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("shpat_…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Validate & connect/i })).toBeDisabled();
  });

  it("jumps to the first incomplete step", async () => {
    vi.mocked(api.setupStatus).mockResolvedValue(
      makeStatus({
        shopify: { connected: true, domain: "acme.myshopify.com" },
        knowledge_base: { chunk_count: 12 },
        steps: { shopify: true, policies: true, voice: false, test: false },
      })
    );
    render(<SetupPage connection={conn} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Brand voice")).toBeInTheDocument());
    expect(screen.getByPlaceholderText("Northwind Supply")).toBeInTheDocument();
  });

  it("lands on the test step when everything except test is done", async () => {
    vi.mocked(api.setupStatus).mockResolvedValue(
      makeStatus({
        shopify: { connected: true, domain: "acme.myshopify.com" },
        knowledge_base: { chunk_count: 12 },
        voice_set: true,
        voice: { store_name: "Acme", tone: "friendly", sign_off: "", support_email: "" },
        steps: { shopify: true, policies: true, voice: true, test: false },
      })
    );
    render(<SetupPage connection={conn} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Test it live")).toBeInTheDocument());
  });

  it("lets the user switch steps from the indicator", async () => {
    vi.mocked(api.setupStatus).mockResolvedValue(makeStatus());
    render(<SetupPage connection={conn} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Connect Shopify")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /2\. Knowledge/i }));
    expect(screen.getByText("Import your knowledge")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /3\. Brand voice/i }));
    expect(screen.getByText("How replies should sound")).toBeInTheDocument();
  });

  it("advances to knowledge step after a successful connect", async () => {
    vi.mocked(api.setupStatus).mockResolvedValue(makeStatus());
    vi.mocked(api.setupShopify).mockResolvedValue({
      connected: true,
      domain: "acme.myshopify.com",
      shop_name: "Acme",
      shop_email: "owner@acme.com",
      currency: "USD",
    });
    render(<SetupPage connection={conn} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Connect Shopify")).toBeInTheDocument());

    await userEvent.type(screen.getByPlaceholderText("your-store.myshopify.com"), "acme");
    await userEvent.type(screen.getByPlaceholderText("shpat_…"), "shpat_123");
    await userEvent.click(screen.getByRole("button", { name: /Validate & connect/i }));

    await waitFor(() => expect(screen.getByText("Import your knowledge")).toBeInTheDocument());
    expect(api.setupShopify).toHaveBeenCalledWith(conn, "acme", "shpat_123");
  });

  it("shows an error when connect fails", async () => {
    vi.mocked(api.setupStatus).mockResolvedValue(makeStatus());
    vi.mocked(api.setupShopify).mockRejectedValue(new Error("Shopify rejected the access token"));
    render(<SetupPage connection={conn} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Connect Shopify")).toBeInTheDocument());

    await userEvent.type(screen.getByPlaceholderText("your-store.myshopify.com"), "acme");
    await userEvent.type(screen.getByPlaceholderText("shpat_…"), "bad");
    await userEvent.click(screen.getByRole("button", { name: /Validate & connect/i }));

    await waitFor(() =>
      expect(screen.getByText(/Shopify rejected the access token/)).toBeInTheDocument()
    );
  });
});
