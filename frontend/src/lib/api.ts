import type { VoiceAdapter } from "./types";

export async function readHealth(signal: AbortSignal) {
  const response = await fetch("/api/health", { signal, cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const value: unknown = await response.json();
  if (!value || typeof value !== "object" || !("status" in value) || value.status !== "ok") throw new Error("Invalid health response");
  return value;
}

// TODO(hack): Connect the agreed voice API when published by the backend owner.
// No invented endpoints, browser LLM keys or utterance-to-intent fallbacks.
async function unavailable(): Promise<never> { throw new Error("VOICE_API_NOT_CONNECTED"); }
export const voiceAdapter: VoiceAdapter = {
  available: false, create: unavailable, text: unavailable,
  audio: unavailable, cancel: unavailable, end: unavailable,
};
