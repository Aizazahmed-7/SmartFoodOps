// The kitchen screen (S6/S7 surfaces): three status-grouped queues polling
// every 3s. Decisions are honest-async — a 202 means "the saga has it";
// the next poll shows the truth, including a customer beating us to it.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  acceptOrder, getRestaurantOrders, markPreparing, markReady, rejectOrder,
} from "../api/client";
import type { FeedOrder, OrderStatus } from "../api/types";
import { ErrorNote, Money, StatusTag } from "../components/ui";

function useFeed(status: OrderStatus) {
  return useQuery({
    queryKey: ["feed", status],
    queryFn: () => getRestaurantOrders(status),
    refetchInterval: 3000,
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
  status,
  actions,
  empty,
  branchLabels,
}: {
  title: string;
  status: OrderStatus;
  actions: (order: FeedOrder) => { label: string; run: (id: string) => Promise<unknown>; danger?: boolean }[];
  empty: string;
  branchLabels?: Record<string, string>;
}) {
  const feed = useFeed(status);
  return (
    <section className="space-y-2">
      <h2 className="flex items-center gap-2 text-lg font-semibold">
        {title}
        {feed.data && feed.data.items.length > 0 && (
          <span className="tag bg-orange-500/20 text-orange-300">{feed.data.items.length}</span>
        )}
      </h2>
      <ErrorNote error={feed.error} />
      {feed.data?.items.length === 0 && <p className="text-sm text-slate-500">{empty}</p>}
      {feed.data?.next_cursor && (
        <p className="text-sm text-amber-300">
          Showing the oldest 100 — more are waiting. Work the queue down!
        </p>
      )}
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {feed.data?.items.map((o) => (
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
  return (
    <div className="space-y-8">
      <Queue
        title="Incoming"
        status="CONFIRMED"
        branchLabels={labels}
        empty="No new orders — they appear here the moment payment clears."
        actions={() => [
          { label: "Accept", run: acceptOrder },
          { label: "Reject", run: rejectOrder, danger: true },
        ]}
      />
      <Queue
        title="Accepted"
        status="ACCEPTED"
        branchLabels={labels}
        empty="Nothing accepted yet."
        actions={() => [{ label: "Start preparing", run: markPreparing }]}
      />
      <Queue
        title="In the kitchen"
        status="PREPARING"
        branchLabels={labels}
        empty="Nothing on the stove."
        actions={() => [{ label: "Food is ready", run: markReady }]}
      />
      <Queue
        title="Awaiting pickup"
        status="READY"
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
      Queues refresh every 3s. An order can leave ANY queue without your
      action: the customer may cancel until the courier picks up, and
      un-answered incoming orders time out. <StatusTag status="READY" /> orders
      leave when the courier collects them.
    </p>
  );
}
