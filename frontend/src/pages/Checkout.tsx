import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  ApiError, clearIdemKey, listAddresses, placeOrder, toOrderLines,
} from "../api/client";
import { hasCode } from "../api/errors";
import { useQuote } from "../hooks/useQuote";
import { useAuth } from "../state/auth";
import { useCart } from "../state/cart";
import { ErrorNote, Money, Note, Section } from "../components/ui";

/** The mock-PSP's magic tokens — failure injection is a dropdown away. */
const CARDS = [
  { token: "tok_ok", label: "Visa ····4242 (approves)" },
  { token: "tok_decline", label: "Visa ····0002 (declines)" },
  { token: "tok_timeout", label: "Visa ····0044 (PSP hangs — watch the retry)" },
  { token: "tok_unknown", label: "Visa ····0999 (PSP 500s then recovers)" },
];

export default function Checkout() {
  const { claims } = useAuth();
  const cart = useCart();
  const navigate = useNavigate();
  const location = useLocation();
  const [addressId, setAddressId] = useState<string | null>(null);
  const [cardToken, setCardToken] = useState(CARDS[0].token);
  const [priceChanged, setPriceChanged] = useState(false);

  const addresses = useQuery({
    queryKey: ["addresses"],
    queryFn: listAddresses,
    enabled: !!claims,
  });
  // Quoting + menu_version re-pin live in useQuote, shared with Cart — the
  // shown price and the version placement consents to can never diverge.
  const quote = useQuote();

  const chosenAddress = addressId ?? addresses.data?.[0]?.id ?? null;

  const place = useMutation({
    mutationFn: () =>
      placeOrder({
        restaurant_id: cart.restaurantId!,
        menu_version: cart.menuVersion!, // consent to THIS menu — drift → PRICE_CHANGED
        address_id: chosenAddress!,
        card_token: cardToken,
        lines: toOrderLines(cart.lines),
      }),
    onSuccess: (placed) => {
      clearIdemKey(); // this key's job is done forever
      cart.clear();
      navigate(`/orders/${placed.order_id}`);
    },
    onError: (error) => {
      if (hasCode(error, "PRICE_CHANGED")) {
        // Re-quote (fresh totals + version) and ask for one tap of consent;
        // useQuote re-pins the cart's menu_version when the fresh quote lands.
        setPriceChanged(true);
        quote.refetch();
      }
    },
  });

  if (!claims)
    return (
      <div className="py-16 text-center">
        <p className="text-slate-400">Sign in to check out.</p>
        {/* Carry the interrupted location — sign-in must land back HERE. */}
        <Link to="/login" state={{ from: location }} className="btn-primary mt-4 inline-block">
          Sign in
        </Link>
      </div>
    );
  if (cart.lines.length === 0)
    return (
      <div className="py-16 text-center text-slate-500">
        <p>Nothing to check out.</p>
        <Link to="/" className="btn-primary mt-4 inline-block">Browse restaurants</Link>
      </div>
    );

  const totals = quote.data?.totals;
  const blocked =
    !chosenAddress || !totals || place.isPending || (quote.isFetching && priceChanged);

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="text-xl font-bold">Checkout</h1>

      <Section title="Deliver to">
        {addresses.data?.length ? (
          <div className="grid gap-2 sm:grid-cols-2">
            {addresses.data.map((a) => (
              <label key={a.id} className="card flex cursor-pointer items-center gap-3">
                <input
                  type="radio"
                  name="addr"
                  className="accent-orange-500"
                  checked={chosenAddress === a.id}
                  onChange={() => setAddressId(a.id)}
                />
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

      <Section title="Pay with">
        <div className="grid gap-2 sm:grid-cols-2">
          {CARDS.map((c) => (
            <label key={c.token} className="card flex cursor-pointer items-center gap-3">
              <input
                type="radio"
                name="card"
                className="accent-orange-500"
                checked={cardToken === c.token}
                onChange={() => setCardToken(c.token)}
              />
              <span className="text-sm">{c.label}</span>
            </label>
          ))}
        </div>
        <p className="text-xs text-slate-500">
          Your card is only <em>held</em> now — money moves after delivery.
        </p>
      </Section>

      <Section title="Order summary">
        <div className="card space-y-1 text-sm">
          {(quote.data?.lines ?? []).map((l, i) => (
            <div key={i} className="flex justify-between">
              <span>{l.qty} × {l.name}</span>
              <Money cents={l.line_total_cents} />
            </div>
          ))}
          {totals && (
            <>
              <div className="flex justify-between text-slate-400">
                <span>Delivery + tax</span>
                <Money cents={totals.fee_cents + totals.tax_cents} />
              </div>
              <div className="mt-2 flex justify-between border-t border-slate-800 pt-2 font-semibold">
                <span>Total</span>
                <Money cents={totals.total_cents} />
              </div>
            </>
          )}
        </div>
      </Section>

      {quote.error instanceof ApiError && (
        <Note tone="warn">
          {"Couldn't price your cart: "}{quote.error.message}{" — "}
          <button className="underline" onClick={() => quote.refetch()}>try again</button>.
        </Note>
      )}
      {priceChanged && totals && (
        <Note tone="warn">
          The menu changed while you were deciding — the new total is{" "}
          <strong><Money cents={totals.total_cents} /></strong>. Placing the order
          confirms the new price.
        </Note>
      )}
      {!hasCode(place.error, "PRICE_CHANGED") ? (
        <ErrorNote error={place.error} />
      ) : null}

      <button
        className="btn-primary w-full"
        disabled={blocked}
        onClick={() => { setPriceChanged(false); place.mutate(); }}
      >
        {place.isPending ? "Placing…" : priceChanged ? "Confirm new price & place order" : "Place order"}
        {totals ? <> · <Money cents={totals.total_cents} /></> : null}
      </button>
    </div>
  );
}
