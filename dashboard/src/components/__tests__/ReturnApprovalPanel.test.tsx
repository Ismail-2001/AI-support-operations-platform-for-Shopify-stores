import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ReturnApprovalPanel } from "../ReturnApprovalPanel";
import { api } from "../../lib/api";
import type { ReturnEligibility, ReturnRate, SuggestedAction } from "../../lib/types";

vi.mock("../../lib/api", () => ({
  api: {
    getReturnEligibility: vi.fn(),
    getReturnRates: vi.fn(),
    approveReturnLabel: vi.fn(),
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

const ELIGIBLE: ReturnEligibility = {
  ticket_id: "t1",
  order_id: "999",
  eligible: true,
  reason: null,
  window_days: 30,
  last_return_date: "2026-11-04",
  line_items: [{ line_item_id: 111, title: "Mug", quantity: 1, sku: "MUG-1" }],
  label_provider: { provider: "shipengine", configured: true, missing_settings: [] },
};

const RATES: ReturnRate[] = [
  { rate_id: "r-usps", carrier: "USPS", service: "Ground Advantage", amount: 4.35, currency: "USD" },
  { rate_id: "r-ups", carrier: "UPS", service: "Ground", amount: 7.2, currency: "USD" },
];

const ACTION: SuggestedAction = {
  type: "return_label",
  order_id: "999",
  reason: "Damaged on arrival",
  requires_approval: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getReturnEligibility).mockResolvedValue(ELIGIBLE);
  vi.mocked(api.getReturnRates).mockResolvedValue({
    ticket_id: "t1",
    order_id: "999",
    rates: RATES,
    cheapest_rate_id: "r-usps",
  });
});

describe("ReturnApprovalPanel", () => {
  it("shows the return window and eligible items when eligible", async () => {
    render(<ReturnApprovalPanel connection={conn} ticketId="t1" action={ACTION} onApproved={vi.fn()} />);
    expect(screen.getByText("AI suggests: Create prepaid return label")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/Return window:/)).toBeInTheDocument());
    expect(screen.getByText(/through 2026-11-04/)).toBeInTheDocument();
    expect(screen.getByText(/1 item\(s\) eligible/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve return label" })).toBeEnabled();
  });

  it("buys the label with an idempotency key and shows tracking + PDF link", async () => {
    vi.mocked(api.approveReturnLabel).mockResolvedValue({
      label: {
        label_id: "L-1",
        label_url: "https://labels.example/L-1.pdf",
        tracking_number: "TR-1",
        carrier: "USPS",
        cost_usd: 4.35,
      },
      replayed: false,
    });
    const onApproved = vi.fn();
    render(<ReturnApprovalPanel connection={conn} ticketId="t1" action={ACTION} onApproved={onApproved} />);
    await waitFor(() => expect(screen.getByText(/Return window:/)).toBeInTheDocument());

    await userEvent.click(screen.getByText("Approve return label"));
    expect(screen.getByText(/charges your carrier account/i)).toBeInTheDocument();

    await userEvent.click(screen.getByText("Confirm"));
    await waitFor(() => expect(screen.getByText("Return label created.")).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "Open label PDF" })).toHaveAttribute(
      "href",
      "https://labels.example/L-1.pdf",
    );
    expect(api.approveReturnLabel).toHaveBeenCalledTimes(1);
    const [, ticketId, body, idempotencyKey] = vi.mocked(api.approveReturnLabel).mock.calls[0];
    expect(ticketId).toBe("t1");
    expect(body.reason).toBe("Damaged on arrival");
    expect(body.rate_id).toBe("r-usps");
    expect(typeof idempotencyKey).toBe("string");
    expect(onApproved).toHaveBeenCalled();
  });

  it("shows live carrier costs before approval and defaults to the cheapest rate", async () => {
    render(<ReturnApprovalPanel connection={conn} ticketId="t1" action={ACTION} onApproved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Carrier cost before purchase/)).toBeInTheDocument());
    expect(screen.getByText("$4.35")).toBeInTheDocument();
    expect(screen.getByText("$7.20")).toBeInTheDocument();
    expect(screen.getByText(/USPS · Ground Advantage/)).toBeInTheDocument();
    expect(screen.getByLabelText(/USPS · Ground Advantage/)).toBeChecked();
    expect(screen.getByText(/cheapest/i)).toBeInTheDocument();

    await userEvent.click(screen.getByText("Approve return label"));
    expect(screen.getByText(/Buy this label for USPS at \$4\.35/)).toBeInTheDocument();
  });

  it("lets the operator pick a different carrier rate", async () => {
    vi.mocked(api.approveReturnLabel).mockResolvedValue({
      label: { label_id: "L-2", carrier: "UPS", cost_usd: 7.2 },
      replayed: false,
    });
    render(<ReturnApprovalPanel connection={conn} ticketId="t1" action={ACTION} onApproved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Carrier cost before purchase/)).toBeInTheDocument());

    await userEvent.click(screen.getByLabelText(/UPS · Ground/));
    await userEvent.click(screen.getByText("Approve return label"));
    await userEvent.click(screen.getByText("Confirm"));
    await waitFor(() => expect(screen.getByText("Return label created.")).toBeInTheDocument());

    const [, , body] = vi.mocked(api.approveReturnLabel).mock.calls[0];
    expect(body.rate_id).toBe("r-ups");
  });

  it("blocks approval when the carrier returns no rates", async () => {
    vi.mocked(api.getReturnRates).mockResolvedValue({
      ticket_id: "t1",
      order_id: "999",
      rates: [],
      cheapest_rate_id: null,
    });
    render(<ReturnApprovalPanel connection={conn} ticketId="t1" action={ACTION} onApproved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/No carrier rates available/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "No carrier rates" })).toBeDisabled();
    expect(api.approveReturnLabel).not.toHaveBeenCalled();
  });

  it("keeps approval available when live rates cannot be fetched", async () => {
    vi.mocked(api.getReturnRates).mockRejectedValue(
      new (await import("../../lib/api")).ApiError(503, "ShipEngine unreachable"),
    );
    render(<ReturnApprovalPanel connection={conn} ticketId="t1" action={ACTION} onApproved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Rates unavailable/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Approve return label" })).toBeEnabled();
  });

  it("blocks approval when the return window has expired", async () => {
    vi.mocked(api.getReturnEligibility).mockResolvedValue({
      ...ELIGIBLE,
      eligible: false,
      reason: "Return window expired 10 day(s) ago (window: 30 days)",
    });
    render(<ReturnApprovalPanel connection={conn} ticketId="t1" action={ACTION} onApproved={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText(/Return window expired 10 day/)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Return label unavailable" })).toBeDisabled();
    expect(api.approveReturnLabel).not.toHaveBeenCalled();
  });

  it("blocks approval when ShipEngine / the return address is not configured", async () => {
    vi.mocked(api.getReturnEligibility).mockResolvedValue({
      ...ELIGIBLE,
      label_provider: {
        provider: "shipengine",
        configured: false,
        missing_settings: ["SHIPENGINE_API_KEY", "RETURN_ADDRESS1"],
      },
    });
    render(<ReturnApprovalPanel connection={conn} ticketId="t1" action={ACTION} onApproved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Return shipping is not configured/)).toBeInTheDocument());
    expect(screen.getByText(/SHIPENGINE_API_KEY/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Return label unavailable" })).toBeDisabled();
  });

  it("surfaces a server-side rejection instead of claiming success", async () => {
    vi.mocked(api.approveReturnLabel).mockRejectedValue(
      new (await import("../../lib/api")).ApiError(409, "Return window expired"),
    );
    render(<ReturnApprovalPanel connection={conn} ticketId="t1" action={ACTION} onApproved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Return window:/)).toBeInTheDocument());
    await userEvent.click(screen.getByText("Approve return label"));
    await userEvent.click(screen.getByText("Confirm"));
    await waitFor(() => expect(screen.getByText(/Return window expired/)).toBeInTheDocument());
    expect(screen.queryByText("Return label created.")).not.toBeInTheDocument();
  });
});
