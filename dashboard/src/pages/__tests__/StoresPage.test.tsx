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
    createStore: vi.fn(),
    deleteStore: vi.fn(),
  },
}));

const conn = { baseUrl: "http://localhost:8001", apiKey: "test-key" };

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
});
