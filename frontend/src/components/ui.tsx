import type { ReactNode } from "react";

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

/** The honest stub banner for Week-2 seams. */
export function ComingSoon({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/60 px-4 py-3 text-sm text-slate-400">
      <span className="mr-2 rounded bg-orange-500/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-orange-400">
        Week 2
      </span>
      {children}
    </div>
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
