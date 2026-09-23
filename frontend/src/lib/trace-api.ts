import type { SessionTrace, SessionTraceSummary, TraceView } from "./trace-contract";

export class TraceApiError extends Error {
  status: number;
  constructor(status: number) { super(`Trace API: HTTP ${status}`); this.status = status; }
}
async function get<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, { cache: "no-store", signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]) });
  if (!response.ok) throw new TraceApiError(response.status);
  return response.json() as Promise<T>;
}
export const traceApi = {
  sessions: (limit: number, offset: number, signal: AbortSignal) => get<SessionTraceSummary[]>(`/traces/sessions?limit=${limit}&offset=${offset}`, signal),
  session: (id: string, signal: AbortSignal) => get<SessionTrace>(`/traces/sessions/${encodeURIComponent(id)}`, signal),
  trace: (id: string, signal: AbortSignal) => get<TraceView>(`/traces/${encodeURIComponent(id)}`, signal),
};
