import { ComingSoon } from "../components/ui";

const PLACEHOLDER = [
  { name: "Biryani House", status: "DELIVERED", total: "$18.00", when: "yesterday" },
  { name: "Taco Town", status: "SETTLED", total: "$9.50", when: "last week" },
];

export default function Orders() {
  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <h1 className="text-xl font-bold">Your orders</h1>
      <ComingSoon>
        Order history reads the order service (Week 2). Below is the intended
        layout with placeholder data.
      </ComingSoon>
      <div className="space-y-2 opacity-50 select-none" aria-hidden>
        {PLACEHOLDER.map((o, i) => (
          <div key={i} className="card flex items-center justify-between">
            <div>
              <p className="font-medium">{o.name}</p>
              <p className="text-xs text-slate-500">{o.when}</p>
            </div>
            <div className="text-right">
              <span className="tag">{o.status}</span>
              <p className="mt-1 text-sm font-semibold">{o.total}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
