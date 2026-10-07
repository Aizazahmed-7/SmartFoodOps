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
  Branch,
  BrowseResult,
  CancelResult,
  CourierView,
  DecisionResult,
  Feed,
  Menu,
  MenuItem,
  NotificationList,
  OrderDetail,
  ContentDraft,
  FeedbackDigest,
  OrderExplanation,
  OrderFeedback,
  OrderList,
  OrderStatus,
  PlacedOrder,
  Profile,
  Quote,
  RestaurantCard,
  RiderMe,
  SearchResult,
  StockRow,
  TokenPair,
 RestaurantAnalytics,
  AskAccepted,
  Recommendations,
  AssistantCard,
  AssistantTicket } from "./types";

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

export const listBranches = (brandId: string) =>
  request<{ branches: Branch[] }>("GET", `/v1/restaurants/${brandId}/branches`);
export const createBranch = (
  brandId: string,
  body: { branch_label: string; city: string; lat?: number; lon?: number },
) => request<Branch>("POST", `/v1/restaurants/${brandId}/branches`, body);
export const setBaseItemAvailability = (branchId: string, itemId: string, available: boolean) =>
  request<{ item_id: string; available: boolean }>(
    "PUT",
    `/v1/restaurants/${branchId}/base-items/${itemId}/availability`,
    { available },
  );

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
  /** The total the customer was SHOWN and is consenting to (ADR-0036).
   *  The server reprices from its own snapshot and refuses on mismatch —
   *  this is consent, never an asserted price. */
  expected_total_cents: number;
  address_id: string;
  card_token: string;
  lines: ReturnType<typeof toOrderLines>;
}

/**
 * Placement is one request again (ADR-0024): a retry with the same key
 * either finds the order (the server replays it from the row) or attaches
 * to the workflow already making it — the server never answers "busy,
 * come back", so there is no 409-wait loop to run here.
 */
export const placeOrder = (body: PlaceOrderBody) =>
  request<PlacedOrder>("POST", "/v1/orders", body, {
    headers: { "Idempotency-Key": idemKeyFor(body) },
  });

/** Buy a 60s single-use ticket for the SSE stream (FR-38): EventSource
 * cannot send Authorization, and a JWT in a query string would soak into
 * access logs — so the authed POST trades the JWT for a ticket, and the
 * stream URL carries only that. 503 = tracking off; the poll carries on. */
export const getTrackTicket = (orderId: string) =>
  request<{ ticket: string; expires_in: number; stream: string }>(
    "POST", "/v1/track/ticket", { order_id: orderId },
  );

export const listOrders = (cursor?: string) =>
  request<OrderList>("GET", `/v1/orders${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`);

export const getOrder = (orderId: string) =>
  request<OrderDetail>("GET", `/v1/orders/${orderId}`);

/** 202 submitted / 200 already done — both resolve; 409 (courier has it) throws. */
export const cancelOrder = (orderId: string) =>
  request<CancelResult>("POST", `/v1/orders/${orderId}/cancel`);

// ── kitchen: feed, decisions, prep (W2, partner side) ──────────────

export const getRestaurantOrders = (statuses: OrderStatus[], cursor?: string) =>
  request<Feed>(
    "GET",
    // Repeated status params: ONE round trip fetches every queue the page
    // renders; limit=100 (the backend max) — a kitchen wants the whole board.
    `/v1/restaurant/orders?${statuses.map((s) => `status=${s}`).join("&")}&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
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

export const getRestaurantAnalytics = (days: number) =>
  request<RestaurantAnalytics>("GET", `/v1/restaurant/analytics?days=${days}`);

export const getStock = (rid: string) =>
  request<{ items: StockRow[]; capacity: number | null }>(
    "GET", `/v1/inventory/restaurants/${rid}/stock`);
export const setStock = (rid: string, itemId: string, available: number) =>
  request<StockRow>("PUT", `/v1/inventory/restaurants/${rid}/stock/${itemId}`, { available });
export const setCapacity = (rid: string, capacity: number) =>
  request<{ restaurant_id: string; capacity: number; active: number }>(
    "PUT", `/v1/inventory/restaurants/${rid}/capacity`, { capacity });

// ── notifications ──────────────────────────────────────────────────

/** S9: a 60s single-use ticket for the bell stream — the caller's own
 * identity is the channel; there is nothing else to name. 503 = push off;
 * the 15s poll carries on. */
// ── dispatch: the rider surface + the courier dot ──────────────────

export const setRiderStatus = (online: boolean, position?: { lat: number; lon: number }) =>
  request<{ status: string }>("POST", "/v1/rider/status", { online, ...(position ?? {}) });
export const getRiderMe = () => request<RiderMe>("GET", "/v1/rider/me");
export const acceptRiderOffer = (offerId: string, orderId: string) =>
  request<{ status: string }>("POST", `/v1/rider/offers/${offerId}/accept`, {
    order_id: orderId,
  });
export const tapDelivery = (orderId: string, action: "pickup" | "deliver") =>
  request<{ status: string }>("POST", `/v1/rider/deliveries/${orderId}/${action}`);
export const getCourier = (orderId: string) =>
  request<CourierView>("GET", `/v1/deliveries/${orderId}/courier`);

/** B5: why is my order where it is. 404 = unknown or not yours (the two
 * are one answer by design), which the caller renders as "no explanation"
 * rather than as an error. */
export const getOrderExplanation = (orderId: string) =>
  request<OrderExplanation>("GET", `/v1/assistant/orders/${orderId}/explanation`);

/** B6: rate a delivered order. PUT because one row per order — re-sending
 * is the same statement, and a correction replaces rather than conflicts. */
export const putOrderFeedback = (orderId: string, rating: number, comment: string | null) =>
  request<{ order_id: string; rating: number; comment: string | null }>(
    "PUT", `/v1/orders/${orderId}/feedback`, { rating, comment: comment || null },
  );

/** 404 = never rated (or not yours), which the caller renders as "ask". */
export const getOrderFeedback = (orderId: string) =>
  request<OrderFeedback>("GET", `/v1/orders/${orderId}/feedback`);

// ── the content studio (B6) ────────────────────────────────────────

export const listDrafts = (params: { status?: string; kind?: string } = {}) => {
  const q = new URLSearchParams();
  if (params.status) q.set("status", params.status);
  if (params.kind) q.set("kind", params.kind);
  return request<{ drafts: ContentDraft[] }>("GET", `/v1/assistant/drafts?${q}`);
};

export const draftMenuItems = (body: { item_ids?: string[]; category?: string; request?: string }) =>
  request<{ draft_ids: string[]; queued: number; skipped: number }>(
    "POST", "/v1/assistant/drafts/menu-items", body,
  );

export const draftBusinessCopy = (kind: "promotions" | "engagement", ask: string) =>
  request<{ draft_id: string; kind: string }>(
    "POST", `/v1/assistant/drafts/${kind}`, { request: ask },
  );

/** Records the decision. The MENU WRITE is a separate, ordinary Catalog
 * PATCH the caller makes first — the assistant has no menu-write authority
 * (ADR-0029), and the order matters: a failure between the two leaves a
 * draft that still needs action rather than a false record of publication. */
export const approveDraft = (draftId: string, publishedContent: string) =>
  request<ContentDraft>(
    "POST", `/v1/assistant/drafts/${draftId}/approve`, { published_content: publishedContent },
  );

export const rejectDraft = (draftId: string) =>
  request<{ draft_id: string; status: string }>("POST", `/v1/assistant/drafts/${draftId}/reject`);

export const replayDraft = (draftId: string) =>
  request<{ draft_id: string; status: string }>("POST", `/v1/assistant/drafts/${draftId}/replay`);

export const getFeedbackDigest = () =>
  request<FeedbackDigest>("GET", "/v1/assistant/feedback");

export const requestFeedbackSummary = () =>
  request<{ draft_id: string; kind: string }>("POST", "/v1/assistant/feedback/summary");

export const getNotifyTicket = () =>
  request<{ ticket: string; expires_in: number; stream: string }>(
    "POST", "/v1/notifications/ticket",
  );

export const listNotifications = (cursor?: string) =>
  request<NotificationList>(
    "GET", `/v1/notifications${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`);
export const markNotificationRead = (id: string) =>
  request<{ id: string; read_at: string }>("POST", `/v1/notifications/${id}/read`);
export const markAllNotificationsRead = () =>
  request<{ marked: number }>("POST", "/v1/notifications/read-all");

export { decodeClaims };


// ── the assistant (B3) ─────────────────────────────────────────────

/** 202, not 200: the answer does not exist yet. The turn runs detached and
 * the body carries an id and a stream ticket. `Idempotency-Key` makes a
 * retry stream the turn already running rather than paying for a second
 * generation (FR-67). */
export const askAssistant = (body: {
  question: string;
  city: string;
  conversation_id?: string;
}) =>
  request<AskAccepted>("POST", "/v1/assistant/messages", body, {
    // A fresh key per ask, NOT `idemKeyFor`: that helper keeps one slot in
    // localStorage keyed by body hash, so a question would evict the
    // checkout key, and asking the same thing twice on purpose would
    // silently re-stream the first answer. What the key protects here is
    // the transparent 401-refresh retry inside `request`.
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });

/** A fresh ticket for a stream already in flight. Without this, resumption
 * does not work in a browser at all: `EventSource` reconnects on its own, to
 * the same URL, carrying the ticket it already spent. */
export const getAssistantTicket = (messageId: string) =>
  request<AssistantTicket>("POST", `/v1/assistant/messages/${messageId}/ticket`);

/** The dishes the answer named, priced NOW. A separate read from the stream
 * on purpose — the prose is final the moment it is written and a price is
 * not, so a reader who comes back tomorrow gets yesterday's words and
 * today's menu (FR-60). */
export const getAssistantItems = (messageId: string) =>
  request<{ items: AssistantCard[] }>("GET", `/v1/assistant/messages/${messageId}/items`);

/** What to order, for a customer who has not asked anything (FR-80), or who
 * gave a budget (FR-76). Reading this is what records the showing the
 * acceptance rate is measured against — so it is a GET with a side effect,
 * deliberately: the alternative is a client that reports its own
 * impressions, which is the thing FR-79 rules out. */
export const getRecommendations = (city: string, budgetCents?: number) =>
  request<Recommendations>(
    "GET",
    `/v1/assistant/recommendations?city=${encodeURIComponent(city)}` +
      (budgetCents ? `&budget_cents=${budgetCents}` : ""),
  );
