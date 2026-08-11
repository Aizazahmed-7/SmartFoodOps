import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { login, register } from "../api/client";
import { ErrorNote } from "../components/ui";

export default function Register() {
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
      await register(email, password); // idempotent 202 either way (enumeration-safe)
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
      <h1 className="text-xl font-bold">Create account</h1>
      <input className="input" type="email" placeholder="email" value={email}
        onChange={(e) => setEmail(e.target.value)} required />
      <input className="input" type="password" placeholder="password (10+ characters)"
        minLength={10} value={password}
        onChange={(e) => setPassword(e.target.value)} required />
      <ErrorNote error={error} />
      <button className="btn-primary w-full" disabled={busy}>
        {busy ? "Creating…" : "Create account"}
      </button>
    </form>
  );
}
