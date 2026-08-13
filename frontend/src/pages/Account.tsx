import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import {
  addAddress, deleteAddress, getProfile, listAddresses, updateProfile,
} from "../api/client";
import { useAuth } from "../state/auth";
import { ErrorNote, Section, Spinner } from "../components/ui";

export default function Account() {
  const { claims } = useAuth();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [name, setName] = useState<string | null>(null);
  const [phone, setPhone] = useState<string | null>(null);
  const [addr, setAddr] = useState({ label: "", line1: "", city: "springfield" });

  const profile = useQuery({ queryKey: ["profile"], queryFn: getProfile, enabled: !!claims });
  const addresses = useQuery({ queryKey: ["addresses"], queryFn: listAddresses, enabled: !!claims });

  const saveProfile = useMutation({
    mutationFn: () =>
      updateProfile({
        ...(name !== null ? { full_name: name } : {}),
        ...(phone !== null ? { phone } : {}),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["profile"] }),
  });
  const createAddress = useMutation({
    mutationFn: () => addAddress(addr),
    onSuccess: () => {
      setAddr({ label: "", line1: "", city: "springfield" });
      queryClient.invalidateQueries({ queryKey: ["addresses"] });
    },
  });
  const removeAddress = useMutation({
    mutationFn: deleteAddress,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["addresses"] }),
  });

  if (!claims) return <Navigate to="/login" replace state={{ from: location }} />;
  if (profile.isLoading) return <Spinner />;

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      <Section title="Profile">
        <div className="card space-y-3">
          <p className="text-sm text-slate-400">{profile.data?.email} · {profile.data?.role}</p>
          <input className="input" placeholder="Full name"
            value={name ?? profile.data?.full_name ?? ""}
            onChange={(e) => setName(e.target.value)} />
          <input className="input" placeholder="Phone"
            value={phone ?? profile.data?.phone ?? ""}
            onChange={(e) => setPhone(e.target.value)} />
          <ErrorNote error={saveProfile.error} />
          <button className="btn-primary" disabled={saveProfile.isPending || (name === null && phone === null)}
            onClick={() => saveProfile.mutate()}>
            Save profile
          </button>
        </div>
      </Section>

      <Section title={`Addresses (${addresses.data?.length ?? 0}/20)`}>
        <div className="space-y-2">
          {addresses.data?.map((a) => (
            <div key={a.id} className="card flex items-center justify-between">
              <div>
                <p className="font-medium">{a.label}</p>
                <p className="text-xs text-slate-400">{a.line1}, {a.city}</p>
              </div>
              <button className="btn-danger px-3 py-1 text-xs"
                onClick={() => removeAddress.mutate(a.id)}>
                Remove
              </button>
            </div>
          ))}
        </div>
        <div className="card space-y-2">
          <div className="grid grid-cols-2 gap-2">
            <input className="input" placeholder="Label (home, work…)" value={addr.label}
              onChange={(e) => setAddr({ ...addr, label: e.target.value })} />
            <select className="input" value={addr.city}
              onChange={(e) => setAddr({ ...addr, city: e.target.value })}>
              <option value="springfield">Springfield</option>
              <option value="shelbyville">Shelbyville</option>
            </select>
          </div>
          <input className="input" placeholder="Street address" value={addr.line1}
            onChange={(e) => setAddr({ ...addr, line1: e.target.value })} />
          <ErrorNote error={createAddress.error} />
          <button className="btn-ghost" disabled={!addr.label || !addr.line1 || createAddress.isPending}
            onClick={() => createAddress.mutate()}>
            Add address
          </button>
        </div>
      </Section>
    </div>
  );
}
