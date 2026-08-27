// The toy world's cities — the ONE source of truth for every city chip,
// select, and default across the app. Ids are what the API stores and
// browse filters on (lowercase slugs, exact match server-side); labels are
// what people see. The first entry is the default everywhere, and its
// coordinate box is the one CityMap draws.
//
// NOTE: the seed/demo scripts still build their world as
// springfield/shelbyville — if you `make seed`, those rows won't appear
// under these chips. This list is for the hand-built world.
export const CITIES = [
  { id: "rawalpindi", label: "Rawalpindi" },
  { id: "islamabad", label: "Islamabad" },
] as const;

export const DEFAULT_CITY: string = CITIES[0].id;
