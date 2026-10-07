// Hand-written mirrors of the gateway DTOs (see /openapi.json).

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export type Role = "customer" | "restaurant_admin" | "rider" | "system_admin";

export interface Claims {
  sub: string;
  // A SET since multi-role: a promoted owner keeps `customer`. Optional
  // because zustand persists claims — a returning user may still hold a
  // token minted before this claim existed, until their next refresh.
  roles?: Role[];
  restaurant_id?: string;
  rider_id?: string;
  exp: number;
}

export interface Profile {
  id: string;
  email: string;
  roles: string[];
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
  cuisines: string[];
  lat?: number | null;
  lon?: number | null;
  // NULL only on a BRAND: place-shaped fields live on the branch since
  // catalog migration 0009. Browse and Search cards are always branches,
  // so they always carry these — the partner console's base-menu scope is
  // the one place a brand reaches this type.
  city: string | null;
  status: "open" | "paused" | null;
  // Brands (ADR-0028): cards are branches; title by display_name.
  brand_id?: string | null;
  branch_label?: string | null;
  display_name?: string;
}

export interface Branch {
  id: string;
  brand_id: string | null;
  branch_label: string | null;
  name: string;
  display_name: string;
  city: string;
  status: "open" | "paused";
  lat: number | null;
  lon: number | null;
  // A branch is the only holder of a schedule (catalog 0009).
  hours: Record<string, string[]> | null;
  timezone: string;
}

export interface Restaurant extends RestaurantCard {
  lat: number | null;
  lon: number | null;
  hours: Record<string, string[]> | null;
  timezone: string | null;
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
  // "base" = inherited from the brand (86 via the availability endpoint);
  // "local" = this scope owns the row (ordinary PATCH/DELETE).
  source?: "base" | "local";
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
  display_name?: string;
  brand_id?: string | null;
  status: string;
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

// `CancelReason` used to live here: a hand-kept mirror of the saga's
// vocabulary, used to turn a slug into a sentence on the order page. It
// had already fallen behind — `no_rider_available` was missing — and the
// server now owns that copy (FR-86), so the mirror is gone rather than
// left to drift further. The wire field stays `string | null` below,
// which is what it always was.

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
  restaurant_id?: string; // which branch this ticket belongs to
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
}

// ── notifications ──────────────────────────────────────────────────

export interface NotificationRow {
  id: string;
  order_id: string;
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

/**
 * Why an order is where it is (B5, FR-83/FR-86).
 *
 * `reason` is the closed set the resolver produces and is what this app
 * branches on — never the prose. The copy is server-owned so it can state
 * facts only the backend has (how long a kitchen has been cooking, whether
 * a card hold existed), and it will change; a UI that pattern-matched
 * English would break the next time it improved.
 */
export interface OrderExplanation {
  order_id: string;
  reason: string;
  text: string;
  /** How long the current stage has been running, bucketed. */
  bucket: string;
  locale: string;
  /** "template" normally; "fallback" means the server could not fill its
   * own copy in — a defect, not a customer-facing state. */
  source: string;
}

/** Reasons where the order is over and there is nothing left to wait for. */
export const SETTLED_REASONS = new Set([
  "delivered",
  "refunded",
  "cancelled_by_customer",
  "cancelled_by_restaurant",
  "cancelled_restaurant_silent",
  "cancelled_item_unavailable",
  "cancelled_kitchen_full",
  "cancelled_payment_declined",
  "cancelled_no_courier",
  "cancelled_system",
]);

/** What a customer said about a delivered order (B6, FR-91). */
export interface OrderFeedback {
  order_id: string;
  rating: number;
  comment: string | null;
  submitted_at: string;
}

/** Orders a customer may rate — mirrors order's RATEABLE_STATUSES.
 * REFUNDED is deliberately absent: the food arrived, but a refund means
 * something went wrong that a 1-5 scale cannot express. */
export const RATEABLE_STATUSES: OrderStatus[] = ["DELIVERED", "SETTLED"];

// ── the content studio (B6) ────────────────────────────────────────

/** One piece of generated copy and everything that happened to it.
 *
 * `status` is the whole lifecycle — there is no second place to look.
 * `parked` is the dead-letter queue, which is a ROW rather than a broker
 * artifact precisely so this screen can show it: the restaurant whose copy
 * never arrived sees that it did not, and why.
 */
export interface ContentDraft {
  draft_id: string;
  /** Where a menu write for this draft has to go. A base item belongs to
   * the brand and a branch-local item to the branch (ADR-0028), so the
   * console cannot infer it from the dashboard's selected scope. */
  restaurant_id: string;
  brand_id: string | null;
  kind: "menu_item" | "promotion" | "engagement" | "feedback_summary";
  status: "queued" | "drafted" | "parked" | "published" | "rejected";
  target_id: string | null;
  request: string | null;
  /** The facts the copy was written FROM, frozen when the admin asked. */
  subject: Record<string, unknown> | null;
  /** What the model wrote. */
  content: string | null;
  /** What the admin actually published, which may differ. */
  published_content: string | null;
  model: string | null;
  /** Why it parked, in words an operator can act on. */
  error: string | null;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
}

export interface FeedbackDigest {
  counts: {
    reviews: number;
    with_comment: number;
    average_rating: number;
    ratings: Record<string, number>;
  };
  /** False below the floor — the rows are shown instead, which is the
   * honest answer at that size rather than a degraded one. */
  can_summarise: boolean;
  summary: { themes: string[]; quotes: string[]; model: string; drafted_at: string } | null;
  feedback: { order_id: string; rating: number; comment: string | null; submitted_at: string }[];
}

// ── the assistant (B3) ─────────────────────────────────────────────

export interface AskAccepted {
  conversation_id: string;
  /** The turn runs DETACHED, so this arrives before any answer exists —
   * which is what makes the stream resumable at all (ADR-0042 §1). */
  message_id: string;
  /** Single-use. `EventSource` cannot set headers and a JWT in a query
   * string soaks into access logs, so the ticket is the stream's auth. */
  ticket: string;
  stream: string;
}

export interface AssistantTicket {
  ticket: string;
  stream: string;
  expires_in: number;
}

/** One SSE frame. `item_ids` rides only the terminal frame — they are known
 * once grounding has run. */
export interface AssistantChunk {
  seq: number;
  text: string;
  done: boolean;
  item_ids?: string[];
}

/** A dish the answer named, priced at READ time (FR-60). Never cached with
 * the prose: the words are final, the price is not. */
export interface AssistantCard {
  item_id: string;
  name: string;
  price_cents: number;
  currency: string;
  /** The dish is 86'd. */
  available: boolean;
  /** Addable right now: available AND the kitchen is open and unpaused.
   * Kept apart from `available` so a closed kitchen does not read as a dish
   * that is gone — it is back at 6pm. */
  orderable: boolean;
  /** The least this dish can cost once required options are satisfied. A
   * budget is measured against THIS, not `price_cents` — a dish with a
   * required paid option cannot be bought for its base price (FR-76). */
  min_total_cents: number;
  /** The dish has a modifier group with `min_select >= 1`. It cannot be
   * added from here: the cart would hold a line the quote endpoint refuses,
   * and nothing in the app can repair one. */
  needs_choice: boolean;
  restaurant_id: string;
  restaurant_name: string;
  open_now: boolean;
}

/** Two dishes people actually order together, from co-order signal within
 * one restaurant (FR-77). Carries its own cards: a combo's dishes are
 * usually not in the recommendation list, so ids alone would be unrenderable. */
export interface AssistantCombo {
  items: AssistantCard[];
  total_cents: number;
  /** How many orders contained both. This is evidence, not a score. */
  orders: number;
}

export interface Recommendations {
  /** `taste` when the customer has enough history, `popular` when they do
   * not (FR-75/FR-80). Shown to the customer, because "because you often
   * order…" and "popular near you" are different promises. */
  basis: "taste" | "popular";
  city: string;
  budget_cents: number | null;
  /** What the budget was measured against. `dish_subtotal` means delivery
   * fee and tax are NOT included — the customer is choosing dishes, not
   * approving a total. */
  budget_applies_to: "dish_subtotal";
  items: AssistantCard[];
  combos: AssistantCombo[];
}
