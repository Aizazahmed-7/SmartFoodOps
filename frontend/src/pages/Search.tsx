import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { search } from "../api/client";
import { CITIES } from "../cities";
import { ErrorNote, Money, Spinner } from "../components/ui";
import { useCity } from "../state/city";

export default function Search() {
  const [text, setText] = useState("");
  const [q, setQ] = useState(""); // debounced
  // Search is geo-scoped, and NOT sending a city is not a neutral default:
  // semantic retrieval requires one (FR-63), so a city-less query silently
  // falls back to lexical matching.
  //
  // The SHARED store, not a local copy. This comment used to claim the
  // chips "mirror Browse's so the two pages agree" while holding its own
  // `useState` — so a customer who picked a city on Browse searched a
  // different one here, and the assistant panel (also geo-scoped, FR-63)
  // answered about a third. Nothing looked wrong on any of the three.
  const { city, setCity } = useCity();
  useEffect(() => {
    const t = setTimeout(() => setQ(text.trim()), 300);
    return () => clearTimeout(t);
  }, [text]);

  const query = useQuery({
    queryKey: ["search", q, city],
    queryFn: () => search(q, { city }),
    enabled: q.length >= 2,
  });

  return (
    <div className="space-y-4">
      <input
        autoFocus
        className="input text-base"
        placeholder="Search dishes or restaurants — typos welcome (try “biriani”)"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="flex flex-wrap items-center gap-2">
        {CITIES.map((c) => (
          <button
            key={c.id}
            className={`chip ${city === c.id ? "chip-active" : ""}`}
            onClick={() => setCity(c.id)}
          >
            {c.label}
          </button>
        ))}
      </div>
      {query.isFetching && <Spinner />}
      <ErrorNote error={query.error} />
      {query.data && (
        <div className="space-y-3">
          {query.data.results.length === 0 && (
            <p className="py-10 text-center text-slate-500">
              No matches for “{q}” in {CITIES.find((c) => c.id === city)?.label ?? city}.
            </p>
          )}
          {query.data.results.map((hit) => (
            <Link key={hit.restaurant.id} to={`/r/${hit.restaurant.id}`}
              className="card block transition hover:border-orange-500/50">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold">
                  {hit.restaurant.display_name ?? hit.restaurant.name}
                </h3>
                <span className="flex items-center gap-2 text-xs capitalize text-slate-500">
                  {hit.restaurant.status === "paused" && (
                    <span className="rounded bg-slate-700 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-300">
                      closed
                    </span>
                  )}
                  {hit.restaurant.city}
                </span>
              </div>
              {hit.matched_items.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {hit.matched_items.map((item) => (
                    <span key={item.id} className="rounded-lg bg-slate-800 px-2 py-1 text-xs">
                      {item.name} · <Money cents={item.price_cents} />
                    </span>
                  ))}
                </div>
              )}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
