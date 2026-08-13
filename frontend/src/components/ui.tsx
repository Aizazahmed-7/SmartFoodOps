import type { ReactNode } from "react";
import type { OrderStatus } from "../api/types";

const STATUS_STYLE: Partial<Record<OrderStatus, string>> = {
  SETTLED: "bg-emerald-950 text-emerald-300",
  DELIVERED: "bg-emerald-950 text-emerald-300",
  CANCELLING: "bg-red-950 text-red-300",
  CANCELLED: "bg-red-950 text-red-300",
  REFUNDED: "bg-red-950 text-red-300",
};

export function StatusTag({ status }: { status: OrderStatus }) {
  return <span className={`tag ${STATUS_STYLE[status] ?? ""}`}>{status.replace("_", " ")}</span>;
}

export function Money({ cents }: { cents: number }) {
  const sign = cents < 0 ? "-" : "";
  return (
    <span className="tabular-nums">
      {sign}${(Math.abs(cents) / 100).toFixed(2)}
    </span>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  return (
    <p className="rounded-xl bg-red-950/60 border border-red-900 px-3 py-2 text-sm text-red-200">
      {message}
    </p>
  );
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-slate-200">{title}</h2>
      {children}
    </section>
  );
}

export function Spinner() {
  return (
    <div className="flex justify-center py-10">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-slate-700 border-t-orange-500" />
    </div>
  );
}
