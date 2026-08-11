import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { login } from "../api/client";
import { ErrorNote } from "../components/ui";

export default function Login() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      navigate("/");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="mx-auto mt-10 max-w-sm space-y-3">
      <h1 className="text-xl font-bold">Sign in</h1>
      <input className="input" type="email" placeholder="email" value={email}
        onChange={(e) => setEmail(e.target.value)} required />
      <input className="input" type="password" placeholder="password" value={password}
        onChange={(e) => setPassword(e.target.value)} required />
      <ErrorNote error={error} />
      <button className="btn-primary w-full" disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </button>
      <p className="text-center text-sm text-slate-400">
        New here? <Link to="/register" className="text-orange-400">Create an account</Link>
      </p>
      <p className="text-center text-xs text-slate-600">
        Seeded demo: owner-springfield-biryani-house@demo.smartfood.dev / demo1234demo
      </p>
    </form>
  );
}
