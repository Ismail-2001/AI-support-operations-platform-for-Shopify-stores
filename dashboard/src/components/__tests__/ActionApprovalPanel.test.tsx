import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ActionApprovalPanel } from "../ActionApprovalPanel";
import { api } from "../../lib/api";
import type { SuggestedAction, TicketOrder } from "../../lib/types";

vi.mock("../../lib/api", () => ({
  api: {
    getTicketOrder: vi.fn(),
    approveCancel: vi.fn(),
    approveEditAddress: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

const conn = { baseUrl: "http://localhost:8001", apiKey: "test-key" };

const ORDER: TicketOrder = {
  ticket_id: "t1",
  order_id: "999",
  order_name: "#1042",
  total_price: 50,
  currency: "USD",
  financial_status: "paid",
  fulfillment_status: "unfulfilled",
  cancelled_at: null,
  shipping_address: { address1: "123 Old Street", city: "Springfield", zip: "00001", country: "United States" },
  line_items: [],
  already_refunded: 0,
  refundable: 50,
};

const CANCEL_ACTION: SuggestedAction = {
  type: "cancel_order",
  order_id: "999",
  reason: "Customer changed their mind",
  requires_approval: true,
};

const EDIT_ACTION: SuggestedAction = {
  type: "edit_address",
  order_id: "999",
  reason: "Typo in apartment number",
  address: { address1: "456 New Avenue", city: "Shelbyville", zip: "00002", country: "United States" },
  requires_approval: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getTicketOrder).mockResolvedValue(ORDER);
});

describe("ActionApprovalPanel — cancel order", () => {
  it("shows the suggestion, order preview, and undo warning", async () => {
    render(<ActionApprovalPanel connection={conn} ticketId="t1" action={CANCEL_ACTION} onApproved={vi.fn()} />);
    expect(screen.getByText("AI suggests: Cancel order")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("#1042")).toBeInTheDocument());
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
  });

  it("runs the two-step confirm and calls approveCancel with the reason", async () => {
    vi.mocked(api.approveCancel).mockResolvedValue({});
    const onApproved = vi.fn();
    render(<ActionApprovalPanel connection={conn} ticketId="t1" action={CANCEL_ACTION} onApproved={onApproved} />);

    await userEvent.click(screen.getByText("Approve cancel"));
    expect(screen.getByText(/Cancel this order for real/i)).toBeInTheDocument();

    await userEvent.click(screen.getByText("Confirm"));
    await waitFor(() => expect(screen.getByText("Order cancelled.")).toBeInTheDocument());
    expect(api.approveCancel).toHaveBeenCalledTimes(1);
    const [, ticketId, reason] = vi.mocked(api.approveCancel).mock.calls[0];
    expect(ticketId).toBe("t1");
    expect(reason).toBe("Customer changed their mind");
    expect(onApproved).toHaveBeenCalled();
  });

  it("blocks approval when the order has already shipped", async () => {
    vi.mocked(api.getTicketOrder).mockResolvedValue({ ...ORDER, fulfillment_status: "fulfilled" });
    render(<ActionApprovalPanel connection={conn} ticketId="t1" action={CANCEL_ACTION} onApproved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/order has shipped/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Approve cancel" })).toBeDisabled();
  });
});

describe("ActionApprovalPanel — edit address", () => {
  it("shows current vs requested side by side", async () => {
    render(<ActionApprovalPanel connection={conn} ticketId="t1" action={EDIT_ACTION} onApproved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("123 Old Street, Springfield 00001, United States")).toBeInTheDocument());
    expect(screen.getByText("456 New Avenue, Shelbyville 00002, United States")).toBeInTheDocument();
    expect(screen.getByText("AI suggests: Edit shipping address")).toBeInTheDocument();
  });

  it("requires all core fields before enabling approval", async () => {
    const action: SuggestedAction = {
      ...EDIT_ACTION,
      address: { address1: "", city: "", zip: "", country: "United States" },
    };
    render(<ActionApprovalPanel connection={conn} ticketId="t1" action={action} onApproved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Required:/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Approve address change" })).toBeDisabled();
  });

  it("submits the edited address and shows before/after", async () => {
    vi.mocked(api.approveEditAddress).mockResolvedValue({
      previous_address: ORDER.shipping_address,
      address: { ...EDIT_ACTION.address! },
    });
    const onApproved = vi.fn();
    render(<ActionApprovalPanel connection={conn} ticketId="t1" action={EDIT_ACTION} onApproved={onApproved} />);

    const cityInput = screen.getByDisplayValue("Shelbyville");
    await userEvent.clear(cityInput);
    await userEvent.type(cityInput, "Capital City");

    await userEvent.click(screen.getByText("Approve address change"));
    await userEvent.click(screen.getByText("Confirm"));

    await waitFor(() => expect(screen.getByText("Address updated.")).toBeInTheDocument());
    const [, , address] = vi.mocked(api.approveEditAddress).mock.calls[0];
    expect(address.city).toBe("Capital City");
    expect(onApproved).toHaveBeenCalled();
    expect(screen.getByText(/Before:/)).toBeInTheDocument();
    expect(screen.getByText(/After:/)).toBeInTheDocument();
  });

  it("surfaces a server-side rejection instead of claiming success", async () => {
    vi.mocked(api.approveEditAddress).mockRejectedValue(
      new (await import("../../lib/api")).ApiError(409, "Order is 'fulfilled' — the address can only be edited before it ships"),
    );
    render(<ActionApprovalPanel connection={conn} ticketId="t1" action={EDIT_ACTION} onApproved={vi.fn()} />);
    await userEvent.click(screen.getByText("Approve address change"));
    await userEvent.click(screen.getByText("Confirm"));
    await waitFor(() =>
      expect(screen.getByText(/address can only be edited before it ships/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText("Address updated.")).not.toBeInTheDocument();
  });
});
