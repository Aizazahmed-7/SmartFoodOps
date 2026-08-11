import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { browse } from "../api/client";
import type { RestaurantCard } from "../api/types";
import { ErrorNote, Spinner } from "../components/ui";

const CITIES = ["springfield", "shelbyville"];
const CUISINES = ["pakistani", "burgers", "italian", "japanese", "mexican", "indian",
                  "vietnamese", "middle-eastern", "chinese", "pizza"];
const TAGS = ["vegetarian", "vegan", "halal", "spicy"];

export function RestaurantTile({ r }: { r: RestaurantCard }) {
  return (
    <Link to={`/r/${r.id}`} className="card block transition hover:border-orange-500/50">
      <div className="flex items-start justify-between">
        <h3 className="font-semibold">{r.name}</h3>
        {r.status === "paused" && (
          <span className="tag bg-red-950 text-red-300">closed</span>
        )}
      </div>
      <p className="mt-1 text-xs capitalize text-slate-400">{r.city}</p>
      <div className="mt-2 flex flex-wrap gap-1">
        {r.cuisines.map((c) => <span key={c} className="tag">{c}</span>)}
      </div>
    </Link>
  );
}

export default function Browse() {
  const [city, setCity] = useState(CITIES[0]);
  const [cuisine, setCuisine] = useState<string | undefined>();
  const [tag, setTag] = useState<string | undefined>();
  const [pages, setPages] = useState(1);

  const query = useQuery({
    queryKey: ["browse", city, cuisine, tag, pages],
    queryFn: async () => {
      const results = await Promise.all(
        Array.from({ length: pages }, (_, p) => browse(city, { cuisine, tag, page: p })),
      );
      return {
        restaurants: results.flatMap((r) => r.restaurants),
        hasMore: results[results.length - 1].has_more,
      };
    },
  });

  const chip = (active: boolean) => `chip ${active ? "chip-active" : ""}`;
  const pick = (setter: (v: string | undefined) => void, current?: string) =>
    (value: string) => { setter(current === value ? undefined : value); setPages(1); };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {CITIES.map((c) => (
          <button key={c} className={chip(city === c)}
            onClick={() => { setCity(c); setPages(1); }}>
            {c}
          </button>
        ))}
        <span className="mx-1 text-slate-700">|</span>
        {CUISINES.map((c) => (
          <button key={c} className={chip(cuisine === c)} onClick={() => pick(setCuisine, cuisine)(c)}>
            {c}
          </button>
        ))}
        <span className="mx-1 text-slate-700">|</span>
        {TAGS.map((t) => (
          <button key={t} className={chip(tag === t)} onClick={() => pick(setTag, tag)(t)}>
            {t}
          </button>
        ))}
      </div>

      {query.isLoading && <Spinner />}
      <ErrorNote error={query.error} />
      {query.data && (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {query.data.restaurants.map((r) => <RestaurantTile key={r.id} r={r} />)}
          </div>
          {query.data.restaurants.length === 0 && (
            <p className="py-10 text-center text-slate-500">Nothing here — try another filter.</p>
          )}
          {query.data.hasMore && (
            <div className="flex justify-center">
              <button className="btn-ghost" onClick={() => setPages((p) => p + 1)}>
                Load more
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
