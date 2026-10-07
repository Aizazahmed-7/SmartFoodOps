// The toy world's cities — the ONE source of truth for every city chip,
// select, and default across the app. Ids are what the API stores and
// browse filters on (lowercase slugs, exact match server-side); labels are
// what people see. The first entry is the default everywhere, and its
// coordinate box is the one CityMap draws.
//
// These ids are the same ones `make seed` builds its world under — they were
// not, for a while, and a seeded world unreachable from the city chips was
// the result.
// Islamabad first, because rider-sim's courier start corners all sit in the
// Islamabad box: with Rawalpindi first, the app's DEFAULT screen was a city
// no simulated courier could serve, and every order placed without touching
// a chip sat at READY until it cancelled. Exactly the failure the note above
// describes, arriving from the other direction.
//
// This order does NOT reach CityMap. That component has its own hardcoded
// `CITY` box which must be kept equal to the first seed box by hand — an
// earlier version of this comment claimed the two were linked, and they
// were not.
export const CITIES = [
  { id: "islamabad", label: "Islamabad" },
  { id: "rawalpindi", label: "Rawalpindi" },
] as const;

export const DEFAULT_CITY: string = CITIES[0].id;
