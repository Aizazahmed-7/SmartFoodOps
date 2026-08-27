import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { getMenu } from "../api/client";
import type { MenuItem, ModifierGroup } from "../api/types";
import { useCart, type CartOption } from "../state/cart";
import { ErrorNote, Money, Spinner } from "../components/ui";

/** min/max-aware option picker. Radio when exactly one, checkboxes otherwise —
 * client-side UX only; the backend re-validates at pricing (W2). */
function ModifierDialog({
  item, restaurant, onClose,
}: {
  item: MenuItem;
  restaurant: { id: string; name: string; version: number };
  onClose: () => void;
}) {
  const add = useCart((c) => c.add);
  const clear = useCart((c) => c.clear);
  const [chosen, setChosen] = useState<Record<string, string[]>>(
    Object.fromEntries(item.modifier_groups.map((g) => [g.id, []])),
  );
  const [conflict, setConflict] = useState(false);

  const toggle = (group: ModifierGroup, optionId: string) => {
    setChosen((prev) => {
      const current = prev[group.id];
      if (group.min_select === 1 && group.max_select === 1)
        return { ...prev, [group.id]: [optionId] }; // radio
      if (current.includes(optionId))
        return { ...prev, [group.id]: current.filter((o) => o !== optionId) };
      if (current.length >= group.max_select) return prev; // cap reached
      return { ...prev, [group.id]: [...current, optionId] };
    });
  };

  const satisfied = item.modifier_groups.every(
    (g) => chosen[g.id].length >= g.min_select,
  );
  const options: CartOption[] = item.modifier_groups.flatMap((g) =>
    chosen[g.id].map((optionId) => {
      const o = g.options.find((x) => x.id === optionId)!;
      return { groupId: g.id, groupName: g.name, optionId, name: o.name,
               deltaCents: o.price_delta_cents };
    }),
  );
  const total = item.price_cents + options.reduce((s, o) => s + o.deltaCents, 0);

  const submit = (clearFirst = false) => {
    if (clearFirst) clear();
    const result = add(restaurant, {
      itemId: item.id, name: item.name, priceCents: item.price_cents, options,
    });
    if (result === "different-restaurant") setConflict(true);
    else onClose();
  };

  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}>
      <div className="card w-full max-w-md space-y-4" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-lg font-semibold">{item.name}</h3>
        {item.modifier_groups.map((g) => (
          <div key={g.id}>
            <p className="mb-2 text-sm font-medium text-slate-300">
              {g.name}
              <span className="ml-2 text-xs text-slate-500">
                {g.min_select === 1 && g.max_select === 1
                  ? "choose one"
                  : `choose ${g.min_select === 0 ? "up to" : `${g.min_select}–`}${g.max_select}`}
              </span>
            </p>
            <div className="flex flex-wrap gap-2">
              {g.options.map((o) => (
                <button key={o.id}
                  className={`chip ${chosen[g.id].includes(o.id) ? "chip-active" : ""}`}
                  onClick={() => toggle(g, o.id)}>
                  {o.name}
                  {o.price_delta_cents !== 0 && (
                    <span className="ml-1 opacity-70">
                      +<Money cents={o.price_delta_cents} />
                    </span>
                  )}
                </button>
              ))}
            </div>
          </div>
        ))}
        {conflict ? (
          <div className="space-y-2">
            <p className="text-sm text-amber-300">
              Your cart holds items from another restaurant — one restaurant per order.
            </p>
            <div className="flex gap-2">
              <button className="btn-danger flex-1" onClick={() => submit(true)}>
                Clear cart & add
              </button>
              <button className="btn-ghost flex-1" onClick={onClose}>Keep cart</button>
            </div>
          </div>
        ) : (
          <button className="btn-primary w-full" disabled={!satisfied}
            onClick={() => submit()}>
            Add to cart · <Money cents={total} />
          </button>
        )}
      </div>
    </div>
  );
}

export default function RestaurantPage() {
  const { id } = useParams<{ id: string }>();
  const [picking, setPicking] = useState<MenuItem | null>(null);
  const query = useQuery({ queryKey: ["menu", id], queryFn: () => getMenu(id!) });

  if (query.isLoading) return <Spinner />;
  if (query.error) return <ErrorNote error={query.error} />;
  const menu = query.data!;
  const restaurant = {
    id: menu.restaurant_id,
    name: menu.display_name ?? menu.name,
    version: menu.version,
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold">{menu.name}</h1>
        {menu.status === "paused" && (
          <span className="tag bg-red-950 text-red-300">currently closed</span>
        )}
      </div>
      {menu.categories.map((cat) => (
        <section key={cat.id}>
          <h2 className="mb-3 text-lg font-semibold text-slate-300">{cat.name}</h2>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {cat.items.map((item) => (
              <div key={item.id} className={`card ${item.available ? "" : "opacity-50"}`}>
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <h3 className="font-medium">{item.name}</h3>
                    {item.description && (
                      <p className="mt-0.5 text-xs text-slate-400">{item.description}</p>
                    )}
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {item.tags.map((t) => <span key={t} className="tag">{t}</span>)}
                    </div>
                  </div>
                  <div className="text-right">
                    <p className="font-semibold"><Money cents={item.price_cents} /></p>
                    {item.available && menu.status !== "paused" ? (
                      <button className="btn-primary mt-2 px-3 py-1 text-xs"
                        onClick={() => {
                          if (item.modifier_groups.length > 0) return setPicking(item);
                          const cart = useCart.getState();
                          const line = { itemId: item.id, name: item.name,
                                         priceCents: item.price_cents, options: [] };
                          if (cart.add(restaurant, line) === "different-restaurant" &&
                              confirm("Your cart holds another restaurant's items. Clear it?")) {
                            cart.clear();
                            cart.add(restaurant, line);
                          }
                        }}>
                        Add
                      </button>
                    ) : (
                      <span className="mt-2 block text-xs text-slate-500">
                        {item.available ? "closed" : "sold out"}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>
      ))}
      {picking && (
        <ModifierDialog item={picking} restaurant={restaurant}
          onClose={() => setPicking(null)} />
      )}
    </div>
  );
}
