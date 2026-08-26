/**
 * The rider console — the game half of the demo. You ARE the courier:
 * arrow keys / WASD (or click-to-glide) move your dot through the toy
 * city, and every real subsystem reacts — your position feeds Redis GEO
 * at 1 Hz over the gateway WebSocket, offers pop with a live countdown,
 * and the pickup/deliver buttons arm only inside the 40 m tap radius
 * (the same ARRIVE_M the rider-sim uses).
 *
 * Two transports, on purpose (the design's own claim, demonstrated):
 *  - REST is the FLOOR: /v1/rider/me polls every 2s — kill the socket
 *    and the console keeps working, offers included.
 *  - The WS is the ACCELERATOR: GPS up at 1 Hz, offers down instantly.
 * The render loop runs at ~30 fps; the WIRE stays at 1 Hz — screen rate
 * and wire rate are independent, exactly like a real rider app.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  acceptRiderOffer,
  browse,
  getRiderMe,
  setRiderStatus,
  tapDelivery,
} from "../api/client";
import type { RiderMe } from "../api/types";
import CityMap, { CITY, Pin, clampToCity, project } from "../components/CityMap";
import { ErrorNote } from "../components/ui";
import { useAuth } from "../state/auth";

const SPEED_MPS = 45; // arcade-brisk — the demo shouldn't feel like traffic
const ARRIVE_M = 40; // mirrors rider_sim.main.ARRIVE_M and the sim's taps
const M_PER_DEG_LAT = 111_320;
const START = { lat: 39.8005, lon: -89.652 }; // mid-city kickoff

function metersBetween(a: { lat: number; lon: number }, b: { lat: number; lon: number }) {
  const dLat = (b.lat - a.lat) * M_PER_DEG_LAT;
  const dLon = (b.lon - a.lon) * M_PER_DEG_LAT * Math.cos((a.lat * Math.PI) / 180);
  return Math.hypot(dLat, dLon);
}

const KEY_VECTORS: Record<string, [number, number]> = {
  ArrowUp: [1, 0], w: [1, 0],
  ArrowDown: [-1, 0], s: [-1, 0],
  ArrowLeft: [0, -1], a: [0, -1],
  ArrowRight: [0, 1], d: [0, 1],
};

export default function RiderConsole() {
  const { claims, access } = useAuth();
  const queryClient = useQueryClient();
  const [online, setOnline] = useState(false);
  const [position, setPosition] = useState(START);
  const [socketLive, setSocketLive] = useState(false);
  const [countdown, setCountdown] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);

  const positionRef = useRef(position);
  positionRef.current = position;
  const heldKeys = useRef(new Set<string>());
  const clickTarget = useRef<{ lat: number; lon: number } | null>(null);
  const socketRef = useRef<WebSocket | null>(null);

  const isRider = claims?.role === "rider";

  // The poll floor: /me every 2s while online (background too — an offer
  // must ring even when the tab is hidden behind the customer window).
  const me = useQuery<RiderMe>({
    queryKey: ["rider-me"],
    queryFn: getRiderMe,
    enabled: isRider && online,
    refetchInterval: 2000,
    refetchIntervalInBackground: true,
  });
  const restaurants = useQuery({
    queryKey: ["rider-city"],
    queryFn: () => browse("springfield", {}),
    enabled: isRider,
    staleTime: 300_000,
  });

  const refetchMe = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ["rider-me"] }),
    [queryClient],
  );

  // ── movement: a 80ms TIMER integrates keys/click-glide (not rAF —
  // browsers freeze rAF in hidden/unfocused tabs, and a demo often runs
  // beside a customer window; hidden tabs clamp intervals to ~1s, so the
  // dt clamp of 1.2s keeps the glide at FULL SPEED either way). The WIRE
  // stays 1 Hz regardless — render rate and wire rate are independent.
  useEffect(() => {
    if (!online) return;
    let last = performance.now();
    const step = () => {
      const now = performance.now();
      const dt = Math.min(1.2, (now - last) / 1000);
      last = now;
      let { lat, lon } = positionRef.current;
      let [dLat, dLon] = [0, 0];
      for (const key of heldKeys.current) {
        const vec = KEY_VECTORS[key];
        if (vec) {
          dLat += vec[0];
          dLon += vec[1];
        }
      }
      if (dLat !== 0 || dLon !== 0) {
        clickTarget.current = null; // keys override the click glide
        const norm = Math.hypot(dLat, dLon);
        lat += ((dLat / norm) * SPEED_MPS * dt) / M_PER_DEG_LAT;
        lon +=
          ((dLon / norm) * SPEED_MPS * dt) /
          (M_PER_DEG_LAT * Math.cos((lat * Math.PI) / 180));
      } else if (clickTarget.current) {
        const target = clickTarget.current;
        const distance = metersBetween({ lat, lon }, target);
        const reach = SPEED_MPS * dt;
        if (distance <= reach) {
          ({ lat, lon } = target);
          clickTarget.current = null;
        } else {
          // Glide a FRACTION of the remaining way — degrees stay degrees,
          // so no unit conversion is needed at all.
          const fraction = reach / distance;
          lat += (target.lat - lat) * fraction;
          lon += (target.lon - lon) * fraction;
        }
      }
      const clamped = clampToCity(lat, lon);
      if (clamped.lat !== positionRef.current.lat || clamped.lon !== positionRef.current.lon) {
        setPosition(clamped);
      }
    };
    const ticker = setInterval(step, 80);
    const down = (e: KeyboardEvent) => {
      if (KEY_VECTORS[e.key]) {
        heldKeys.current.add(e.key);
        e.preventDefault();
      }
    };
    const up = (e: KeyboardEvent) => heldKeys.current.delete(e.key);
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      clearInterval(ticker);
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, [online]);

  // ── the socket: GPS up at 1 Hz, offers down; best-effort forever ──
  useEffect(() => {
    if (!online || !access) return;
    let closed = false;
    let socket: WebSocket | null = null;
    let pinger: ReturnType<typeof setInterval> | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const connect = () => {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${location.host}/ws/rider`, ["bearer", access]);
      socketRef.current = socket;
      socket.onopen = () => {
        setSocketLive(true);
        pinger = setInterval(() => {
          const p = positionRef.current;
          socket?.send(JSON.stringify({ type: "ping", lat: p.lat, lon: p.lon }));
        }, 1000);
      };
      socket.onmessage = () => refetchMe(); // offer/revoke frames → look again
      socket.onclose = () => {
        setSocketLive(false);
        if (pinger) clearInterval(pinger);
        if (!closed) retry = setTimeout(connect, 3000); // REST floor holds meanwhile
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => {
      closed = true;
      if (pinger) clearInterval(pinger);
      if (retry) clearTimeout(retry);
      socket?.close();
      socketRef.current = null;
    };
  }, [online, access, refetchMe]);

  // Offer countdown — cosmetic urgency; dispatch's timer is the truth.
  const offer = me.data?.offer ?? null;
  useEffect(() => {
    if (!offer) {
      setCountdown(null);
      return;
    }
    setCountdown(15);
    const tick = setInterval(() => setCountdown((c) => (c === null || c <= 0 ? c : c - 1)), 1000);
    return () => clearInterval(tick);
  }, [offer?.offer_id]);

  if (!isRider) {
    return (
      <div className="mx-auto mt-10 max-w-md space-y-3 text-center">
        <h1 className="text-xl font-bold">Rider console</h1>
        <p className="text-slate-400">
          Sign in with a rider account to take deliveries — the seeded demo riders are{" "}
          <code className="text-orange-400">rider1@demo.smartfood.dev</code> (…2, …3), password{" "}
          <code className="text-orange-400">demo1234demo</code>.
        </p>
      </div>
    );
  }

  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      await refetchMe();
    } catch (err) {
      setError(err);
      await refetchMe(); // a 409 means the truth moved — show it
    }
  };

  const toggleOnline = () =>
    act(async () => {
      if (online) {
        await setRiderStatus(false);
        setOnline(false);
      } else {
        await setRiderStatus(true, positionRef.current);
        setOnline(true);
      }
    });

  const delivery = me.data?.delivery ?? null;
  const target =
    delivery?.state === "ASSIGNED"
      ? { ...delivery.pickup, tap: "pickup" as const, label: "Pick up" }
      : delivery?.state === "PICKED_UP"
        ? { ...delivery.dropoff, tap: "deliver" as const, label: "Deliver" }
        : null;
  const targetDistance =
    target && target.lat != null && target.lon != null
      ? metersBetween(position, { lat: target.lat, lon: target.lon })
      : null;
  const canTap = targetDistance !== null && targetDistance <= ARRIVE_M;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-bold">Rider console</h1>
        <span
          className={`rounded-full px-2 py-0.5 text-xs ${online ? "bg-emerald-900 text-emerald-300" : "bg-slate-800 text-slate-400"}`}
        >
          {online ? "online" : "offline"}
        </span>
        <span className="text-xs text-slate-500">
          {socketLive ? "live GPS stream" : online ? "poll mode (socket down)" : ""}
        </span>
        <div className="flex-1" />
        <button className="btn-primary" onClick={toggleOnline}>
          {online ? "Go offline" : "Go online"}
        </button>
      </div>
      <p className="text-sm text-slate-400">
        Drive with <b>WASD / arrow keys</b> or click the map to glide. Get within {ARRIVE_M} m to
        tap pickup / deliver.
      </p>
      {error != null && <ErrorNote error={error} />}

      {offer && (
        <div className="flex items-center gap-3 rounded-xl border border-orange-700 bg-orange-950/40 p-3">
          <div className="flex-1">
            <div className="font-semibold">
              New offer — {offer.restaurant_name || "a restaurant"}
            </div>
            <div className="text-xs text-slate-400">
              order {offer.order_id.slice(0, 12)}… · pickup highlighted on the map
            </div>
          </div>
          {countdown !== null && (
            <span className="text-2xl font-bold tabular-nums text-orange-400">{countdown}</span>
          )}
          <button
            className="btn-primary"
            onClick={() => act(() => acceptRiderOffer(offer.offer_id, offer.order_id))}
          >
            Accept
          </button>
        </div>
      )}

      {delivery && (
        <div className="flex items-center gap-3 rounded-xl border border-slate-700 bg-slate-900 p-3">
          <div className="flex-1 text-sm">
            <b>{delivery.state === "ASSIGNED" ? "Head to the restaurant" : "Head to the customer"}</b>
            <span className="ml-2 text-slate-400">
              {targetDistance !== null ? `${Math.round(targetDistance)} m away` : ""}
            </span>
          </div>
          {target && (
            <button
              className={canTap ? "btn-primary" : "btn-ghost cursor-not-allowed opacity-50"}
              disabled={!canTap}
              onClick={() => act(() => tapDelivery(delivery.order_id, target.tap))}
            >
              {target.label}
            </button>
          )}
        </div>
      )}

      <CityMap
        onMapClick={(lat, lon) => {
          clickTarget.current = { lat, lon };
        }}
      >
        {(restaurants.data?.restaurants ?? []).map(
          (r) =>
            r.lat != null &&
            r.lon != null && (
              <Pin
                key={r.id}
                lat={r.lat}
                lon={r.lon}
                glyph="🍛"
                label={r.name}
                highlight={
                  delivery?.state === "ASSIGNED" && delivery.pickup.lat === r.lat
                }
              />
            ),
        )}
        {delivery?.dropoff.lat != null && delivery.dropoff.lon != null && (
          <Pin
            lat={delivery.dropoff.lat}
            lon={delivery.dropoff.lon}
            glyph="🏠"
            label="drop-off"
            highlight={delivery.state === "PICKED_UP"}
          />
        )}
        {offer?.pickup.lat != null && offer.pickup.lon != null && (
          <Pin lat={offer.pickup.lat} lon={offer.pickup.lon} glyph="📦" highlight />
        )}
        {/* you — drawn last, always on top */}
        <RiderDot lat={position.lat} lon={position.lon} carrying={delivery?.state === "PICKED_UP"} />
      </CityMap>
      <p className="text-xs text-slate-600">
        The box is a real {(0.04 * 111.32).toFixed(1)} km of latitude ({CITY.south}–{CITY.north}) —
        Redis GEOSEARCH, the 3 km offer radius and your dot all share these coordinates.
      </p>
    </div>
  );
}

function RiderDot({ lat, lon, carrying }: { lat: number; lon: number; carrying: boolean }) {
  const p = project(lat, lon);
  return (
    <g>
      <circle cx={p.x} cy={p.y} r={10} fill="#f97316" stroke="#0f172a" strokeWidth={3} />
      <text x={p.x} y={p.y - 14} textAnchor="middle" fontSize={16}>
        {carrying ? "🛵📦" : "🛵"}
      </text>
    </g>
  );
}
