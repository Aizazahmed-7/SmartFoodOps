// The ADR-0017 client-side cart: item IDs, quantities, and chosen modifier
// IDs — plus DISPLAY prices from the browsed menu. Prices here are estimates
// for the UI only; the backend re-prices everything at quote/placement and
// answers PRICE_CHANGED on drift. Persisted in localStorage.

import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface CartOption {
  groupId: string;
  groupName: string;
  optionId: string;
  name: string;
  deltaCents: number;
}

export interface CartLine {
  key: string; // itemId + sorted option ids — same choice merges, different splits
  itemId: string;
  name: string;
  priceCents: number;
  qty: number;
  options: CartOption[];
}

interface CartState {
  restaurantId: string | null;
  restaurantName: string | null;
  menuVersion: number | null;
  lines: CartLine[];
  add: (
    restaurant: { id: string; name: string; version: number },
    line: Omit<CartLine, "key" | "qty">,
  ) => "added" | "different-restaurant";
  setQty: (key: string, qty: number) => void;
  clear: () => void;
}

export function lineTotalCents(line: CartLine): number {
  const options = line.options.reduce((sum, o) => sum + o.deltaCents, 0);
  return (line.priceCents + options) * line.qty;
}

export const useCart = create<CartState>()(
  persist(
    (set, get) => ({
      restaurantId: null,
      restaurantName: null,
      menuVersion: null,
      lines: [],
      add: (restaurant, line) => {
        const state = get();
        if (state.restaurantId && state.restaurantId !== restaurant.id) {
          return "different-restaurant"; // one restaurant per order — caller confirms a clear
        }
        const key =
          line.itemId + ":" + line.options.map((o) => o.optionId).sort().join("+");
        const existing = state.lines.find((l) => l.key === key);
        set({
          restaurantId: restaurant.id,
          restaurantName: restaurant.name,
          menuVersion: restaurant.version,
          lines: existing
            ? state.lines.map((l) => (l.key === key ? { ...l, qty: l.qty + 1 } : l))
            : [...state.lines, { ...line, key, qty: 1 }],
        });
        return "added";
      },
      setQty: (key, qty) =>
        set((state) => ({
          lines:
            qty <= 0
              ? state.lines.filter((l) => l.key !== key)
              : state.lines.map((l) => (l.key === key ? { ...l, qty } : l)),
        })),
      clear: () =>
        set({ restaurantId: null, restaurantName: null, menuVersion: null, lines: [] }),
    }),
    { name: "sfo-cart" },
  ),
);
