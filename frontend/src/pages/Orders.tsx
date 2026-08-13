import { useInfiniteQuery } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { listOrders } from "../api/client";
import { TERMINAL_STATUSES } from "../api/types";
import { useAuth } from "../state/auth";
import { ErrorNote, Money, Spinner, StatusTag } from "../components/ui";

export default function Orders() {
  const { claims } = useAuth();
  const location = useLocation();
  const orders = useInfiniteQuery({
    queryKey: ["orders"],
    queryFn: ({ pageParam }) => listOrders(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!claims,
    // Poll while anything on the FIRST page is still moving (that's where
    // live orders sort); go quiet when all terminal.
    refetchInterval: (query) =>
      query.state.data?.pages[0]?.items.some((o) => !TERMINAL_STATUSES.includes(o.status))
        ? 3000
        : false,
    refetchIntervalInBackground: true, // tracking keeps moving in a background tab
  });

  if (!claims)
    return (
      <div className="py-16 text-center">
        <p className="text-slate-400">Sign in to see your orders.</p>
        {/* Carry the interrupted location — sign-in must land back HERE. */}
        <Link to="/login" state={{ from: location }} className="btn-primary mt-4 inline-block">
          Sign in
        </Link>
      </div>
    );
  if (orders.isLoading) return <Spinner />;

  const items = orders.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <h1 className="text-xl font-bold">Your orders</h1>
      <ErrorNote error={orders.error} />
      {items.length === 0 && (
        <p className="py-12 text-center text-slate-500">
          No orders yet — <Link to="/" className="text-orange-400">find something tasty</Link>.
        </p>
      )}
      <div className="space-y-2">
        {items.map((o) => (
          <Link key={o.order_id} to={`/orders/${o.order_id}`}
            className="card flex items-center justify-between transition hover:border-slate-700">
            <div>
              <p className="font-medium">{o.restaurant_name}</p>
              <p className="text-xs text-slate-500">
                {new Date(o.placed_at).toLocaleString()}
              </p>
            </div>
            <div className="text-right">
              <StatusTag status={o.status} />
              <p className="mt-1 text-sm font-semibold">
                <Money cents={o.total_cents} />
              </p>
            </div>
          </Link>
        ))}
      </div>
      {orders.hasNextPage && (
        <button
          className="btn-ghost w-full"
          disabled={orders.isFetchingNextPage}
          onClick={() => orders.fetchNextPage()}
        >
          {orders.isFetchingNextPage ? "Loading…" : "Load older orders"}
        </button>
      )}
    </div>
  );
}
