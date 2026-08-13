// One thin, typed gateway client. Auth is transparent: requests carry the
// access token; an AUTH_TOKEN_EXPIRED 401 triggers ONE single-flight refresh
// (the rotation also picks up new claims — e.g. the restaurant_admin grant,
// ADR-0020) and the request retries once.

import { decodeClaims, useAuth } from "../state/auth";
import type { CartLine } from "../state/cart";
// Type-only import — erased at compile time, so no runtime cycle with
// errors.ts (which imports the ApiError class from this file).
import type { ErrorCode } from "./errors";
import type {
  Address,
  BrowseResult,
  CancelResult,
  DecisionResult,
  Feed,
  Menu,
  MenuItem,
  OrderDetail,
  OrderList,
  OrderStatus,
  PlacedOrder,
  Profile,
  Quote,
  RestaurantCard,
  SearchResult,
  StockRow,
  TokenPair,
} from "./types";

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
    public details?: { field: string; issue: string }[],
    /** Gateway correlation id — surfaced in the UI so a bug report can be
     * matched to the backend's logs (absent on client-made errors). */
    public requestId?: string,
  ) {
    super(message);
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
  opts: { headers?: Record<string, string>; retry?: boolean } = {},
): Promise<T> {
  const { headers = {}, retry = true } = opts;
  const { access } = useAuth.getState();
  const resp = await fetch(path, {
    method,
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(access ? { Authorization: `Bearer ${access}` } : {}),
      ...headers, // e.g. Idempotency-Key — same headers on the retry below
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 204) return undefined as T;
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    // The gateway envelope. `code` is typed as ErrorCode so comparisons here
    // are spell-checked; codes outside the union still pass through as-is.
    const err = (data?.error ?? {}) as {
      code?: ErrorCode;
      message?: string;
      request_id?: string;
      details?: { field: string; issue: string }[];
    };
    if (err.code === "AUTH_TOKEN_EXPIRED" && retry) {
      await refreshTokens();
      return request<T>(method, path, body, { headers, retry: false });
    }
    throw new ApiError(
      err.code ?? "UNKNOWN", err.message ?? resp.statusText, resp.status, err.details, err.request_id);
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

// ── orders: quote, placement, tracking, cancel (W2) ────────────────

/** Cart lines → the wire shape both quote and placement accept. */
export const toOrderLines = (lines: CartLine[]) =>
  lines.map((l) => ({
    item_id: l.itemId,
    qty: l.qty,
    options: l.options.map((o) => ({ group_id: o.groupId, option_id: o.optionId })),
  }));

export const getQuote = (restaurantId: string, lines: CartLine[]) =>
  request<Quote>("POST", "/v1/quote", {
    restaurant_id: restaurantId,
    lines: toOrderLines(lines),
  });

/**
 * Placement is the one call that needs an Idempotency-Key: it creates a
 * resource whose id the client can't know. The key is minted once per cart
 * BODY and persisted in localStorage — the CART lives in localStorage, so
 * the key must survive exactly as long as the body it protects (a new tab
 * with the same cart must replay, not double-order). A retry reuses it,
 * editing the cart regenerates it, success clears it.
 */
const IDEM_STORE = "sfo-place-idem";

export function idemKeyFor(body: unknown): string {
  const hash = JSON.stringify(body);
  try {
    const stored = JSON.parse(localStorage.getItem(IDEM_STORE) ?? "null");
    if (stored?.hash === hash) return stored.key;
  } catch {
    /* corrupted entry — mint fresh */
  }
  const key = crypto.randomUUID();
  localStorage.setItem(IDEM_STORE, JSON.stringify({ hash, key }));
  return key;
}

export const clearIdemKey = () => localStorage.removeItem(IDEM_STORE);

export interface PlaceOrderBody {
  restaurant_id: string;
  menu_version: number;
  address_id: string;
  card_token: string;
  lines: ReturnType<typeof toOrderLines>;
}

export const placeOrder = (body: PlaceOrderBody) =>
  request<PlacedOrder>("POST", "/v1/orders", body, {
    headers: { "Idempotency-Key": idemKeyFor(body) },
  });

export const listOrders = (cursor?: string) =>
  request<OrderList>("GET", `/v1/orders${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`);

export const getOrder = (orderId: string) =>
  request<OrderDetail>("GET", `/v1/orders/${orderId}`);

/** 202 submitted / 200 already done — both resolve; 409 (courier has it) throws. */
export const cancelOrder = (orderId: string) =>
  request<CancelResult>("POST", `/v1/orders/${orderId}/cancel`);

// ── kitchen: feed, decisions, prep (W2, partner side) ──────────────

export const getRestaurantOrders = (status: OrderStatus, cursor?: string) =>
  request<Feed>(
    "GET",
    // limit=100 (the backend max): a kitchen screen wants the whole queue.
    `/v1/restaurant/orders?status=${status}&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );

export const acceptOrder = (orderId: string) =>
  request<DecisionResult>("POST", `/v1/restaurant/orders/${orderId}/accept`);
export const rejectOrder = (orderId: string) =>
  request<DecisionResult>("POST", `/v1/restaurant/orders/${orderId}/reject`);
export const markPreparing = (orderId: string) =>
  request<{ order_id: string; status: OrderStatus }>(
    "POST", `/v1/restaurant/orders/${orderId}/preparing`);
export const markReady = (orderId: string) =>
  request<{ order_id: string; status: OrderStatus }>(
    "POST", `/v1/restaurant/orders/${orderId}/ready`);

// ── inventory: stock + capacity (strict stock, S1) ─────────────────

export const getStock = (rid: string) =>
  request<{ items: StockRow[] }>("GET", `/v1/inventory/restaurants/${rid}/stock`);
export const setStock = (rid: string, itemId: string, available: number) =>
  request<StockRow>("PUT", `/v1/inventory/restaurants/${rid}/stock/${itemId}`, { available });
export const setCapacity = (rid: string, capacity: number) =>
  request<{ restaurant_id: string; capacity: number; active: number }>(
    "PUT", `/v1/inventory/restaurants/${rid}/capacity`, { capacity });

export { decodeClaims };
