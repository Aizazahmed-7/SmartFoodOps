import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { search } from "../api/client";
import { ErrorNote, Money, Spinner } from "../components/ui";

export default function Search() {
  const [text, setText] = useState("");
  const [q, setQ] = useState(""); // debounced
  useEffect(() => {
    const t = setTimeout(() => setQ(text.trim()), 300);
    return () => clearTimeout(t);
  }, [text]);

  const query = useQuery({
    queryKey: ["search", q],
    queryFn: () => search(q, {}),
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
      {query.isFetching && <Spinner />}
      <ErrorNote error={query.error} />
      {query.data && (
        <div className="space-y-3">
          {query.data.results.length === 0 && (
            <p className="py-10 text-center text-slate-500">No matches for “{q}”.</p>
          )}
          {query.data.results.map((hit) => (
            <Link key={hit.restaurant.id} to={`/r/${hit.restaurant.id}`}
              className="card block transition hover:border-orange-500/50">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold">
                  {hit.restaurant.display_name ?? hit.restaurant.name}
                </h3>
                <span className="text-xs capitalize text-slate-500">{hit.restaurant.city}</span>
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
