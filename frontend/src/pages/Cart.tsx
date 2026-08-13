import { Link, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { hasCode } from "../api/errors";
import { useQuote } from "../hooks/useQuote";
import { lineTotalCents, useCart } from "../state/cart";
import { Money, Note } from "../components/ui";
import { useAuth } from "../state/auth";

/** Per-code inline messages — the pricing taxonomy, in human. */
function QuoteError({ error, names }: { error: unknown; names: Map<string, string> }) {
  if (hasCode(error, "RESTAURANT_CLOSED"))
    return <Note tone="warn">This restaurant isn't taking orders right now.</Note>;
  if (hasCode(error, "ITEM_UNAVAILABLE")) {
    // details carry item_ids — translate to the names on the lines.
    const gone = (error.details ?? [])
      .map((d) => (d as { item_id?: string }).item_id ?? d.field)
      .map((id) => (id ? (names.get(id) ?? id) : ""))
      .filter(Boolean);
    return <Note tone="warn">No longer available: {gone.join(", ")} — remove to continue.</Note>;
  }
  if (hasCode(error, "VALIDATION_FAILED"))
    return <Note tone="warn">{error.details?.[0]?.issue ?? error.message}</Note>;
  if (error instanceof ApiError) return <Note tone="warn">{error.message}</Note>;
  return null;
}

export default function Cart() {
  const cart = useCart();
  const { claims } = useAuth();
  const navigate = useNavigate();
  const estimate = cart.lines.reduce((sum, l) => sum + lineTotalCents(l), 0);

  // Quoting + menu_version re-pin live in useQuote, shared with Checkout.
  const quote = useQuote();

  if (cart.lines.length === 0) {
    return (
      <div className="py-16 text-center text-slate-500">
        <p className="text-lg">Your cart is empty.</p>
        <Link to="/" className="btn-primary mt-4 inline-block">Browse restaurants</Link>
      </div>
    );
  }

  const totals = quote.data?.totals;
  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-bold">{cart.restaurantName}</h1>
        <button className="text-xs text-slate-500 hover:text-red-300" onClick={cart.clear}>
          Clear cart
        </button>
      </div>

      {cart.lines.map((line) => (
        <div key={line.key} className="card flex items-center gap-3">
          <div className="flex-1">
            <p className="font-medium">{line.name}</p>
            {line.options.length > 0 && (
              <p className="text-xs text-slate-400">
                {line.options.map((o) => o.name).join(", ")}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button className="btn-ghost px-2.5 py-1"
              onClick={() => cart.setQty(line.key, line.qty - 1)}>−</button>
            <span className="w-6 text-center tabular-nums">{line.qty}</span>
            <button className="btn-ghost px-2.5 py-1"
              onClick={() => cart.setQty(line.key, line.qty + 1)}>+</button>
          </div>
          <div className="w-20 text-right font-medium">
            <Money cents={lineTotalCents(line)} />
          </div>
        </div>
      ))}

      {totals ? (
        <div className="card space-y-1 text-sm">
          <div className="flex justify-between text-slate-400">
            <span>Subtotal</span><Money cents={totals.subtotal_cents} />
          </div>
          <div className="flex justify-between text-slate-400">
            <span>Delivery fee</span><Money cents={totals.fee_cents} />
          </div>
          <div className="flex justify-between text-slate-400">
            <span>Tax</span><Money cents={totals.tax_cents} />
          </div>
          <div className="mt-1 flex justify-between border-t border-slate-800 pt-2 text-base font-bold">
            <span>Total</span><Money cents={totals.total_cents} />
          </div>
          <p className="text-xs text-slate-500">Priced by the server just now · menu v{quote.data!.menu_version}</p>
        </div>
      ) : (
        <div className="card flex items-center justify-between">
          <div>
            <p className="font-semibold">Estimated total</p>
            <p className="text-xs text-slate-500">
              {claims ? (quote.isFetching ? "Getting the exact price…" : "") : "Sign in for an exact quote."}
            </p>
          </div>
          <p className="text-xl font-bold"><Money cents={estimate} /></p>
        </div>
      )}

      <QuoteError
        error={quote.error}
        names={new Map(cart.lines.map((l) => [l.itemId, l.name]))}
      />

      <button
        className="btn-primary w-full"
        disabled={!!quote.error}
        onClick={() => navigate("/checkout")}
      >
        Checkout{totals ? <> · <Money cents={totals.total_cents} /></> : null}
      </button>
    </div>
  );
}
