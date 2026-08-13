import { useEffect, useRef } from "react";
import { Link, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { onboardRestaurant } from "./api/client";
import { useAuth } from "./state/auth";
import { useCart } from "./state/cart";
import Account from "./pages/Account";
import Browse from "./pages/Browse";
import Cart from "./pages/Cart";
import Checkout from "./pages/Checkout";
import Login from "./pages/Login";
import OrderDetail from "./pages/OrderDetail";
import Orders from "./pages/Orders";
import PartnerDashboard from "./pages/PartnerDashboard";
import PartnerOnboard from "./pages/PartnerOnboard";
import Register from "./pages/Register";
import RestaurantPage from "./pages/Restaurant";
import Search from "./pages/Search";

function Header() {
  const { claims, logout } = useAuth();
  const lines = useCart((c) => c.lines);
  const navigate = useNavigate();
  const location = useLocation();
  const count = lines.reduce((n, l) => n + l.qty, 0);
  const tab = ({ isActive }: { isActive: boolean }) =>
    `rounded-lg px-3 py-1.5 text-sm ${isActive ? "bg-slate-800 text-white" : "text-slate-400 hover:text-white"}`;

  return (
    <header className="sticky top-0 z-10 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
      <div className="mx-auto flex max-w-5xl items-center gap-2 px-4 py-3">
        <Link to="/" className="mr-3 text-lg font-bold">
          Smart<span className="text-orange-500">Food</span>
        </Link>
        <nav className="flex flex-1 items-center gap-1">
          <NavLink to="/" end className={tab}>Browse</NavLink>
          <NavLink to="/search" className={tab}>Search</NavLink>
          <NavLink to="/orders" className={tab}>Orders</NavLink>
          {claims?.role === "restaurant_admin" ? (
            <NavLink to="/partner/dashboard" className={tab}>My restaurant</NavLink>
          ) : (
            <NavLink to="/partner" className={tab}>Become a partner</NavLink>
          )}
        </nav>
        <Link to="/cart" className="btn-ghost relative">
          Cart
          {count > 0 && (
            <span className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-orange-500 text-[10px] font-bold text-slate-950">
              {count}
            </span>
          )}
        </Link>
        {claims ? (
          <>
            <Link to="/account" className="btn-ghost">Account</Link>
            <button className="btn-ghost" onClick={() => { logout(); navigate("/"); }}>
              Sign out
            </button>
          </>
        ) : (
          // Carry the current location — sign-in resumes where it interrupted.
          <Link to="/login" state={{ from: location }} className="btn-primary">Sign in</Link>
        )}
      </div>
    </header>
  );
}

/** ADR-0020 client contract: replay a half-finished onboarding on app load. */
function useOnboardingReplay() {
  const { claims, pendingOnboarding, setPendingOnboarding } = useAuth();
  const attempted = useRef(false);
  useEffect(() => {
    if (!claims || !pendingOnboarding || attempted.current) return;
    attempted.current = true;
    onboardRestaurant(pendingOnboarding)
      .then(() => setPendingOnboarding(null)) // grant landed; claims refreshed
      .catch(() => {}); // still down — keep the flag, try next launch
  }, [claims, pendingOnboarding, setPendingOnboarding]);
}

export default function App() {
  useOnboardingReplay();
  const pending = useAuth((s) => s.pendingOnboarding);
  return (
    <div className="min-h-screen">
      <Header />
      {pending && (
        <div className="border-b border-orange-900/50 bg-orange-950/40 px-4 py-2 text-center text-sm text-orange-200">
          Finishing your restaurant setup automatically — you can keep browsing.
        </div>
      )}
      <main className="mx-auto max-w-5xl px-4 py-6">
        <Routes>
          <Route path="/" element={<Browse />} />
          <Route path="/search" element={<Search />} />
          <Route path="/r/:id" element={<RestaurantPage />} />
          <Route path="/cart" element={<Cart />} />
          <Route path="/checkout" element={<Checkout />} />
          <Route path="/orders" element={<Orders />} />
          <Route path="/orders/:id" element={<OrderDetail />} />
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          <Route path="/account" element={<Account />} />
          <Route path="/partner" element={<PartnerOnboard />} />
          <Route path="/partner/dashboard" element={<PartnerDashboard />} />
        </Routes>
      </main>
    </div>
  );
}
