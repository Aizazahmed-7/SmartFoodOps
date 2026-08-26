import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { cancelOrder, getCourier, getOrder, getTrackTicket } from "../api/client";
import { hasCode } from "../api/errors";
import {
  CANCEL_FAMILY, CANCELLABLE_STATUSES, TERMINAL_STATUSES,
  type CancelReason, type OrderStatus,
} from "../api/types";
import { ErrorNote, Money, Note, Spinner, StatusTag } from "../components/ui";
import CityMap, { Pin, project } from "../components/CityMap";

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
  // S4: live tracking. The stream pushes STATUS HINTS; every render still
  // comes from the GET (the database stays the only truth), so a lost or
  // phantom hint costs one refetch at most. While the stream is up the
  // poll idles; any stream failure silently returns to the 3s poll — the
  // customer never sees the difference, only the latency.
  const [streaming, setStreaming] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  const order = useQuery({
    queryKey: ["order", id],
    queryFn: () => getOrder(id!),
    refetchInterval: (query) =>
      // Poll while the order is still moving — AND while it is not here
      // yet: a placement whose saga answered slowly hands back a real id
      // seconds before the row exists (ADR-0023's pending case), so a 404
      // right after checkout means "being placed", not "no such order".
      streaming
        ? false // the stream is the ticker; the poll is the floor beneath it
        : !query.state.data || !TERMINAL_STATUSES.includes(query.state.data.status)
          ? 3000
          : false,
    refetchIntervalInBackground: true, // tracking keeps moving in a background tab
    retry: (failureCount, error) => hasCode(error, "NOT_FOUND") && failureCount < 5,
  });

  const cancel = useMutation({
    mutationFn: () => cancelOrder(id!),
    // 202 and 200 both resolve; either way the poll shows the truth next tick.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["order", id] }),
  });

  const status = order.data?.status;
  useEffect(() => {
    if (!id || !status || TERMINAL_STATUSES.includes(status)) return;
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const connect = async () => {
      try {
        const { ticket } = await getTrackTicket(id);
        if (cancelled) return;
        const es = new EventSource(`/sse/track/${id}?ticket=${encodeURIComponent(ticket)}`);
        esRef.current = es;
        es.addEventListener("status", () => {
          // A hint, not a payload: refetch and let the GET be the truth.
          queryClient.invalidateQueries({ queryKey: ["order", id] });
        });
        es.addEventListener("reconnect", () => {
          // Jittered lifetime reached (FR-36) — reopen with a fresh ticket.
          es.close();
          if (!cancelled) retry = setTimeout(connect, 250);
        });
        es.onopen = () => setStreaming(true);
        es.onerror = () => {
          // Tickets are single-use, so EventSource's built-in reconnect
          // would just 401 — close, fall back to the poll, try again soon.
          es.close();
          setStreaming(false);
          if (!cancelled) retry = setTimeout(connect, 5000);
        };
      } catch {
        setStreaming(false); // 503 = tracking off; the poll carries on
      }
    };
    connect();
    return () => {
      cancelled = true;
      if (retry) clearTimeout(retry);
      esRef.current?.close();
      setStreaming(false);
    };
  }, [id, status && TERMINAL_STATUSES.includes(status), queryClient]);

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

      {!cancelled && <CourierMap orderId={o.order_id} status={o.status} />}

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


/**
 * The customer's courier dot (dispatch milestone): a 2s authed poll of
 * /v1/deliveries/{id}/courier while a courier could be moving — the
 * poll-floor philosophy (positions are 30s-TTL telemetry in Redis; a
 * poll is exactly as live as the data). 404 = no delivery row yet (the
 * cascade hasn't started) — render nothing, quietly.
 */
const COURIER_PHASES = ["READY", "PICKED_UP"];
function CourierMap({ orderId, status }: { orderId: string; status: string }) {
  const courier = useQuery({
    queryKey: ["courier", orderId],
    queryFn: () => getCourier(orderId),
    enabled: COURIER_PHASES.includes(status),
    refetchInterval: 2000,
    refetchIntervalInBackground: true,
    retry: false, // a 404 is an answer (no rider yet), not a flake
  });
  const view = courier.data;
  if (!COURIER_PHASES.includes(status) || !view) return null;
  const heading =
    view.state === "PICKED_UP"
      ? "Your rider is on the way"
      : view.state === "ASSIGNED"
        ? "A rider is heading to the restaurant"
        : "Finding you a rider…";
  return (
    <div className="card space-y-2">
      <div className="flex items-center justify-between text-sm">
        <b>{heading}</b>
        <span className="text-xs text-slate-500">live · toy-city coordinates</span>
      </div>
      <CityMap className="max-h-80">
        {view.pickup.lat != null && view.pickup.lon != null && (
          <Pin lat={view.pickup.lat} lon={view.pickup.lon} glyph="🍛" label="restaurant" />
        )}
        {view.dropoff.lat != null && view.dropoff.lon != null && (
          <Pin lat={view.dropoff.lat} lon={view.dropoff.lon} glyph="🏠" label="you" />
        )}
        {view.lat != null && view.lon != null && <CourierDot lat={view.lat} lon={view.lon} />}
      </CityMap>
    </div>
  );
}

function CourierDot({ lat, lon }: { lat: number; lon: number }) {
  const p = project(lat, lon);
  return (
    <g>
      <circle cx={p.x} cy={p.y} r={9} fill="#22c55e" stroke="#0f172a" strokeWidth={3} />
      <text x={p.x} y={p.y - 13} textAnchor="middle" fontSize={15}>🛵</text>
    </g>
  );
}
