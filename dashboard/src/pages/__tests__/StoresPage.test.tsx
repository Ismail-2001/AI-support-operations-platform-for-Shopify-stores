import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { StoresPage } from "../StoresPage";
import { ToastProvider } from "../../components/Toast";
import { api } from "../../lib/api";
import type { StoreRecord } from "../../lib/types";

vi.mock("../../lib/api", () => ({
  api: {
    listStores: vi.fn(),
    getStoreSummary: vi.fn(),
    createStore: vi.fn(),
    deleteStore: vi.fn(),
  },
}));

const conn = { baseUrl: "http://localhost:8001", apiKey: "test-key" };

const emptySummary = {
  counts: { total: 0, shopify: 0, gorgias: 0, shipengine: 0, subscriptions: 0 },
  sync_running: 0,
  stores: [],
};

function makeStore(overrides: Partial<StoreRecord> = {}): StoreRecord {
  return {
    id: "store-1",
    name: "Acme",
    shop_domain: "acme.myshopify.com",
    has_shopify_token: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderPage(props: Partial<React.ComponentProps<typeof StoresPage>> = {}) {
  const onActivateStore = vi.fn();
  const onStoresChanged = vi.fn();
  const utils = render(
    <ToastProvider>
      <StoresPage
        connection={conn}
        selectedStore={null}
        onActivateStore={onActivateStore}
        onStoresChanged={onStoresChanged}
        {...props}
      />
    </ToastProvider>,
  );
  return { ...utils, onActivateStore, onStoresChanged };
}

const mocked = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  mocked.listStores.mockResolvedValue({ stores: [] });
  mocked.getStoreSummary.mockResolvedValue(emptySummary);
});

describe("StoresPage", () => {
  it("shows an empty state when no stores exist", async () => {
    renderPage();
    await waitFor(() =>
      expect(screen.getByText(/No stores yet/)).toBeInTheDocument(),
    );
  });

  it("renders stores with active marker", async () => {
    mocked.listStores.mockResolvedValue({
      stores: [makeStore(), makeStore({ id: "store-2", name: "Beta", shop_domain: "", has_shopify_token: false })],
    });
    renderPage({ selectedStore: "store-1" });
    await waitFor(() => expect(screen.getByText("Beta")).toBeInTheDocument());
    expect(screen.getByText("(active)")).toBeInTheDocument();
    expect(screen.getAllByText("not connected")).toHaveLength(1);
    expect(screen.getByText("none")).toBeInTheDocument();
  });

  it("creates a store and refreshes the list", async () => {
    mocked.createStore.mockResolvedValue({ store: makeStore() });
    mocked.listStores.mockResolvedValueOnce({ stores: [] }).mockResolvedValueOnce({
      stores: [makeStore()],
    });
    const { onStoresChanged } = renderPage();
    await waitFor(() => expect(screen.getByText(/No stores yet/)).toBeInTheDocument());

    await userEvent.type(screen.getByPlaceholderText("Acme"), "Acme");
    await userEvent.type(screen.getByPlaceholderText("acme.myshopify.com"), "acme.myshopify.com");
    await userEvent.type(screen.getByPlaceholderText("shpat_..."), "shpat_abc");
    await userEvent.click(screen.getByRole("button", { name: "Add store" }));

    await waitFor(() =>
      expect(mocked.createStore).toHaveBeenCalledWith(conn, {
        name: "Acme",
        shop_domain: "acme.myshopify.com",
        shopify_access_token: "shpat_abc",
      }),
    );
    await waitFor(() => expect(screen.getByText("Acme")).toBeInTheDocument());
    expect(onStoresChanged).toHaveBeenCalled();
  });

  it("requires a name before creating", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText(/No stores yet/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Add store" })).toBeDisabled();
    expect(mocked.createStore).not.toHaveBeenCalled();
  });

  it("deletes only after a second confirming click", async () => {
    mocked.listStores.mockResolvedValue({ stores: [makeStore()] });
    mocked.deleteStore.mockResolvedValue({ deleted: true, store_id: "store-1" });
    const { onStoresChanged } = renderPage();
    await waitFor(() => expect(screen.getByText("Acme")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /Delete/ }));
    expect(screen.getByRole("button", { name: /Confirm delete/ })).toBeInTheDocument();
    expect(mocked.deleteStore).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /Confirm delete/ }));
    await waitFor(() =>
      expect(mocked.deleteStore).toHaveBeenCalledWith(conn, "store-1"),
    );
    expect(onStoresChanged).toHaveBeenCalled();
  });

  it("activates a store through the callback", async () => {
    mocked.listStores.mockResolvedValue({ stores: [makeStore()] });
    const { onActivateStore } = renderPage();
    await waitFor(() => expect(screen.getByText("Acme")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Use store" }));
    expect(onActivateStore).toHaveBeenCalledWith("store-1");
  });

  it("shows fleet health chips from the summary", async () => {
    mocked.listStores.mockResolvedValue({
      stores: [makeStore(), makeStore({ id: "store-2", name: "Beta" })],
    });
    mocked.getStoreSummary.mockResolvedValue({
      counts: { total: 2, shopify: 1, gorgias: 1, shipengine: 0, subscriptions: 0 },
      sync_running: 1,
      stores: [],
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("2 stores")).toBeInTheDocument());
    expect(screen.getByText("Shopify 1/2")).toBeInTheDocument();
    expect(screen.getByText("Gorgias 1/2")).toBeInTheDocument();
    expect(screen.getByText("Subscriptions 0/2")).toBeInTheDocument();
    expect(screen.getByText("1 syncing")).toBeInTheDocument();
  });

  it("renders integration pills and sync state per store", async () => {
    mocked.listStores.mockResolvedValue({ stores: [makeStore()] });
    mocked.getStoreSummary.mockResolvedValue({
      counts: { total: 1, shopify: 1, gorgias: 1, shipengine: 0, subscriptions: 1 },
      sync_running: 1,
      stores: [
        {
          id: "store-1",
          name: "Acme",
          shop_domain: "acme.myshopify.com",
          integrations: {
            shopify: true,
            gorgias: true,
            shipengine: false,
            recharge: true,
            skio: false,
          },
          subscription_provider: "recharge",
          sync: {
            status: "running",
            last_sync_at: null,
            products_seen: 12,
            error: null,
          },
        },
      ],
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("gorgias")).toBeInTheDocument());
    expect(screen.getByText("recharge")).toBeInTheDocument();
    expect(screen.getByText("syncing")).toBeInTheDocument();
    // Shopify is shown in its own column, not duplicated as a pill.
    expect(screen.queryByText("shopify")).not.toBeInTheDocument();
  });

  it("shows sync error state with the failure message", async () => {
    mocked.listStores.mockResolvedValue({ stores: [makeStore()] });
    mocked.getStoreSummary.mockResolvedValue({
      counts: { total: 1, shopify: 1, gorgias: 0, shipengine: 0, subscriptions: 0 },
      sync_running: 0,
      stores: [
        {
          id: "store-1",
          name: "Acme",
          shop_domain: "acme.myshopify.com",
          integrations: {
            shopify: true,
            gorgias: false,
            shipengine: false,
            recharge: false,
            skio: false,
          },
          subscription_provider: null,
          sync: {
            status: "error",
            last_sync_at: null,
            products_seen: 0,
            error: "429 rate limited",
          },
        },
      ],
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("sync error")).toBeInTheDocument());
    expect(screen.getByTitle("429 rate limited")).toBeInTheDocument();
  });

  it("keeps the store list working when the summary request fails", async () => {
    mocked.listStores.mockResolvedValue({ stores: [makeStore()] });
    mocked.getStoreSummary.mockRejectedValue(new Error("boom"));
    renderPage();
    await waitFor(() => expect(screen.getByText("Acme")).toBeInTheDocument());
    expect(screen.queryByTestId("fleet-summary")).not.toBeInTheDocument();
    expect(screen.getByText(/not connected|acme\.myshopify\.com/)).toBeInTheDocument();
  });
});
