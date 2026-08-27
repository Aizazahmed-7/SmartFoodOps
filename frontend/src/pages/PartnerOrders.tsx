// The kitchen screen (S6/S7 surfaces): four status-grouped queues off ONE
// batched query. Freshness is push-over-pull: the bell's SSE hints
// invalidate ["feed"] the moment an order arrives or cancels, our own
// actions invalidate on settle, and a 15s floor bounds what push can miss
// (courier pickups emit no hint). Decisions are honest-async — a 202 means
// "the saga has it"; the next refresh shows the truth, including a
// customer beating us to it.

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  acceptOrder, getRestaurantOrders, markPreparing, markReady, rejectOrder,
} from "../api/client";
import type { FeedOrder, OrderStatus } from "../api/types";
import { ErrorNote, Money, StatusTag } from "../components/ui";

// The whole board, oldest-first across queues, in one request.
const BOARD: OrderStatus[] = ["CONFIRMED", "ACCEPTED", "PREPARING", "READY"];

function useBoard() {
  return useQuery({
    queryKey: ["feed"],
    queryFn: () => getRestaurantOrders(BOARD),
    refetchInterval: 15000, // the floor; hints carry the speed
    refetchIntervalInBackground: true, // kitchen tablets live in background tabs
  });
}

function FeedCard({
  order,
  actions,
  branchLabel,
}: {
  order: FeedOrder;
  actions: { label: string; run: (id: string) => Promise<unknown>; danger?: boolean }[];
  branchLabel?: string;
}) {
  const queryClient = useQueryClient();
  const act = useMutation({
    mutationFn: ({ run }: { run: (id: string) => Promise<unknown> }) => run(order.order_id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["feed"] }),
  });
  return (
    <div className="card space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs text-slate-500">
          {branchLabel && <span className="tag mr-1 bg-sky-950 text-sky-300">{branchLabel}</span>}
          {order.order_id.slice(0, 12)}… · {new Date(order.placed_at).toLocaleTimeString()}
        </span>
        <span className="font-semibold"><Money cents={order.total_cents} /></span>
      </div>
      <ul className="text-sm">
        {order.items.map((item, i) => (
          <li key={i}>{item.qty} × {item.name}</li>
        ))}
      </ul>
      {actions.length > 0 && (
        <div className="flex gap-2">
          {actions.map((a) => (
            <button
              key={a.label}
              className={`${a.danger ? "btn-danger" : "btn-primary"} flex-1 px-2 py-1.5 text-sm`}
              disabled={act.isPending}
              onClick={() => act.mutate({ run: a.run })}
            >
              {a.label}
            </button>
          ))}
        </div>
      )}
      <ErrorNote error={act.error} />
    </div>
  );
}

function Queue({
  title,
  orders,
  loaded,
  actions,
  empty,
  branchLabels,
}: {
  title: string;
  orders: FeedOrder[];
  loaded: boolean;
  actions: (order: FeedOrder) => { label: string; run: (id: string) => Promise<unknown>; danger?: boolean }[];
  empty: string;
  branchLabels?: Record<string, string>;
}) {
  return (
    <section className="space-y-2">
      <h2 className="flex items-center gap-2 text-lg font-semibold">
        {title}
        {orders.length > 0 && (
          <span className="tag bg-orange-500/20 text-orange-300">{orders.length}</span>
        )}
      </h2>
      {loaded && orders.length === 0 && <p className="text-sm text-slate-500">{empty}</p>}
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {orders.map((o) => (
          <FeedCard
            key={o.order_id}
            order={o}
            actions={actions(o)}
            branchLabel={o.restaurant_id ? branchLabels?.[o.restaurant_id] : undefined}
          />
        ))}
      </div>
    </section>
  );
}

export default function PartnerOrders({
  branchLabels,
}: {
  // Branch label per ticket (ADR-0028) — shown only for multi-branch brands.
  branchLabels?: Record<string, string>;
} = {}) {
  const labels =
    branchLabels && Object.keys(branchLabels).length > 1 ? branchLabels : undefined;
  const board = useBoard();
  const byStatus = useMemo(() => {
    const groups: Record<string, FeedOrder[]> = {};
    for (const o of board.data?.items ?? []) (groups[o.status] ??= []).push(o);
    return groups;
  }, [board.data]);
  const loaded = board.data !== undefined;
  return (
    <div className="space-y-8">
      <ErrorNote error={board.error} />
      {board.data?.next_cursor && (
        <p className="text-sm text-amber-300">
          Showing the oldest 100 active tickets — more are waiting. Work the board down!
        </p>
      )}
      <Queue
        title="Incoming"
        orders={byStatus["CONFIRMED"] ?? []}
        loaded={loaded}
        branchLabels={labels}
        empty="No new orders — they appear here the moment payment clears."
        actions={() => [
          { label: "Accept", run: acceptOrder },
          { label: "Reject", run: rejectOrder, danger: true },
        ]}
      />
      <Queue
        title="Accepted"
        orders={byStatus["ACCEPTED"] ?? []}
        loaded={loaded}
        branchLabels={labels}
        empty="Nothing accepted yet."
        actions={() => [{ label: "Start preparing", run: markPreparing }]}
      />
      <Queue
        title="In the kitchen"
        orders={byStatus["PREPARING"] ?? []}
        loaded={loaded}
        branchLabels={labels}
        empty="Nothing on the stove."
        actions={() => [{ label: "Food is ready", run: markReady }]}
      />
      <Queue
        title="Awaiting pickup"
        orders={byStatus["READY"] ?? []}
        loaded={loaded}
        branchLabels={labels}
        empty="Nothing waiting for a courier."
        actions={() => []}
      />
      <StatusNote />
    </div>
  );
}

function StatusNote() {
  return (
    <p className="text-xs text-slate-500">
      Queues update live (new and cancelled orders push instantly); a 15s
      refresh guards the stream. An order can leave ANY queue without your
      action: the customer may cancel until the courier picks up, and
      un-answered incoming orders time out. <StatusTag status="READY" /> orders
      leave when the courier collects them.
    </p>
  );
}
