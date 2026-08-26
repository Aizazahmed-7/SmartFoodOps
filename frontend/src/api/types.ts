// Hand-written mirrors of the gateway DTOs (see /openapi.json).

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface Claims {
  sub: string;
  role: "customer" | "restaurant_admin" | "rider" | "system_admin";
  restaurant_id?: string;
  rider_id?: string;
  exp: number;
}

export interface Profile {
  id: string;
  email: string;
  role: string;
  full_name: string | null;
  phone: string | null;
}

export interface Address {
  id: string;
  label: string;
  line1: string;
  city: string;
  lat: number | null;
  lon: number | null;
}

export interface RestaurantCard {
  id: string;
  name: string;
  city: string;
  cuisines: string[];
  status: "open" | "paused";
  version: number;
  lat?: number | null;
  lon?: number | null;
}

export interface Restaurant extends RestaurantCard {
  lat: number | null;
  lon: number | null;
  hours: Record<string, string[]> | null;
}

export interface ModifierOption {
  id: string;
  name: string;
  price_delta_cents: number;
  rank: number;
}

export interface ModifierGroup {
  id: string;
  name: string;
  min_select: number;
  max_select: number;
  rank: number;
  options: ModifierOption[];
}

export interface MenuItem {
  id: string;
  category_id: string;
  name: string;
  description: string | null;
  price_cents: number;
  currency: string;
  available: boolean;
  rank: number;
  tags: string[];
  modifier_groups: ModifierGroup[];
}

export interface MenuCategory {
  id: string;
  name: string;
  rank: number;
  items: MenuItem[];
}

export interface Menu {
  restaurant_id: string;
  name: string;
  status: string;
  version: number;
  categories: MenuCategory[];
}

export interface BrowseResult {
  restaurants: RestaurantCard[];
  page: number;
  has_more: boolean;
}

export interface SearchHit {
  restaurant: RestaurantCard;
  score: number;
  matched_items: { id: string; name: string; price_cents: number; score: number }[];
}

export interface SearchResult {
  query: string;
  page: number;
  has_more: boolean;
  results: SearchHit[];
}

// ── orders (W2) ────────────────────────────────────────────────────

/** The 13-state machine (ARCHITECTURE §6.2), verbatim. */
export type OrderStatus =
  | "PLACED"
  | "VALIDATED"
  | "PAYMENT_CLEARED"
  | "CONFIRMED"
  | "ACCEPTED"
  | "PREPARING"
  | "READY"
  | "PICKED_UP"
  | "DELIVERED"
  | "SETTLED"
  | "CANCELLING"
  | "CANCELLED"
  | "REFUNDED";

/** Money after the saga is done with it: nothing left to change. */
export const TERMINAL_STATUSES: OrderStatus[] = ["SETTLED", "CANCELLED", "REFUNDED"];

/** The FR-21 window — mirror of the backend's CANCELLABLE_STATES. */
export const CANCELLABLE_STATUSES: OrderStatus[] = [
  "PLACED", "VALIDATED", "PAYMENT_CLEARED", "CONFIRMED", "ACCEPTED", "PREPARING", "READY",
];

/** The cancel branch of the machine — an order here never reaches DELIVERED. */
export const CANCEL_FAMILY: OrderStatus[] = ["CANCELLING", "CANCELLED", "REFUNDED"];

/** Why an order died — mirror of the saga's cancel_reason vocabulary. */
export type CancelReason =
  | "item_unavailable"
  | "at_capacity"
  | "payment_declined"
  | "restaurant_rejected"
  | "restaurant_timeout"
  | "customer_cancelled"
  // The saga could not finish a pre-confirmation step in time (ADR-0023
  // follow-up). Pre-kitchen only, and never charged.
  | "system_timeout";

export interface Totals {
  subtotal_cents: number;
  discount_cents: number;
  fee_cents: number;
  tax_cents: number;
  total_cents: number;
}

export interface QuoteLine {
  item_id: string;
  name: string;
  unit_price_cents: number;
  qty: number;
  options: { group_id: string; group_name: string; option_id: string; name: string; price_delta_cents: number }[];
  line_total_cents: number;
}

/** POST /v1/quote — the server is the only pricer (FR-16). */
export interface Quote {
  restaurant_name: string;
  menu_version: number;
  currency: string;
  lines: QuoteLine[];
  totals: Totals;
}

export interface PlacedOrder {
  order_id: string;
  status: OrderStatus;
}

export interface OrderSummary {
  order_id: string;
  restaurant_name: string;
  status: OrderStatus;
  total_cents: number;
  placed_at: string;
}

export interface OrderList {
  items: OrderSummary[];
  next_cursor: string | null;
}

export interface OrderDetail {
  order_id: string;
  status: OrderStatus;
  restaurant_id: string;
  restaurant_name: string;
  menu_version: number;
  placed_at: string;
  cancel_reason: string | null;
  currency: string;
  totals: Totals;
  delivery_address: { address_id: string; label: string; line1: string; city: string };
  items: {
    menu_item_id: string;
    name: string;
    qty: number;
    unit_price_cents: number;
    options: unknown[];
    line_total_cents: number;
  }[];
}

export interface CancelResult {
  order_id: string;
  status: OrderStatus;
  cancel_reason?: string | null;
}

// ── kitchen feed + stock (W2, partner side) ────────────────────────

export interface FeedOrder {
  order_id: string;
  status: OrderStatus;
  placed_at: string;
  total_cents: number;
  currency: string;
  cancel_reason: string | null;
  items: { menu_item_id: string; name: string; qty: number }[];
}

export interface Feed {
  items: FeedOrder[];
  next_cursor: string | null;
}

export interface DecisionResult {
  order_id: string;
  decision: "accept" | "reject";
  status: OrderStatus;
}

export interface StockRow {
  item_id: string;
  available: number;
  version: number;
}

// ── notifications ──────────────────────────────────────────────────

export interface NotificationRow {
  id: string;
  order_id: string;
  kind: string;
  title: string;
  body: string;
  created_at: string;
  read_at: string | null;
}

export interface NotificationList {
  items: NotificationRow[];
  next_cursor: string | null;
  unread: number;
}

/** S7 — the owner's analytics read (claim-scoped; no id travels). */
export interface DayMetrics {
  day: string;
  orders: number;
  cancelled: number;
  delivered: number;
  revenue_cents: number;
}

export interface RestaurantAnalytics {
  restaurant_id: string;
  window_days: number;
  days: DayMetrics[];
  window: { orders: number; settled: number; cancelled: number };
  cancellation_rate: number | null;
  acceptance_rate: number | null;
  totals: {
    orders: number;
    settled: number;
    cancelled: number;
    revenue_cents: number;
    customers: number;
    repeat_customers: number;
    aov_cents: number | null;
    repeat_rate: number | null;
  };
  funnel: {
    views: number;
    viewers: number;
    converted_viewers: number;
    conversion_rate: number | null;
  };
}

// ── dispatch (the rider console + the customer's courier dot) ──────

export interface GeoPoint {
  lat: number | null;
  lon: number | null;
}

export interface RiderOffer {
  offer_id: string;
  order_id: string;
  restaurant_name: string;
  pickup: GeoPoint;
  dropoff: GeoPoint;
}

export interface RiderDelivery extends RiderOffer {
  state: "ASSIGNED" | "PICKED_UP" | string;
}

export interface RiderMe {
  status: "online" | "offline";
  offer: RiderOffer | null;
  delivery: RiderDelivery | null;
}

export interface CourierView {
  state: string;
  lat: number | null;
  lon: number | null;
  pickup: GeoPoint;
  dropoff: GeoPoint;
}
