import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { getRestaurantAnalytics } from "../api/client";
import type { DayMetrics } from "../api/types";
import { ErrorNote, Money, Spinner } from "../components/ui";

/** S7 — the owner's business view over the analytics read model.
 *
 * Everything here is RENDERING: every number arrives computed from the
 * server (integer cents, rates already rounded) — the FE does layout math
 * only (bar heights), never money math. The chart is plain divs on
 * purpose: a bar is a rectangle, and a chart library is a dependency this
 * page doesn't need to earn its one visual. */

const WINDOWS = [7, 14, 30] as const;

function pct(rate: number | null): string {
  return rate === null ? "—" : `${(rate * 100).toFixed(1)}%`;
}

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="card">
      <p className="text-xs text-slate-400">{label}</p>
      <p className="mt-1 text-lg font-semibold">{children}</p>
    </div>
  );
}

function RevenueBars({ days }: { days: DayMetrics[] }) {
  const max = Math.max(...days.map((d) => d.revenue_cents), 1);
  return (
    <div className="card">
      <p className="mb-3 text-sm font-semibold">Revenue by day (settled orders)</p>
      <div className="flex h-36 items-end gap-1">
        {days.map((d) => (
          <div key={d.day} className="group flex h-full flex-1 flex-col items-center justify-end gap-1"
            title={`${d.day} — ${d.orders} orders`}>
            <div className="w-full rounded-t bg-orange-500/80 transition-colors group-hover:bg-orange-400"
              style={{ height: `${(d.revenue_cents / max) * 100}%`, minHeight: d.revenue_cents ? 2 : 0 }} />
            <span className="text-[10px] text-slate-500">{d.day.slice(5)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function OrderBars({ days }: { days: DayMetrics[] }) {
  const max = Math.max(...days.map((d) => d.orders), 1);
  return (
    <div className="card">
      <p className="mb-1 text-sm font-semibold">Orders vs cancelled by day</p>
      <p className="mb-3 text-xs text-slate-500">
        <span className="text-emerald-400">■</span> kept ·{" "}
        <span className="text-red-400">■</span> cancelled
      </p>
      <div className="flex h-36 items-end gap-1">
        {days.map((d) => {
          const kept = d.orders - d.cancelled;
          return (
            <div key={d.day} className="flex h-full flex-1 flex-col items-center justify-end gap-1"
              title={`${d.day} — ${kept} kept / ${d.cancelled} cancelled`}>
              {/* stacked: cancelled sits on top of kept, one truthful column */}
              <div className="flex w-full flex-col justify-end"
                style={{ height: `${(d.orders / max) * 100}%` }}>
                <div className="w-full rounded-t bg-red-900/80"
                  style={{ height: `${d.orders ? (d.cancelled / d.orders) * 100 : 0}%` }} />
                <div className="w-full bg-emerald-800/80"
                  style={{ height: `${d.orders ? (kept / d.orders) * 100 : 0}%` }} />
              </div>
              <span className="text-[10px] text-slate-500">{d.day.slice(5)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function PartnerInsights() {
  const [days, setDays] = useState<(typeof WINDOWS)[number]>(14);
  const query = useQuery({
    queryKey: ["insights", days],
    queryFn: () => getRestaurantAnalytics(days),
  });

  if (query.isLoading) return <Spinner />;
  if (query.error) return <ErrorNote error={query.error} />;
  const a = query.data!;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h2 className="text-lg font-semibold">Insights</h2>
        <div className="ml-auto flex gap-1 rounded-xl border border-slate-800 p-1">
          {WINDOWS.map((w) => (
            <button key={w}
              className={`rounded-lg px-3 py-1 text-sm ${days === w ? "bg-slate-800 text-white" : "text-slate-400 hover:text-white"}`}
              onClick={() => setDays(w)}>
              {w}d
            </button>
          ))}
        </div>
      </div>

      {/* Lifetime — the headline numbers, independent of the window picker */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Lifetime revenue (settled)">
          <Money cents={a.totals.revenue_cents} />
        </Stat>
        <Stat label="Lifetime orders">
          {a.totals.orders}
          <span className="ml-2 text-xs font-normal text-slate-400">
            {a.totals.settled} settled · {a.totals.cancelled} cancelled
          </span>
        </Stat>
        <Stat label="Average order value">
          {a.totals.aov_cents === null ? "—" : <Money cents={a.totals.aov_cents} />}
        </Stat>
        <Stat label="Repeat customers">
          {pct(a.totals.repeat_rate)}
          <span className="ml-2 text-xs font-normal text-slate-400">
            {a.totals.repeat_customers} of {a.totals.customers}
          </span>
        </Stat>
      </div>

      {/* The window — what the picker actually controls */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label={`Orders (${days}d)`}>{a.window.orders}</Stat>
        <Stat label={`Settled (${days}d)`}>{a.window.settled}</Stat>
        <Stat label="Acceptance rate">{pct(a.acceptance_rate)}</Stat>
        <Stat label="Cancellation rate">{pct(a.cancellation_rate)}</Stat>
      </div>

      {/* The funnel (S8): browse → order. Conversion is measured over
          signed-in viewers — anonymous browsers count toward views only. */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label={`Menu views (${days}d)`}>{a.funnel.views}</Stat>
        <Stat label="Signed-in viewers">{a.funnel.viewers}</Stat>
        <Stat label="Ordered within 24h">{a.funnel.converted_viewers}</Stat>
        <Stat label="Conversion rate">{pct(a.funnel.conversion_rate)}</Stat>
      </div>

      {a.days.length === 0 ? (
        <p className="card text-sm text-slate-400">
          No orders in this window yet — the charts appear with the first one.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <RevenueBars days={a.days} />
          <OrderBars days={a.days} />
        </div>
      )}
    </div>
  );
}
