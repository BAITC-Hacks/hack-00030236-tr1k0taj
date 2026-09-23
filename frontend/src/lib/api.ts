import type { VoiceAdapter } from "./types";
import type { CallEvent, RealtimeSttSession } from "./call-contract";
import { readSSE } from "./sse.ts";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message = `HTTP ${status}`) { super(message); this.status = status; }
}
function traceHeaders(parent?: string): Record<string, string> {
  return parent && /^00-(?!0{32}-)[a-f0-9]{32}-(?!0{16}-)[a-f0-9]{16}-[a-f0-9]{2}$/.test(parent) ? { traceparent: parent } : {};
}
async function request(path: string, init?: RequestInit) {
  const response = await fetch(`/api${path}`, { cache: "no-store", ...init });
  if (!response.ok) throw new ApiError(response.status);
  return response;
}
async function json<T>(path: string, signal: AbortSignal, body?: unknown): Promise<T> {
  signal = AbortSignal.any([signal, AbortSignal.timeout(15000)]);
  const response = await request(path, body === undefined ? { signal } : {
    signal, method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  return response.json();
}
export async function readHealth(signal: AbortSignal) {
  const value = await json<{ status: string }>("/health", signal);
  if (value.status !== "ok") throw new Error("Invalid health response");
  return value;
}
export const voiceAdapter: VoiceAdapter = {
  available: true,
  async capabilities(signal) {
    const caps = await json<import("./call-contract").Capabilities>("/capabilities", signal);
    if (!caps.providers || !Array.isArray(caps.audio_input)) throw new Error("Call API contract changed");
    return caps;
  },
  create: (id, signal) => json("/calls", signal, { session_id: id }),
  context: (id, signal) => json(`/sessions/${encodeURIComponent(id)}/context`, signal),
  board: (id, signal) => json(`/sessions/${encodeURIComponent(id)}/board`, signal),
  debug: (id, signal) => json(`/calls/${encodeURIComponent(id)}/router/last`, signal),
  async stream(id, value, receive, signal, onTrace, options) {
    const audio = typeof value !== "string";
    const form = new FormData();
    if (audio) form.set("audio", value, `utterance.${value.type.includes("ogg") ? "ogg" : value.type.includes("wav") ? "wav" : "webm"}`);
    const response = await request(`/calls/${encodeURIComponent(id)}/turns/${audio ? "audio" : "text"}`, {
      method: "POST", signal, headers: audio ? undefined : { "Content-Type": "application/json" },
      body: audio ? form : JSON.stringify({ text: value, ...(options?.source ? { source: options.source } : {}) }),
    });
    signal.throwIfAborted();
    const traceparent = traceHeaders(response.headers.get("traceparent") ?? undefined).traceparent;
    const headerId = response.headers.get("x-trace-id");
    const traceId = traceparent?.split("-")[1] ?? (headerId && /^[a-f0-9]{32}$/.test(headerId) && !/^0+$/.test(headerId) ? headerId : undefined);
    onTrace?.({ traceId, traceparent });
    if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) throw new Error("Expected SSE");
    let terminal = false;
    await readSSE(response.body, data => {
      if (!data || typeof data !== "object" || !("type" in data) || !("turn_id" in data) || typeof data.type !== "string" || typeof data.turn_id !== "number") throw new Error("Invalid SSE envelope");
      const event = data as CallEvent;
      terminal ||= event.type === "turn.done" || event.type === "turn.cancelled" || (event.type === "error" && event.fatal);
      receive(event);
    }, signal);
    if (!terminal) throw new Error("Incomplete response stream");
  },
  async sttSession(id, languageHint, signal) {
    const qs = languageHint ? `?language_hint=${languageHint}` : "";
    const response = await request(`/calls/${encodeURIComponent(id)}/stt/session${qs}`, { method: "POST", signal });
    return response.json() as Promise<RealtimeSttSession>;
  },
  async cancel(id, turnId, traceparent) { await request(`/calls/${encodeURIComponent(id)}/turns/${turnId}/cancel`, { method: "POST", headers: traceHeaders(traceparent) }); },
  async playback(id, turnId, timing, traceparent) {
    await request(`/calls/${encodeURIComponent(id)}/turns/${turnId}/playback`, {
      method: "POST", headers: { "Content-Type": "application/json", ...traceHeaders(traceparent) }, body: JSON.stringify(timing),
    });
  },
};
