import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { RefundApprovalPanel } from "../RefundApprovalPanel";
import { api } from "../../lib/api";
import type { SuggestedAction, TicketOrder } from "../../lib/types";

vi.mock("../../lib/api", () => ({
  api: {
    getTicketOrder: vi.fn(),
    approveRefund: vi.fn(),
    approveResend: vi.fn(),
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
  total_price: 100,
  currency: "USD",
  financial_status: "paid",
  fulfillment_status: "unfulfilled",
  cancelled_at: null,
  shipping_address: {},
  line_items: [
    { id: 111, title: "Blue Hoodie", quantity: 2, price: "40.00" },
    { id: 222, title: "Beanie", quantity: 1, price: "20.00" },
  ],
  already_refunded: 80,
  refundable: 20,
};

const REFUND_ACTION: SuggestedAction = {
  type: "refund",
  order_id: "999",
  amount: 15,
  reason: "One item arrived damaged",
  requires_approval: true,
};

const RESEND_ACTION: SuggestedAction = {
  type: "resend_order",
  order_id: "999",
  reason: "Replacement — item not received",
  requires_approval: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getTicketOrder).mockResolvedValue(ORDER);
});

describe("RefundApprovalPanel", () => {
  it("shows the refundable remainder from the real order", async () => {
    render(<RefundApprovalPanel connection={conn} ticketId="t1" action={REFUND_ACTION} onApproved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Refundable:/)).toBeInTheDocument());
    expect(screen.getByText("$20.00")).toBeInTheDocument();
    expect(screen.getByText(/of \$100.00 total/)).toBeInTheDocument();
    expect(screen.getByText(/\$80.00 already refunded/)).toBeInTheDocument();
  });

  it("blocks amounts above what is still refundable", async () => {
    render(<RefundApprovalPanel connection={conn} ticketId="t1" action={REFUND_ACTION} onApproved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Refundable:/)).toBeInTheDocument());
    const amount = screen.getByDisplayValue("15");
    await userEvent.clear(amount);
    await userEvent.type(amount, "30");
    expect(screen.getByText(/exceeds the \$20.00 still refundable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Approve & send to Shopify/ })).toBeDisabled();
  });

  it("scopes the refund to checked line items", async () => {
    vi.mocked(api.approveRefund).mockResolvedValue({});
    const onApproved = vi.fn();
    render(<RefundApprovalPanel connection={conn} ticketId="t1" action={REFUND_ACTION} onApproved={onApproved} />);

    await waitFor(() => expect(screen.getByText("Blue Hoodie")).toBeInTheDocument());
    const hoodieRow = screen.getByText("Blue Hoodie").closest("label");
    expect(hoodieRow).not.toBeNull();
    await userEvent.click(hoodieRow!);

    await userEvent.click(screen.getByRole("button", { name: /Approve & send to Shopify/ }));
    await userEvent.click(screen.getByText("Confirm refund"));

    await waitFor(() => expect(api.approveRefund).toHaveBeenCalledTimes(1));
    const [, , amount, reason, key, lineItems] = vi.mocked(api.approveRefund).mock.calls[0];
    expect(amount).toBe(15);
    expect(reason).toBe("One item arrived damaged");
    expect(typeof key).toBe("string");
    expect(lineItems).toEqual([{ line_item_id: 111, quantity: 1 }]);
    expect(onApproved).toHaveBeenCalled();
  });

  it("sends resend suggestions to the resend endpoint, not the refund one", async () => {
    vi.mocked(api.approveResend).mockResolvedValue({});
    render(<RefundApprovalPanel connection={conn} ticketId="t1" action={RESEND_ACTION} onApproved={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: /Approve & send to Shopify/ }));
    await userEvent.click(screen.getByText("Confirm resend"));

    await waitFor(() => expect(api.approveResend).toHaveBeenCalledTimes(1));
    expect(api.approveRefund).not.toHaveBeenCalled();
  });
});
