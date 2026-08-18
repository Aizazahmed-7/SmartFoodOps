import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { cancelOrder, getOrder } from "../api/client";
import { hasCode } from "../api/errors";
import {
  CANCEL_FAMILY, CANCELLABLE_STATUSES, TERMINAL_STATUSES,
  type CancelReason, type OrderStatus,
} from "../api/types";
import { ErrorNote, Money, Note, Spinner, StatusTag } from "../components/ui";

/** The happy chain as the customer sees it (internal hops folded away). */
const JOURNEY: { at: OrderStatus[]; label: string }[] = [
  { at: ["PLACED", "VALIDATED", "PAYMENT_CLEARED"], label: "Placed" },
  { at: ["CONFIRMED"], label: "Confirmed" },
  { at: ["ACCEPTED", "PREPARING"], label: "Cooking" },
  { at: ["READY", "PICKED_UP"], label: "On its way" },
  { at: ["DELIVERED", "SETTLED"], label: "Delivered" },
];

// Reasons where a card hold actually existed and was voided (auth happens
// before CONFIRMED; these cancels all come after it). Stock/decline cancels
// never held money — saying "released" there would be inventing a refund.
const HOLD_WAS_RELEASED = new Set(["restaurant_rejected", "restaurant_timeout", "customer_cancelled"]);

const REASONS: Record<CancelReason, string> = {
  item_unavailable: "some items ran out of stock",
  at_capacity: "the kitchen is at capacity",
  payment_declined: "your card was declined",
  restaurant_rejected: "the restaurant couldn't take the order",
  restaurant_timeout: "the restaurant didn't respond in time",
  customer_cancelled: "you cancelled it",
  system_timeout: "we couldn't complete it in time",
};

function Journey({ status }: { status: OrderStatus }) {
  const reached = JOURNEY.findIndex((step) => step.at.includes(status));
  if (reached < 0) return null; // cancel family renders its own banner
  return (
    <div className="card flex items-center gap-1">
      {JOURNEY.map((step, i) => (
        <div key={step.label} className="flex flex-1 flex-col items-center gap-1">
          <div className={`h-2.5 w-2.5 rounded-full ${i <= reached ? "bg-orange-500" : "bg-slate-700"}`} />
          <span className={`text-[11px] ${i <= reached ? "text-slate-200" : "text-slate-500"}`}>
            {step.label}
          </span>
        </div>
      ))}
    </div>
  );
}

export default function OrderDetail() {
  const { id } = useParams<{ id: string }>();
  const queryClient = useQueryClient();
  const order = useQuery({
    queryKey: ["order", id],
    queryFn: () => getOrder(id!),
    refetchInterval: (query) =>
      // Poll while the order is still moving — AND while it is not here
      // yet: a placement whose saga answered slowly hands back a real id
      // seconds before the row exists (ADR-0023's pending case), so a 404
      // right after checkout means "being placed", not "no such order".
      !query.state.data || !TERMINAL_STATUSES.includes(query.state.data.status) ? 3000 : false,
    refetchIntervalInBackground: true, // tracking keeps moving in a background tab
    retry: (failureCount, error) => hasCode(error, "NOT_FOUND") && failureCount < 5,
  });

  const cancel = useMutation({
    mutationFn: () => cancelOrder(id!),
    // 202 and 200 both resolve; either way the poll shows the truth next tick.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["order", id] }),
  });

  if (order.isLoading) return <Spinner />;
  // A single failed poll must not blank a working tracking screen: only
  // error out when we have nothing to show.
  if (order.error && !order.data) return <ErrorNote error={order.error} />;
  const o = order.data!;
  const cancelled = CANCEL_FAMILY.includes(o.status);
  const cancellable = CANCELLABLE_STATUSES.includes(o.status);

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-bold">{o.restaurant_name}</h1>
        <StatusTag status={o.status} />
      </div>

      {cancelled ? (
        <Note tone="error">
          Order {o.status === "CANCELLING" ? "is being cancelled" : "was cancelled"}
          {/* Unknown reasons (a newer backend) fall back to the raw slug. */}
          {o.cancel_reason ? ` — ${REASONS[o.cancel_reason as CancelReason] ?? o.cancel_reason}` : ""}.
          {o.cancel_reason && HOLD_WAS_RELEASED.has(o.cancel_reason)
            ? " Your card hold has been released."
            : " Your card was never charged."}
        </Note>
      ) : (
        <Journey status={o.status} />
      )}

      <div className="card space-y-1 text-sm">
        {o.items.map((item, i) => (
          <div key={i} className="flex justify-between">
            <span>
              {item.qty} × {item.name}
              {item.options.length > 0 && (
                <span className="block text-xs text-slate-500">
                  {(item.options as { name?: string }[]).map((opt) => opt.name).filter(Boolean).join(", ")}
                </span>
              )}
            </span>
            <Money cents={item.line_total_cents} />
          </div>
        ))}
        <div className="flex justify-between text-slate-400">
          <span>Delivery + tax</span>
          <Money cents={o.totals.fee_cents + o.totals.tax_cents} />
        </div>
        <div className="mt-2 flex justify-between border-t border-slate-800 pt-2 font-semibold">
          <span>Total</span>
          <Money cents={o.totals.total_cents} />
        </div>
      </div>

      <p className="text-xs text-slate-500">
        Delivering to {o.delivery_address.label} — {o.delivery_address.line1},{" "}
        {o.delivery_address.city} · placed {new Date(o.placed_at).toLocaleString()}
      </p>

      {cancellable && (
        <button
          className="btn-danger w-full"
          disabled={cancel.isPending}
          onClick={() => cancel.mutate()}
        >
          {cancel.isPending ? "Cancelling…" : "Cancel order"}
        </button>
      )}
      {hasCode(cancel.error, "ORDER_NOT_CANCELLABLE") ? (
        <p className="text-sm text-amber-300">
          Too late to cancel — the courier already has your food.
        </p>
      ) : (
        <ErrorNote error={cancel.error} />
      )}

      <Link to="/orders" className="inline-block text-sm text-slate-400 hover:text-white">
        ← All orders
      </Link>
    </div>
  );
}
