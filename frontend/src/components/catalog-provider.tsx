"use client";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { loadCatalog } from "@/lib/catalog-api";
import type { CatalogData } from "@/lib/catalog";

type CatalogState = CatalogData & { status: "loading" | "ready" | "error"; refreshing: boolean };
const empty: CatalogData = { scenarios: [], systemIntents: [] };
function useCatalogState() {
  const [state, setState] = useState<CatalogState>({ ...empty, status: "loading", refreshing: true });
  const active = useRef<AbortController | null>(null);
  const fetchCatalog = useCallback(async () => {
    active.current?.abort();
    const controller = new AbortController(); active.current = controller;
    try {
      const data = await loadCatalog(controller.signal);
      if (!controller.signal.aborted) setState({ ...data, status: "ready", refreshing: false });
    } catch {
      if (!controller.signal.aborted) setState({ ...empty, status: "error", refreshing: false });
    } finally { if (active.current === controller) active.current = null; }
  }, []);
  const refresh = useCallback(() => {
    setState(current => ({ ...current, refreshing: true }));
    return fetchCatalog();
  }, [fetchCatalog]);
  useEffect(() => {
    let disposed = false;
    queueMicrotask(() => { if (!disposed) void fetchCatalog(); });
    const onFocus = () => { if (!active.current) void refresh(); };
    window.addEventListener("focus", onFocus);
    return () => { disposed = true; active.current?.abort(); window.removeEventListener("focus", onFocus); };
  }, [fetchCatalog, refresh]);
  const scenarioTitle = (id: string) => state.scenarios.find(s => s.scenario_id === id)?.name ?? state.systemIntents.find(s => s.id === id)?.description ?? id;
  return { ...state, refresh, scenarioTitle };
}
const CatalogContext = createContext<ReturnType<typeof useCatalogState> | null>(null);
export function CatalogProvider({ children }: { children: ReactNode }) { return <CatalogContext.Provider value={useCatalogState()}>{children}</CatalogContext.Provider>; }
export function useCatalog() { const state = useContext(CatalogContext); if (!state) throw new Error("CatalogProvider missing"); return state; }
