// The ONE quote pipeline Cart and Checkout share: the queryKey and the
// enabled gate live here so the two pages can never quote differently.
//
// There is no longer a pinned menu version to re-sync. Placement consents to
// a TOTAL (expected_total_cents, ADR-0036) and Checkout sends the total from
// this very quote, so the price shown and the price consented to are the
// same value by construction — the divergence this hook was created to
// prevent is now unrepresentable rather than merely centralised.

import { useQuery } from "@tanstack/react-query";
import { getQuote } from "../api/client";
import { useAuth } from "../state/auth";
import { useCart } from "../state/cart";

export function useQuote() {
  const { claims } = useAuth();
  const restaurantId = useCart((c) => c.restaurantId);
  const lines = useCart((c) => c.lines);

  // The server is the only pricer: re-quote whenever the cart changes.
  return useQuery({
    queryKey: ["quote", restaurantId, lines],
    queryFn: () => getQuote(restaurantId!, lines),
    enabled: !!claims && lines.length > 0,
    retry: false,
  });
}
