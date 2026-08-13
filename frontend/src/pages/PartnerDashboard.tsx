import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import {
  addCategory, addItem, deleteCategory, deleteItem, getMenu, patchItem,
  pauseRestaurant, resumeRestaurant, type ItemPayload,
} from "../api/client";
import type { MenuItem } from "../api/types";
import { useAuth } from "../state/auth";
import { ErrorNote, Money, Spinner } from "../components/ui";
import PartnerOrders from "./PartnerOrders";
import PartnerStock from "./PartnerStock";

type Tab = "orders" | "menu" | "stock";

interface GroupDraft {
  name: string;
  min_select: number;
  max_select: number;
  options: { name: string; price_delta_cents: number }[];
}

function ItemForm({
  categoryId, onSave, onClose, error, busy,
}: {
  categoryId: string;
  onSave: (item: ItemPayload) => void;
  onClose: () => void;
  error: unknown;
  busy: boolean;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [price, setPrice] = useState("");
  const [tags, setTags] = useState("");
  const [groups, setGroups] = useState<GroupDraft[]>([]);

  const patchGroup = (i: number, changes: Partial<GroupDraft>) =>
    setGroups((gs) => gs.map((g, j) => (j === i ? { ...g, ...changes } : g)));

  const submit = () =>
    onSave({
      category_id: categoryId,
      name,
      description: description || undefined,
      price_cents: Math.round(parseFloat(price) * 100),
      tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      modifier_groups: groups.map((g, rank) => ({
        name: g.name, min_select: g.min_select, max_select: g.max_select, rank,
        options: g.options.map((o, orank) => ({ ...o, rank: orank })),
      })),
    });

  return (
    <div className="fixed inset-0 z-20 flex items-start justify-center overflow-y-auto bg-black/70 p-4"
      onClick={onClose}>
      <div className="card my-8 w-full max-w-lg space-y-3" onClick={(e) => e.stopPropagation()}>
        <h3 className="font-semibold">New item</h3>
        <div className="grid grid-cols-3 gap-2">
          <input className="input col-span-2" placeholder="Name" value={name}
            onChange={(e) => setName(e.target.value)} />
          <input className="input" placeholder="Price ($)" inputMode="decimal" value={price}
            onChange={(e) => setPrice(e.target.value)} />
        </div>
        <input className="input" placeholder="Description (optional)" value={description}
          onChange={(e) => setDescription(e.target.value)} />
        <input className="input" placeholder="Tags, comma-separated (halal, spicy…)"
          value={tags} onChange={(e) => setTags(e.target.value)} />

        {groups.map((g, i) => (
          <div key={i} className="rounded-xl border border-slate-800 p-3 space-y-2">
            <div className="grid grid-cols-4 gap-2">
              <input className="input col-span-2" placeholder="Group (e.g. Size)" value={g.name}
                onChange={(e) => patchGroup(i, { name: e.target.value })} />
              <input className="input" type="number" min={0} title="min choices"
                value={g.min_select}
                onChange={(e) => patchGroup(i, { min_select: +e.target.value })} />
              <input className="input" type="number" min={1} title="max choices"
                value={g.max_select}
                onChange={(e) => patchGroup(i, { max_select: +e.target.value })} />
            </div>
            {g.options.map((o, oi) => (
              <div key={oi} className="grid grid-cols-3 gap-2">
                <input className="input col-span-2" placeholder="Option name" value={o.name}
                  onChange={(e) =>
                    patchGroup(i, {
                      options: g.options.map((x, xi) =>
                        xi === oi ? { ...x, name: e.target.value } : x),
                    })} />
                <input className="input" placeholder="+¢" type="number"
                  value={o.price_delta_cents}
                  onChange={(e) =>
                    patchGroup(i, {
                      options: g.options.map((x, xi) =>
                        xi === oi ? { ...x, price_delta_cents: +e.target.value } : x),
                    })} />
              </div>
            ))}
            <button type="button" className="btn-ghost px-2 py-1 text-xs"
              onClick={() =>
                patchGroup(i, { options: [...g.options, { name: "", price_delta_cents: 0 }] })}>
              + option
            </button>
          </div>
        ))}
        <button type="button" className="btn-ghost px-2 py-1 text-xs"
          onClick={() =>
            setGroups([...groups, { name: "", min_select: 0, max_select: 1,
                                    options: [{ name: "", price_delta_cents: 0 }] }])}>
          + modifier group
        </button>

        <ErrorNote error={error} />
        <div className="flex gap-2">
          <button className="btn-primary flex-1" disabled={busy || !name || !price}
            onClick={submit}>
            Save item
          </button>
          <button className="btn-ghost" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

export default function PartnerDashboard() {
  const { claims } = useAuth();
  const location = useLocation();
  const rid = claims?.restaurant_id;
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("orders");
  const [newCategory, setNewCategory] = useState("");
  const [itemFormCategory, setItemFormCategory] = useState<string | null>(null);

  const menu = useQuery({
    queryKey: ["menu", rid],
    queryFn: () => getMenu(rid!),
    enabled: !!rid,
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["menu", rid] });

  const createCategory = useMutation({
    mutationFn: () => addCategory(rid!, newCategory, menu.data?.categories.length ?? 0),
    onSuccess: () => { setNewCategory(""); refresh(); },
  });
  const removeCategory = useMutation({
    mutationFn: (cid: string) => deleteCategory(rid!, cid),
    onSuccess: refresh,
  });
  const createItem = useMutation({
    mutationFn: (item: ItemPayload) => addItem(rid!, item),
    onSuccess: () => { setItemFormCategory(null); refresh(); },
  });
  const toggle86 = useMutation({
    mutationFn: (item: MenuItem) => patchItem(rid!, item.id, { available: !item.available }),
    onSuccess: refresh,
  });
  const removeItem = useMutation({
    mutationFn: (itemId: string) => deleteItem(rid!, itemId),
    onSuccess: refresh,
  });
  const setStatus = useMutation({
    mutationFn: (pause: boolean) => (pause ? pauseRestaurant(rid!) : resumeRestaurant(rid!)),
    onSuccess: refresh,
  });

  if (!claims) return <Navigate to="/login" replace state={{ from: location }} />;
  if (!rid) return <Navigate to="/partner" replace />;
  if (menu.isLoading) return <Spinner />;
  if (menu.error) return <ErrorNote error={menu.error} />;
  const data = menu.data!;
  const paused = data.status === "paused";

  const tabButton = (t: Tab, label: string) => (
    <button
      className={`rounded-lg px-3 py-1.5 text-sm ${tab === t ? "bg-slate-800 text-white" : "text-slate-400 hover:text-white"}`}
      onClick={() => setTab(t)}
    >
      {label}
    </button>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold">{data.name}</h1>
        <span className={`tag ${paused ? "bg-red-950 text-red-300" : "bg-emerald-950 text-emerald-300"}`}>
          {paused ? "paused" : "open"} · menu v{data.version}
        </span>
        <button className={paused ? "btn-primary" : "btn-danger"}
          disabled={setStatus.isPending}
          onClick={() => setStatus.mutate(!paused)}>
          {paused ? "Resume orders" : "Pause orders"}
        </button>
        <nav className="ml-auto flex gap-1 rounded-xl border border-slate-800 p-1">
          {tabButton("orders", "Orders")}
          {tabButton("menu", "Menu")}
          {tabButton("stock", "Stock")}
        </nav>
      </div>

      {tab === "orders" && <PartnerOrders />}
      {tab === "stock" && <PartnerStock rid={rid} />}
      {tab !== "menu" ? null : (<>
      <ErrorNote error={removeCategory.error ?? toggle86.error ?? removeItem.error} />

      {data.categories.map((cat) => (
        <section key={cat.id} className="space-y-2">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold">{cat.name}</h2>
            <button className="btn-ghost px-2 py-1 text-xs"
              onClick={() => setItemFormCategory(cat.id)}>
              + item
            </button>
            {cat.items.length === 0 && (
              <button className="btn-danger px-2 py-1 text-xs"
                onClick={() => removeCategory.mutate(cat.id)}>
                delete category
              </button>
            )}
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {cat.items.map((item) => (
              <div key={item.id} className={`card ${item.available ? "" : "opacity-60"}`}>
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="font-medium">{item.name} · <Money cents={item.price_cents} /></p>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {item.tags.map((t) => <span key={t} className="tag">{t}</span>)}
                      {item.modifier_groups.length > 0 && (
                        <span className="tag">{item.modifier_groups.length} modifier group(s)</span>
                      )}
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-1">
                    <button
                      className={`btn px-2 py-1 text-xs ${item.available ? "bg-slate-800 hover:bg-slate-700" : "bg-emerald-900 text-emerald-200"}`}
                      title="86 toggle — instantly hides from ordering"
                      onClick={() => toggle86.mutate(item)}>
                      {item.available ? "86 it" : "restock"}
                    </button>
                    <button className="btn-danger px-2 py-1 text-xs"
                      onClick={() => removeItem.mutate(item.id)}>
                      ✕
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>
      ))}

      <div className="card flex max-w-md items-center gap-2">
        <input className="input" placeholder="New category (Starters, Mains…)"
          value={newCategory} onChange={(e) => setNewCategory(e.target.value)} />
        <button className="btn-primary shrink-0" disabled={!newCategory || createCategory.isPending}
          onClick={() => createCategory.mutate()}>
          Add
        </button>
      </div>
      <ErrorNote error={createCategory.error} />

      {itemFormCategory && (
        <ItemForm categoryId={itemFormCategory}
          onSave={(item) => createItem.mutate(item)}
          onClose={() => setItemFormCategory(null)}
          error={createItem.error}
          busy={createItem.isPending} />
      )}
      </>)}
    </div>
  );
}
