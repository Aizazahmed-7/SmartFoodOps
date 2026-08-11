import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { getQuote, NotBuiltYet } from "../api/client";
import { lineTotalCents, useCart } from "../state/cart";
import { ComingSoon, Money } from "../components/ui";

export default function Cart() {
  const cart = useCart();
  const navigate = useNavigate();
  const [quoteNote, setQuoteNote] = useState<string | null>(null);
  const total = cart.lines.reduce((sum, l) => sum + lineTotalCents(l), 0);

  if (cart.lines.length === 0) {
    return (
      <div className="py-16 text-center text-slate-500">
        <p className="text-lg">Your cart is empty.</p>
        <Link to="/" className="btn-primary mt-4 inline-block">Browse restaurants</Link>
      </div>
    );
  }

  // The W2 seam: swaps to POST /v1/quote — same pricing code as placement.
  const requestQuote = async () => {
    try {
      await getQuote({ restaurantId: cart.restaurantId, lines: cart.lines });
    } catch (e) {
      if (e instanceof NotBuiltYet) setQuoteNote(e.message);
    }
  };

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-bold">{cart.restaurantName}</h1>
        <button className="text-xs text-slate-500 hover:text-red-300"
          onClick={cart.clear}>
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

      <div className="card flex items-center justify-between">
        <div>
          <p className="font-semibold">Estimated total</p>
          <p className="text-xs text-slate-500">
            Display estimate — the final price is computed server-side at checkout.
          </p>
        </div>
        <p className="text-xl font-bold"><Money cents={total} /></p>
      </div>

      {quoteNote && <ComingSoon>{quoteNote}</ComingSoon>}

      <div className="flex gap-2">
        <button className="btn-ghost flex-1" onClick={requestQuote}>
          Get exact quote
        </button>
        <button className="btn-primary flex-1" onClick={() => navigate("/checkout")}>
          Checkout
        </button>
      </div>
    </div>
  );
}
