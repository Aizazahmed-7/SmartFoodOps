/**
 * The toy city, drawn by us — no tiles, no map provider (the user's call:
 * "a 2d map created by us, just like a game").
 *
 * Coordinates are REAL lat/lon inside the toy-city box (Rawalpindi in the
 * FE's city list), so the
 * same numbers feed Redis GEOSEARCH, the 3 km offer radius, and this SVG.
 * `project()` is the only bridge: degrees → viewBox units, with latitude
 * flipped (north is up, SVG y grows down) and longitude scaled by
 * cos(latitude) so a meter looks the same in both axes.
 */
import type { ReactNode } from "react";

// MUST mirror tools/seed/seed/main.py CITY_BOXES first box — the FE shows
// it as Rawalpindi (src/cities.ts); coordinates are the contract, the city
// NAME is display-level only.
export const CITY = { south: 39.78, west: -89.67, north: 39.82, east: -89.63 };

const LAT_SPAN = CITY.north - CITY.south;
const LON_SPAN = CITY.east - CITY.west;
const ASPECT = LAT_SPAN / (LON_SPAN * Math.cos((39.8 * Math.PI) / 180)); // ≈1.30
export const MAP_W = 720;
export const MAP_H = Math.round(MAP_W * ASPECT); // ≈938 — meters look square

export function project(lat: number, lon: number): { x: number; y: number } {
  return {
    x: ((lon - CITY.west) / LON_SPAN) * MAP_W,
    y: ((CITY.north - lat) / LAT_SPAN) * MAP_H,
  };
}

export function unproject(x: number, y: number): { lat: number; lon: number } {
  return {
    lat: CITY.north - (y / MAP_H) * LAT_SPAN,
    lon: CITY.west + (x / MAP_W) * LON_SPAN,
  };
}

export const clampToCity = (lat: number, lon: number) => ({
  lat: Math.min(CITY.north, Math.max(CITY.south, lat)),
  lon: Math.min(CITY.east, Math.max(CITY.west, lon)),
});

/** One street every ~0.004° — a legible grid, not a survey. */
function Streets() {
  const lines = [];
  for (let i = 1; i < 10; i++) {
    const y = (i / 10) * MAP_H;
    lines.push(<line key={`h${i}`} x1={0} y1={y} x2={MAP_W} y2={y} />);
  }
  for (let i = 1; i < 10; i++) {
    const x = (i / 10) * MAP_W;
    lines.push(<line key={`v${i}`} x1={x} y1={0} x2={x} y2={MAP_H} />);
  }
  return <g stroke="#1e293b" strokeWidth={3}>{lines}</g>;
}

export function Pin({
  lat,
  lon,
  label,
  glyph,
  highlight = false,
}: {
  lat: number;
  lon: number;
  label?: string;
  glyph: string;
  highlight?: boolean;
}) {
  const p = project(lat, lon);
  return (
    <g>
      {highlight && (
        <circle cx={p.x} cy={p.y} r={26} fill="none" stroke="#f97316" strokeWidth={3}>
          <animate attributeName="r" values="18;30;18" dur="1.6s" repeatCount="indefinite" />
        </circle>
      )}
      <text x={p.x} y={p.y + 7} textAnchor="middle" fontSize={22}>
        {glyph}
      </text>
      {label && (
        <text x={p.x} y={p.y + 26} textAnchor="middle" fontSize={11} fill="#94a3b8">
          {label}
        </text>
      )}
    </g>
  );
}

export default function CityMap({
  children,
  onMapClick,
  className = "",
}: {
  children: ReactNode;
  onMapClick?: (lat: number, lon: number) => void;
  className?: string;
}) {
  return (
    <svg
      viewBox={`0 0 ${MAP_W} ${MAP_H}`}
      className={`w-full rounded-xl border border-slate-800 bg-slate-900 ${className}`}
      onClick={
        onMapClick
          ? (event) => {
              const svg = event.currentTarget;
              const rect = svg.getBoundingClientRect();
              const x = ((event.clientX - rect.left) / rect.width) * MAP_W;
              const y = ((event.clientY - rect.top) / rect.height) * MAP_H;
              const { lat, lon } = unproject(x, y);
              onMapClick(lat, lon);
            }
          : undefined
      }
    >
      <Streets />
      {children}
    </svg>
  );
}
