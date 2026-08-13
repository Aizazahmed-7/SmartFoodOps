import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import { listNotifications, markAllNotificationsRead, markNotificationRead } from "../api/client";
import type { NotificationRow } from "../api/types";
import { useAuth } from "../state/auth";

export default function NotificationBell() {
  const { claims } = useAuth();
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();

  const notifications = useQuery({
    queryKey: ["notifications"],
    queryFn: () => listNotifications(),
    refetchInterval: 15000,
    refetchIntervalInBackground: true, // the badge keeps counting in a background tab
    enabled: !!claims,
  });

  const markRead = useMutation({
    mutationFn: (id: string) => markNotificationRead(id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });
  const markAll = useMutation({
    mutationFn: () => markAllNotificationsRead(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  // Navigating anywhere means the panel's job is done.
  useEffect(() => setOpen(false), [location]);

  // A failed poll must never break the nav: no data → bare button, no badge.
  const unread = notifications.data?.unread ?? 0;
  const items = notifications.data?.items.slice(0, 8) ?? [];

  const openItem = (n: NotificationRow) => {
    if (!n.read_at) markRead.mutate(n.id);
    navigate(claims?.role === "restaurant_admin" ? "/partner/dashboard" : `/orders/${n.order_id}`);
    setOpen(false);
  };

  return (
    <div className="relative">
      <button className="btn-ghost relative" onClick={() => setOpen((o) => !o)}>
        Alerts
        {unread > 0 && (
          <span className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-orange-500 text-[10px] font-bold text-slate-950">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>
      {open && (
        <div className="absolute right-0 top-full z-20 mt-2 w-80 rounded-2xl border border-slate-800 bg-slate-900 p-2 shadow-xl">
          <div className="flex items-center justify-between px-2 py-1">
            <p className="text-sm font-semibold text-slate-200">Notifications</p>
            {unread > 0 && (
              <button
                className="text-xs font-medium text-orange-400 hover:text-orange-300"
                onClick={() => markAll.mutate()}
              >
                Mark all read
              </button>
            )}
          </div>
          {items.length === 0 && (
            <p className="px-2 py-6 text-center text-sm text-slate-500">Nothing yet.</p>
          )}
          {items.map((n) => (
            <button
              key={n.id}
              className="block w-full rounded-xl px-2 py-2 text-left transition-colors hover:bg-slate-800"
              onClick={() => openItem(n)}
            >
              <p className="flex items-center gap-2 text-sm font-medium">
                {!n.read_at && <span className="h-2 w-2 shrink-0 rounded-full bg-orange-500" />}
                {n.title}
              </p>
              <p className="text-xs text-slate-400">{n.body}</p>
              <p className="mt-0.5 text-xs text-slate-500">
                {new Date(n.created_at).toLocaleString()}
              </p>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
