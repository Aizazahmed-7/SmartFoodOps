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
