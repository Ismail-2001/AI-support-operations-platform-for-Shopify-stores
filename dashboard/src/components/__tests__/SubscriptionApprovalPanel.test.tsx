import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SubscriptionApprovalPanel } from "../SubscriptionApprovalPanel";
import { api } from "../../lib/api";
import type { SuggestedAction, TicketSubscriptions } from "../../lib/types";

vi.mock("../../lib/api", () => ({
  api: {
    getSubscriptions: vi.fn(),
    approveSubscriptionAction: vi.fn(),
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

const SUBS: TicketSubscriptions = {
  ticket_id: "t1",
  email: "a@b.com",
  configured: true,
  provider: "recharge",
  subscriptions: [
    {
      id: "sub-1",
      provider: "recharge",
      status: "ACTIVE",
      title: "Coffee Beans",
      quantity: 2,
      price: "20.00",
      next_charge_date: "2026-11-01",
      frequency_unit: "month",
      frequency_count: 1,
    },
  ],
};

const PAUSE_ACTION: SuggestedAction = {
  type: "subscription_action",
  subscription_id: "sub-1",
  subscription_provider: "recharge",
  subscription_operation: "pause",
  reason: "Customer going on vacation",
  requires_approval: true,
};

const FREQ_ACTION: SuggestedAction = {
  type: "subscription_action",
  subscription_id: "sub-1",
  subscription_provider: "recharge",
  subscription_operation: "change_frequency",
  reason: "Customer wants every 2 weeks",
  frequency: { unit: "week", count: 2 },
  requires_approval: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getSubscriptions).mockResolvedValue(SUBS);
});

describe("SubscriptionApprovalPanel", () => {
  it("shows the operation, provider, and subscription details", async () => {
    render(
      <SubscriptionApprovalPanel connection={conn} ticketId="t1" action={PAUSE_ACTION} onApproved={vi.fn()} />,
    );
    expect(screen.getByText("AI suggests: Pause subscription")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Coffee Beans")).toBeInTheDocument());
    expect(screen.getByText("recharge")).toBeInTheDocument();
    expect(screen.getByText(/Next charge:/)).toBeInTheDocument();
  });

  it("runs the two-step confirm and sends the operation with an idempotency key", async () => {
    vi.mocked(api.approveSubscriptionAction).mockResolvedValue({
      subscription: { ...SUBS.subscriptions[0], status: "ACTIVE" },
      operation: "pause",
      replayed: false,
    });
    const onApproved = vi.fn();
    render(
      <SubscriptionApprovalPanel connection={conn} ticketId="t1" action={PAUSE_ACTION} onApproved={onApproved} />,
    );

    await userEvent.click(screen.getByText("Approve pause subscription"));
    expect(screen.getByText(/Apply “Pause subscription”/)).toBeInTheDocument();

    await userEvent.click(screen.getByText("Confirm"));
    await waitFor(() => expect(screen.getByText("Pause subscription applied.")).toBeInTheDocument());
    expect(api.approveSubscriptionAction).toHaveBeenCalledTimes(1);
    const [, ticketId, body, idempotencyKey] = vi.mocked(api.approveSubscriptionAction).mock.calls[0];
    expect(ticketId).toBe("t1");
    expect(body.subscription_id).toBe("sub-1");
    expect(body.operation).toBe("pause");
    expect(body.provider).toBe("recharge");
    expect(typeof idempotencyKey).toBe("string");
    expect(onApproved).toHaveBeenCalled();
  });

  it("disables approval when no subscription app is connected", async () => {
    vi.mocked(api.getSubscriptions).mockResolvedValue({
      ...SUBS,
      configured: false,
      provider: null,
      subscriptions: [],
    });
    render(
      <SubscriptionApprovalPanel connection={conn} ticketId="t1" action={PAUSE_ACTION} onApproved={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getByText(/No subscription app/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Approve pause subscription" })).toBeDisabled();
  });

  it("validates frequency count before enabling approval", async () => {
    const action: SuggestedAction = { ...FREQ_ACTION, frequency: { unit: "week", count: 99 } };
    render(
      <SubscriptionApprovalPanel connection={conn} ticketId="t1" action={action} onApproved={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getByText(/Count must be 1-60/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Approve change delivery frequency" })).toBeDisabled();
  });

  it("submits the edited frequency", async () => {
    vi.mocked(api.approveSubscriptionAction).mockResolvedValue({
      subscription: { ...SUBS.subscriptions[0], frequency_unit: "week", frequency_count: 3 },
      operation: "change_frequency",
      replayed: false,
    });
    render(
      <SubscriptionApprovalPanel connection={conn} ticketId="t1" action={FREQ_ACTION} onApproved={vi.fn()} />,
    );

    const countInput = screen.getByDisplayValue("2");
    await userEvent.clear(countInput);
    await userEvent.type(countInput, "3");

    await userEvent.click(screen.getByText("Approve change delivery frequency"));
    await userEvent.click(screen.getByText("Confirm"));
    await waitFor(() => expect(screen.getByText("Change delivery frequency applied.")).toBeInTheDocument());

    const [, , body] = vi.mocked(api.approveSubscriptionAction).mock.calls[0];
    expect(body.frequency).toEqual({ unit: "week", count: 3 });
  });

  it("surfaces a server-side rejection instead of claiming success", async () => {
    vi.mocked(api.approveSubscriptionAction).mockRejectedValue(
      new (await import("../../lib/api")).ApiError(409, "Subscription is already cancelled"),
    );
    render(
      <SubscriptionApprovalPanel connection={conn} ticketId="t1" action={PAUSE_ACTION} onApproved={vi.fn()} />,
    );
    await userEvent.click(screen.getByText("Approve pause subscription"));
    await userEvent.click(screen.getByText("Confirm"));
    await waitFor(() => expect(screen.getByText(/already cancelled/)).toBeInTheDocument());
    expect(screen.queryByText("Pause subscription applied.")).not.toBeInTheDocument();
  });
});
