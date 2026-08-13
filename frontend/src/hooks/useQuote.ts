// The ONE quote pipeline Cart and Checkout share. The queryKey, the enabled
// gate, and — critically — the menu_version re-pin all live here: a quote
// names the menu version the server priced against, and placement consents
// to exactly that version. When only Cart re-pinned, Checkout could display
// a fresh price while placing with a stale pinned version — a guaranteed
// spurious PRICE_CHANGED consent loop. One hook, no divergence.

import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { getQuote } from "../api/client";
import { useAuth } from "../state/auth";
import { useCart } from "../state/cart";

export function useQuote() {
  const { claims } = useAuth();
  const restaurantId = useCart((c) => c.restaurantId);
  const lines = useCart((c) => c.lines);
  const menuVersion = useCart((c) => c.menuVersion);
  const setMenuVersion = useCart((c) => c.setMenuVersion);

  // The server is the only pricer: re-quote whenever the cart changes.
  const quote = useQuery({
    queryKey: ["quote", restaurantId, lines],
    queryFn: () => getQuote(restaurantId!, lines),
    enabled: !!claims && lines.length > 0,
    retry: false,
  });

  // A quote self-heals across menu edits — its response carries the version
  // the cart should re-pin to (no PRICE_CHANGED at this stage by design).
  useEffect(() => {
    if (quote.data && quote.data.menu_version !== menuVersion) {
      setMenuVersion(quote.data.menu_version); // re-pin: placement consents to THIS menu
    }
  }, [quote.data, menuVersion, setMenuVersion]);

  return quote;
}
