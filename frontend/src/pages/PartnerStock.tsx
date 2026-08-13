// The strict-stock control panel (S1): every item is born at 0 and cannot
// sell until stocked — this tab is where an owner turns the tap on. Stock
// rows are keyed by item id; names come from the menu.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { getMenu, getStock, setCapacity, setStock } from "../api/client";
import { ErrorNote, Spinner } from "../components/ui";

function StockLine({
  rid, itemId, name, available,
}: {
  rid: string; itemId: string; name: string; available: number;
}) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState(String(available));
  const parsed = parseInt(value, 10);
  const valid = Number.isInteger(parsed) && parsed >= 0; // empty box ≠ zero
  const save = useMutation({
    mutationFn: () => setStock(rid, itemId, parsed),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["stock", rid] }),
  });
  const out = available === 0;
  return (
    <div className={`card flex items-center gap-3 ${out ? "border-amber-900" : ""}`}>
      <div className="flex-1">
        <p className="font-medium">{name}</p>
        <p className={`text-xs ${out ? "text-amber-400" : "text-slate-500"}`}>
          {out ? "out of stock — customers can't order this" : `${available} available`}
        </p>
      </div>
      <input
        className="input w-24 text-right tabular-nums"
        type="number"
        min={0}
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <button
        className="btn-primary px-3 py-1.5 text-sm"
        disabled={save.isPending || !valid || parsed === available}
        onClick={() => save.mutate()}
      >
        Set
      </button>
      <ErrorNote error={save.error} />
    </div>
  );
}

export default function PartnerStock({ rid }: { rid: string }) {
  const menu = useQuery({ queryKey: ["menu", rid], queryFn: () => getMenu(rid) });
  const stock = useQuery({ queryKey: ["stock", rid], queryFn: () => getStock(rid) });
  const [capacityValue, setCapacityValue] = useState("");
  const saveCapacity = useMutation({
    mutationFn: () => setCapacity(rid, Math.max(1, parseInt(capacityValue, 10) || 1)),
  });

  if (menu.isLoading || stock.isLoading) return <Spinner />;
  const error = menu.error ?? stock.error;
  if (error) return <ErrorNote error={error} />;

  const byId = new Map(stock.data!.items.map((row) => [row.item_id, row]));
  const items = menu.data!.categories.flatMap((c) => c.items);

  return (
    <div className="space-y-6">
      <section className="card flex max-w-md items-center gap-3">
        <div className="flex-1">
          <p className="font-medium">Kitchen capacity</p>
          <p className="text-xs text-slate-500">
            Concurrent orders before new ones get "at capacity".
          </p>
        </div>
        <input
          className="input w-24 text-right tabular-nums"
          type="number"
          min={1}
          placeholder="10"
          value={capacityValue}
          onChange={(e) => setCapacityValue(e.target.value)}
        />
        <button
          className="btn-primary px-3 py-1.5 text-sm"
          disabled={saveCapacity.isPending || !capacityValue}
          onClick={() => saveCapacity.mutate()}
        >
          Set
        </button>
      </section>
      <ErrorNote error={saveCapacity.error} />

      <div className="space-y-2">
        {items.map((item) => (
          <StockLine
            key={`${item.id}:${byId.get(item.id)?.available ?? 0}`} // remount on server change: orders consume stock live
            rid={rid}
            itemId={item.id}
            name={item.name}
            available={byId.get(item.id)?.available ?? 0}
          />
        ))}
        {items.length === 0 && (
          <p className="text-sm text-slate-500">Add menu items first — stock follows the menu.</p>
        )}
      </div>
    </div>
  );
}
