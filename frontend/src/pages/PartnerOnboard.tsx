import { useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError, onboardRestaurant } from "../api/client";
import { useAuth } from "../state/auth";
import { ErrorNote } from "../components/ui";

const CUISINES = ["pakistani", "burgers", "italian", "japanese", "mexican", "indian",
                  "vietnamese", "middle-eastern", "chinese", "pizza", "bbq", "fast-food"];

/** Self-serve onboarding — the ADR-0020 client contract lives here:
 * 503 → persist the pending flag (App replays it on every launch);
 * success → tokens already refreshed by the client, claims carry the grant. */
export default function PartnerOnboard() {
  const navigate = useNavigate();
  const location = useLocation();
  const { claims, setPendingOnboarding } = useAuth();
  const [name, setName] = useState("");
  const [city, setCity] = useState("springfield");
  const [cuisines, setCuisines] = useState<string[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [pendingNote, setPendingNote] = useState(false);
  const [busy, setBusy] = useState(false);

  if (!claims) return <Navigate to="/login" replace state={{ from: location }} />;
  if (claims.role === "restaurant_admin") return <Navigate to="/partner/dashboard" replace />;

  const toggle = (c: string) =>
    setCuisines((prev) =>
      prev.includes(c) ? prev.filter((x) => x !== c)
      : prev.length >= 5 ? prev : [...prev, c],
    );

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onboardRestaurant({ name, city, cuisines });
      navigate("/partner/dashboard");
    } catch (err) {
      if (err instanceof ApiError && err.status === 503) {
        // Restaurant is committed server-side; the grant will converge.
        setPendingOnboarding({ name, city, cuisines });
        setPendingNote(true);
      } else {
        setError(err);
      }
    } finally {
      setBusy(false);
    }
  };

  if (pendingNote)
    return (
      <div className="mx-auto mt-10 max-w-md space-y-3 text-center">
        <h1 className="text-xl font-bold">Almost there</h1>
        <p className="text-slate-400">
          Your restaurant is saved. We hit a temporary hiccup finishing the setup —
          we'll complete it automatically on your next visit. Nothing is lost.
        </p>
        <Link to="/" className="btn-primary inline-block">Keep browsing</Link>
      </div>
    );

  return (
    <form onSubmit={submit} className="mx-auto mt-6 max-w-md space-y-4">
      <h1 className="text-xl font-bold">Become a partner</h1>
      <p className="text-sm text-slate-400">
        List your restaurant on SmartFood — this creates your brand and its
        first location. You'll manage the menu (and open more branches) right after.
      </p>
      <input className="input" placeholder="Restaurant name" value={name} required
        maxLength={120} onChange={(e) => setName(e.target.value)} />
      <select className="input" value={city} onChange={(e) => setCity(e.target.value)}>
        <option value="springfield">Springfield</option>
        <option value="shelbyville">Shelbyville</option>
      </select>
      <div>
        <p className="mb-2 text-sm text-slate-400">Cuisines (1–5)</p>
        <div className="flex flex-wrap gap-2">
          {CUISINES.map((c) => (
            <button type="button" key={c}
              className={`chip ${cuisines.includes(c) ? "chip-active" : ""}`}
              onClick={() => toggle(c)}>
              {c}
            </button>
          ))}
        </div>
      </div>
      <ErrorNote error={error} />
      <button className="btn-primary w-full" disabled={busy || !name || cuisines.length === 0}>
        {busy ? "Creating…" : "Create my restaurant"}
      </button>
    </form>
  );
}
