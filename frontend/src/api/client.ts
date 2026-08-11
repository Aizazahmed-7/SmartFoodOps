// One thin, typed gateway client. Auth is transparent: requests carry the
// access token; an AUTH_TOKEN_EXPIRED 401 triggers ONE single-flight refresh
// (the rotation also picks up new claims — e.g. the restaurant_admin grant,
// ADR-0020) and the request retries once.

import { decodeClaims, useAuth } from "../state/auth";
import type {
  Address,
  BrowseResult,
  Menu,
  MenuItem,
  Profile,
  RestaurantCard,
  SearchResult,
  TokenPair,
} from "./types";

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
    public details?: { field: string; issue: string }[],
  ) {
    super(message);
  }
}

/** Marks a seam whose backend arrives with Week 2 — the UI shows it as such. */
export class NotBuiltYet extends Error {
  constructor(public feature: string) {
    super(`${feature} arrives with the Week 2 order APIs`);
  }
}

let refreshing: Promise<void> | null = null;

async function refreshTokens(): Promise<void> {
  refreshing ??= (async () => {
    const { refresh, setTokens, logout } = useAuth.getState();
    if (!refresh) {
      logout();
      throw new ApiError("AUTH_INVALID_CREDENTIALS", "not signed in", 401);
    }
    const resp = await fetch("/v1/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!resp.ok) {
      logout(); // rotated family / reuse-revoked / expired — start over
      throw new ApiError("AUTH_INVALID_CREDENTIALS", "session ended — sign in again", 401);
    }
    const pair = (await resp.json()) as TokenPair;
    setTokens(pair.access_token, pair.refresh_token);
  })().finally(() => {
    refreshing = null;
  });
  return refreshing;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  retry = true,
): Promise<T> {
  const { access } = useAuth.getState();
  const resp = await fetch(path, {
    method,
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(access ? { Authorization: `Bearer ${access}` } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 204) return undefined as T;
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = data?.error ?? {};
    if (err.code === "AUTH_TOKEN_EXPIRED" && retry) {
      await refreshTokens();
      return request<T>(method, path, body, false);
    }
    throw new ApiError(err.code ?? "UNKNOWN", err.message ?? resp.statusText, resp.status, err.details);
  }
  return data as T;
}

// ── auth & account (built) ─────────────────────────────────────────

export async function register(email: string, password: string): Promise<void> {
  await request("POST", "/v1/auth/register", { email, password });
}

export async function login(email: string, password: string): Promise<void> {
  const pair = await request<TokenPair>("POST", "/v1/auth/login", { email, password });
  useAuth.getState().setTokens(pair.access_token, pair.refresh_token);
}

/** Explicit refresh — used right after onboarding so claims carry the grant. */
export const refreshSession = refreshTokens;

export const getProfile = () => request<Profile>("GET", "/v1/auth/me");
export const updateProfile = (changes: { full_name?: string; phone?: string }) =>
  request("PATCH", "/v1/auth/me", changes);

export const listAddresses = () => request<Address[]>("GET", "/v1/me/addresses");
export const addAddress = (a: { label: string; line1: string; city: string }) =>
  request<Address>("POST", "/v1/me/addresses", a);
export const deleteAddress = (id: string) => request("DELETE", `/v1/me/addresses/${id}`);

// ── discovery (built) ──────────────────────────────────────────────

export function browse(city: string, opts: { cuisine?: string; tag?: string; page?: number }) {
  const q = new URLSearchParams({ city });
  if (opts.cuisine) q.set("cuisine", opts.cuisine);
  if (opts.tag) q.set("tag", opts.tag);
  if (opts.page) q.set("page", String(opts.page));
  return request<BrowseResult>("GET", `/v1/restaurants?${q}`);
}

export function search(qs: string, opts: { city?: string; page?: number }) {
  const q = new URLSearchParams({ q: qs });
  if (opts.city) q.set("city", opts.city);
  if (opts.page) q.set("page", String(opts.page));
  return request<SearchResult>("GET", `/v1/search?${q}`);
}

export const getMenu = (restaurantId: string) =>
  request<Menu>("GET", `/v1/menus/${restaurantId}`);

// ── partner (built) ────────────────────────────────────────────────

export async function onboardRestaurant(body: {
  name: string;
  city: string;
  cuisines: string[];
}): Promise<RestaurantCard> {
  const restaurant = await request<RestaurantCard>("POST", "/v1/restaurants", body);
  await refreshTokens(); // the grant landed — next token carries restaurant_admin
  return restaurant;
}

export const pauseRestaurant = (id: string) =>
  request<RestaurantCard>("POST", `/v1/restaurants/${id}/pause`);
export const resumeRestaurant = (id: string) =>
  request<RestaurantCard>("POST", `/v1/restaurants/${id}/resume`);

export const addCategory = (rid: string, name: string, rank: number) =>
  request<{ id: string }>("POST", `/v1/restaurants/${rid}/categories`, { name, rank });
export const deleteCategory = (rid: string, cid: string) =>
  request("DELETE", `/v1/restaurants/${rid}/categories/${cid}`);

export interface ItemPayload {
  category_id: string;
  name: string;
  description?: string;
  price_cents: number;
  tags: string[];
  modifier_groups: {
    name: string;
    min_select: number;
    max_select: number;
    rank: number;
    options: { name: string; price_delta_cents: number; rank: number }[];
  }[];
}

export const addItem = (rid: string, item: ItemPayload) =>
  request<MenuItem>("POST", `/v1/restaurants/${rid}/items`, item);
export const patchItem = (rid: string, itemId: string, changes: Record<string, unknown>) =>
  request<MenuItem>("PATCH", `/v1/restaurants/${rid}/items/${itemId}`, changes);
export const deleteItem = (rid: string, itemId: string) =>
  request("DELETE", `/v1/restaurants/${rid}/items/${itemId}`);

// ── Week 2 seams: real signatures, honest errors ───────────────────
// When POST /v1/quote and POST /v1/orders land, these bodies become one
// `request(...)` call each — nothing else in the UI changes.

export async function getQuote(_cart: unknown): Promise<never> {
  throw new NotBuiltYet("Price quote");
}

export async function placeOrder(_cart: unknown): Promise<never> {
  throw new NotBuiltYet("Order placement");
}

export async function listOrders(): Promise<never> {
  throw new NotBuiltYet("Order history");
}

export { decodeClaims };
