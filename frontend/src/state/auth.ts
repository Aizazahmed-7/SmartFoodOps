import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { Claims, Role } from "../api/types";

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

/** Does this session hold `role`? Ordering the calls at a branch IS the
 *  precedence rule — check restaurant_admin before rider before customer.
 *  Falsy for a pre-multi-role token still sitting in localStorage; that
 *  self-corrects at the next refresh (access tokens live 15 minutes). */
export function hasRole(claims: Claims | null, role: Role): boolean {
  return claims?.roles?.includes(role) ?? false;
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
