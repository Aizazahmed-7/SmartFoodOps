import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { listAddresses } from "../api/client";
import { useAuth } from "../state/auth";
import { lineTotalCents, useCart } from "../state/cart";
import { ComingSoon, Money, Section } from "../components/ui";

export default function Checkout() {
  const { claims } = useAuth();
  const cart = useCart();
  const addresses = useQuery({
    queryKey: ["addresses"],
    queryFn: listAddresses,
    enabled: !!claims,
  });
  const total = cart.lines.reduce((sum, l) => sum + lineTotalCents(l), 0);

  if (!claims)
    return (
      <div className="py-16 text-center">
        <p className="text-slate-400">Sign in to check out.</p>
        <Link to="/login" className="btn-primary mt-4 inline-block">Sign in</Link>
      </div>
    );

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="text-xl font-bold">Checkout</h1>

      <Section title="Deliver to">
        {addresses.data?.length ? (
          <div className="grid gap-2 sm:grid-cols-2">
            {addresses.data.map((a) => (
              <label key={a.id} className="card flex cursor-pointer items-center gap-3">
                <input type="radio" name="addr" defaultChecked className="accent-orange-500" />
                <span>
                  <span className="font-medium">{a.label}</span>
                  <span className="block text-xs text-slate-400">{a.line1}, {a.city}</span>
                </span>
              </label>
            ))}
          </div>
        ) : (
          <p className="text-sm text-slate-400">
            No saved addresses — <Link to="/account" className="text-orange-400">add one</Link>.
          </p>
        )}
      </Section>

      <Section title="Order summary">
        <div className="card space-y-1">
          {cart.lines.map((l) => (
            <div key={l.key} className="flex justify-between text-sm">
              <span>{l.qty} × {l.name}</span>
              <Money cents={lineTotalCents(l)} />
            </div>
          ))}
          <div className="mt-2 flex justify-between border-t border-slate-800 pt-2 font-semibold">
            <span>Estimated total</span>
            <Money cents={total} />
          </div>
        </div>
      </Section>

      <ComingSoon>
        Placing the order calls <code className="text-slate-300">POST /v1/orders</code> —
        the Temporal saga, payment authorization, and live tracking arrive with Week 2.
        This screen is wired and waiting.
      </ComingSoon>
      <button className="btn-primary w-full" disabled>
        Place order · <Money cents={total} />
      </button>
    </div>
  );
}
