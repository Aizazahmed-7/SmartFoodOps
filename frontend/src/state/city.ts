import { create } from "zustand";
import { persist } from "zustand/middleware";
import { DEFAULT_CITY } from "../cities";

/** Where the customer is, shared across the app.
 *
 * Browse used to hold this in local state, which was fine while it was the
 * only thing that cared. The assistant panel is global and geo-scoped
 * (FR-63), so a second copy would let it answer about a different city than
 * the one whose restaurants are on screen — and nothing would look wrong.
 *
 * Persisted: a returning customer is almost always in the same city, and
 * re-picking it on every load is the kind of friction nobody reports.
 *
 * `version: 1` is not decoration. A stored value always beats
 * `DEFAULT_CITY` on rehydration, so changing which city is the default
 * does nothing for anyone who has already run the app — and the reason
 * Islamabad became the default is that couriers only operate there. A
 * returning tester would have gone on placing Rawalpindi orders that sit
 * at READY until they cancel. The bump drops the stored value once.
 */
interface CityState {
  city: string;
  setCity: (city: string) => void;
}

export const useCity = create<CityState>()(
  persist(
    (set) => ({ city: DEFAULT_CITY, setCity: (city) => set({ city }) }),
    { name: "sfo-city", version: 1 },
  ),
);
