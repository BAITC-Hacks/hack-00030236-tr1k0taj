import { normalizeCatalog, type CatalogData } from "./catalog.ts";

export async function loadCatalog(signal: AbortSignal): Promise<CatalogData> {
  const requestSignal = AbortSignal.any([signal, AbortSignal.timeout(15000)]);
  const [scenarios, systemIntents] = await Promise.all([
    "/api/kit/scenario",
    "/api/kit/system_intent",
  ].map(async path => {
    const response = await fetch(path, { cache: "no-store", signal: requestSignal });
    if (!response.ok) throw new Error(`Catalog request failed: ${path} (HTTP ${response.status})`);
    return response.json() as Promise<unknown>;
  }));
  requestSignal.throwIfAborted();
  return normalizeCatalog(scenarios, systemIntents);
}
