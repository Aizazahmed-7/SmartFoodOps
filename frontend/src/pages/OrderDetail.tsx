import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import {
  cancelOrder, getCourier, getOrder, getOrderExplanation, getOrderFeedback,
  getTrackTicket, putOrderFeedback,
} from "../api/client";
import { hasCode } from "../api/errors";
import {
  CANCEL_FAMILY, CANCELLABLE_STATUSES, RATEABLE_STATUSES, SETTLED_REASONS, TERMINAL_STATUSES,
  type OrderDetail as OrderDetailT, type OrderExplanation, type OrderStatus,
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

      {cancelled ? null : <Journey status={o.status} />}

      <Explanation order={o} cancelled={cancelled} />

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

      {RATEABLE_STATUSES.includes(o.status) && <Feedback orderId={o.order_id} />}

      <Link to="/orders" className="inline-block text-sm text-slate-400 hover:text-white">
        ← All orders
      </Link>
    </div>
  );
}


/**
 * Why the order is where it is (B5, FR-83/FR-86).
 *
 * The cancellation copy that used to live in this file is gone. It was a
 * hardcoded map of reason slugs plus a hardcoded set of reasons where a
 * card hold existed — and that set omitted `no_rider_available`, so the one
 * customer whose food WAS cooked and then binned was told their card was
 * never charged. The server derives that from `confirmed_at` instead,
 * which is the actual evidence, and it cannot get the set wrong because it
 * is not a set.
 *
 * Failure is silence. An order page that works is worth more than an
 * explanation: if the assistant is down, or the order is not ours, or the
 * backend predates B5, the customer still sees their food, their money and
 * their journey bar. That is also why this is its own component — a failing
 * query here must not take the page with it.
 */
/**
 * Severity from the BUCKET, not the status. A cancelled order is over and
 * reads as an error; a blown deadline or a very long stage is worth
 * colouring; everything else is neutral, because most of the time the
 * honest message is "this is going normally and here is where it is".
 */
function tone(view: OrderExplanation, cancelled: boolean): "info" | "warn" | "error" {
  if (cancelled) return "error";
  return view.bucket === "overdue" || view.bucket === "long" ? "warn" : "info";
}

function Explanation({ order, cancelled }: { order: OrderDetailT; cancelled: boolean }) {
  const orderId = order.order_id;
  const explanation = useQuery({
    queryKey: ["explanation", orderId],
    queryFn: () => getOrderExplanation(orderId),
    // Re-ask while the order is still moving: the same cause reads
    // differently as time passes ("just sent" becomes "9 minutes"), so a
    // once-fetched sentence would quietly go stale on screen.
    refetchInterval: (query) =>
      query.state.data && SETTLED_REASONS.has(query.state.data.reason) ? false : 15_000,
    retry: false, // a 404 is an answer, not a flake
  });

  const view = explanation.data;
  // No explanation — the assistant is down, or the backend predates B5, or
  // the server could not fill its own copy in ("fallback", which is true
  // but useless next to a status the page already shows).
  //
  // For a LIVE order that is fine: the journey bar and the courier map
  // still say where things are. For a CANCELLED one it is not. Part A had
  // a local banner here and removing it made a core screen depend on an
  // optional GenAI service — a customer whose order died would have seen a
  // status tag and nothing about why, or about their money. `cancel_reason`
  // is still on the order payload, so the floor costs two lines.
  if (!view || view.source === "fallback") {
    return cancelled ? <CancelledFloor order={order} /> : null;
  }

  return (
    <Note tone={tone(view, cancelled)}>
      {/* `data-reason` is the stable contract. The prose is server-owned
          and a model may rewrite its wording (B5's polish layer), so
          anything asserting on the words is asserting on a sample. */}
      <span data-testid="order-explanation" data-reason={view.reason}>
        {view.text}
      </span>
    </Note>
  );
}

/**
 * The offline floor for a cancelled order.
 *
 * Deliberately thinner than the server's copy: it names the cause and
 * says nothing about money. Part A's version guessed at the card hold from
 * a hardcoded list of reasons and got FR-32 wrong; the evidence for that
 * claim is a timestamp only the backend has, so when the backend is not
 * answering, the honest thing is to not make the claim.
 */
function CancelledFloor({ order }: { order: OrderDetailT }) {
  const reason = order.cancel_reason;
  return (
    <Note tone="error">
      <span data-testid="order-explanation" data-reason="offline">
        Order {order.status === "CANCELLING" ? "is being cancelled" : "was cancelled"}
        {reason ? ` — ${reason.replace(/_/g, " ")}` : ""}.
      </span>
    </Note>
  );
}

/**
 * Rate a delivered order (B6, FR-91).
 *
 * Part A captured no feedback of any kind, so this control is the entire
 * corpus FR-92's summaries will be built from — which is why it collects a
 * number first and a sentence only if someone wants to leave one. Most
 * people will not, and a form that demands prose collects nothing.
 *
 * Shows back what was already said rather than asking twice. The rating is
 * correctable: the API is a PUT, and a customer who clicks 4 then 5 meant
 * 5 — treating the first click as final would be an interface deciding
 * something the schema does not.
 */
function Feedback({ orderId }: { orderId: string }) {
  const queryClient = useQueryClient();
  const existing = useQuery({
    queryKey: ["feedback", orderId],
    queryFn: () => getOrderFeedback(orderId),
    retry: false, // a 404 is an answer (not rated yet), not a flake
  });
  const [comment, setComment] = useState("");
  const [touched, setTouched] = useState(false);

  const saved = existing.data;
  const rating = saved?.rating ?? 0;

  const submit = useMutation({
    mutationFn: (next: { rating: number; comment: string | null }) =>
      putOrderFeedback(orderId, next.rating, next.comment),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["feedback", orderId] }),
  });

  if (existing.isLoading) return null;

  return (
    <div className="card space-y-2">
      <div className="flex items-center justify-between">
        <b className="text-sm">{saved ? "Your rating" : "How was it?"}</b>
        <div className="flex gap-1">
          {[1, 2, 3, 4, 5].map((n) => (
            <button
              key={n}
              aria-label={`${n} star${n > 1 ? "s" : ""}`}
              data-testid={`rate-${n}`}
              disabled={submit.isPending}
              className={`text-xl leading-none ${n <= rating ? "text-amber-400" : "text-slate-600"}`}
              onClick={() => submit.mutate({ rating: n, comment: saved?.comment ?? null })}
            >
              ★
            </button>
          ))}
        </div>
      </div>

      {/* The sentence is optional and secondary — asked for only once a
          rating exists, because a form that demands prose collects nothing. */}
      {rating > 0 && (
        <>
          <textarea
            className="input h-16 w-full text-sm"
            placeholder="Anything you'd like the restaurant to know? (optional)"
            value={touched ? comment : (saved?.comment ?? "")}
            onChange={(e) => {
              setTouched(true);
              setComment(e.target.value);
            }}
          />
          {touched && (
            <button
              className="btn-primary w-full"
              disabled={submit.isPending}
              onClick={() => {
                submit.mutate({ rating, comment: comment.trim() || null });
                setTouched(false);
              }}
            >
              {submit.isPending ? "Saving…" : "Save comment"}
            </button>
          )}
        </>
      )}
      <ErrorNote error={submit.error} />
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
