import { useCallback, useEffect, useState } from "react";
import { Plus, Store, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";
import type { StoreRecord } from "../lib/types";
import { useToast } from "../components/Toast";

const input =
  "w-full rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-2.5 py-2 text-sm text-ink-900 dark:text-ink-dark-900 placeholder:text-ink-400 dark:placeholder:text-ink-dark-400 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20";
const label = "block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1";

export function StoresPage({
  connection,
  selectedStore,
  onActivateStore,
  onStoresChanged,
}: {
  connection: Connection;
  selectedStore: string | null;
  onActivateStore: (id: string | null) => void;
  onStoresChanged?: () => void;
}) {
  const [stores, setStores] = useState<StoreRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [shopDomain, setShopDomain] = useState("");
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const { toast } = useToast();

  const reload = useCallback(() => {
    return api
      .listStores(connection)
      .then((r) => setStores(r.stores))
      .catch(() => setError("Could not load stores."))
      .finally(() => setLoading(false));
  }, [connection]);

  useEffect(() => {
    setError("");
    setLoading(true);
    reload();
  }, [reload]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setSaving(true);
    setError("");
    try {
      await api.createStore(connection, {
        name: name.trim(),
        shop_domain: shopDomain.trim() || undefined,
        shopify_access_token: token.trim() || undefined,
      });
      setName("");
      setShopDomain("");
      setToken("");
      toast("success", "Store added. Switch to it from the sidebar to work inside it.");
      onStoresChanged?.();
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the store.");
    } finally {
      setSaving(false);
    }
  }

  async function remove(store: StoreRecord) {
    setError("");
    try {
      await api.deleteStore(connection, store.id);
      setConfirmingDeleteId(null);
      if (selectedStore === store.id) onActivateStore(null);
      toast("success", `${store.name} removed from the registry (its data file is kept).`);
      onStoresChanged?.();
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete the store.");
    }
  }

  return (
    <div className="max-w-4xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-ink-900 dark:text-ink-dark-900">Stores</h1>
        <p className="text-sm text-ink-600 dark:text-ink-dark-600 mt-1">
          Each store gets isolated tickets, knowledge and Shopify credentials. Pick the active
          store in the sidebar - every dashboard action then runs inside it.
        </p>
      </div>

      <form onSubmit={create} className="rounded-xl2 border border-line dark:border-line-dark bg-surface dark:bg-surface-dark p-5 space-y-4">
        <div className="flex items-center gap-2 text-sm font-medium text-ink-900 dark:text-ink-dark-900">
          <Plus className="w-4 h-4 text-teal" /> Add a store
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div>
            <label className={label}>Store name</label>
            <input
              className={input}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Acme"
              required
            />
          </div>
          <div>
            <label className={label}>Shop domain</label>
            <input
              className={input}
              value={shopDomain}
              onChange={(e) => setShopDomain(e.target.value)}
              placeholder="acme.myshopify.com"
            />
          </div>
          <div>
            <label className={label}>Shopify access token (optional)</label>
            <input
              className={input}
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="shpat_..."
              autoComplete="off"
            />
          </div>
        </div>
        {error && (
          <div className="text-xs rounded-lg bg-red-500/10 border border-red-500/30 text-red-600 dark:text-red-400 px-3 py-2">
            {error}
          </div>
        )}
        <button
          type="submit"
          disabled={saving || !name.trim()}
          className="text-xs rounded-lg bg-gold text-white font-medium px-4 py-2 hover:bg-gold-700 disabled:opacity-40 transition-colors"
        >
          {saving ? "Adding..." : "Add store"}
        </button>
      </form>

      <div className="rounded-xl2 border border-line dark:border-line-dark bg-surface dark:bg-surface-dark overflow-hidden">
        {loading ? (
          <div className="p-6 text-sm text-ink-600 dark:text-ink-dark-600">Loading stores...</div>
        ) : stores.length === 0 ? (
          <div className="p-6 text-sm text-ink-600 dark:text-ink-dark-600 flex items-center gap-2">
            <Store className="w-4 h-4" /> No stores yet - the deployment default is always active.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-ink-400 dark:text-ink-dark-400 border-b border-line dark:border-line-dark">
                <th className="px-4 py-3">Store</th>
                <th className="px-4 py-3">Shop</th>
                <th className="px-4 py-3">Shopify token</th>
                <th className="px-4 py-3">Added</th>
                <th className="px-4 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {stores.map((s) => {
                const active = selectedStore === s.id;
                return (
                  <tr key={s.id} className="border-b border-line dark:border-line-dark last:border-0">
                    <td className="px-4 py-3 font-medium text-ink-900 dark:text-ink-dark-900">
                      {s.name}
                      {active && <span className="ml-2 text-[11px] text-teal">(active)</span>}
                    </td>
                    <td className="px-4 py-3 text-ink-600 dark:text-ink-dark-600">
                      {s.shop_domain || <span className="text-ink-400">not connected</span>}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`text-[11px] px-2 py-0.5 rounded-full ${
                          s.has_shopify_token
                            ? "bg-teal/15 text-teal"
                            : "bg-ink-100 dark:bg-white/10 text-ink-600 dark:text-ink-dark-600"
                        }`}
                      >
                        {s.has_shopify_token ? "stored" : "none"}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-ink-600 dark:text-ink-dark-600">
                      {new Date(s.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-3 text-right space-x-2 whitespace-nowrap">
                      {!active && (
                        <button
                          onClick={() => onActivateStore(s.id)}
                          className="text-xs text-teal hover:underline"
                        >
                          Use store
                        </button>
                      )}
                      <button
                        onClick={() =>
                          confirmingDeleteId === s.id ? remove(s) : setConfirmingDeleteId(s.id)
                        }
                        onBlur={() => setConfirmingDeleteId((id) => (id === s.id ? null : id))}
                        className={`text-xs inline-flex items-center gap-1 ${
                          confirmingDeleteId === s.id
                            ? "text-red-600 dark:text-red-400 font-medium"
                            : "text-ink-400 hover:text-red-600 dark:hover:text-red-400"
                        }`}
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                        {confirmingDeleteId === s.id ? "Confirm delete" : "Delete"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
