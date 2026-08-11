import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { Claims } from "../api/types";

// The ADR-0020 client contract: a half-finished onboarding is persisted and
// silently replayed on every app load until the grant lands.
export interface PendingOnboarding {
  name: string;
  city: string;
  cuisines: string[];
}

interface AuthState {
  access: string | null;
  refresh: string | null;
  claims: Claims | null;
  pendingOnboarding: PendingOnboarding | null;
  setTokens: (access: string, refresh: string) => void;
  setPendingOnboarding: (p: PendingOnboarding | null) => void;
  logout: () => void;
}

export function decodeClaims(token: string): Claims {
  const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(atob(payload)) as Claims;
}

export const useAuth = create<AuthState>()(
  persist(
    (set) => ({
      access: null,
      refresh: null,
      claims: null,
      pendingOnboarding: null,
      setTokens: (access, refresh) =>
        set({ access, refresh, claims: decodeClaims(access) }),
      setPendingOnboarding: (pendingOnboarding) => set({ pendingOnboarding }),
      logout: () => set({ access: null, refresh: null, claims: null }),
    }),
    { name: "sfo-auth" },
  ),
);
