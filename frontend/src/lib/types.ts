import type { ActionEvent, BoardEntry, CallError, CallEvent, CallStarted, Capabilities, RouterDebug, SessionContext } from "./call-contract";
export type Locale = "ru" | "kk";
export type Fact = { key: string; value: string; source: string; source_id: string; origin: "required" | "background"; usedInTurn?: number };
export type Route = { scenario_id: string; confidence?: number; reason?: string };
export type Turn = {
  id: number; text: string; answer: string; language: string; channel: "text" | "voice";
  scenarios: Route[]; alternatives: Route[]; slots: Record<string, unknown>; facts: Fact[];
  timings: Record<string, number>; raw?: unknown; prompt?: string;
  status?: "processing" | "done" | "cancelled" | "error";
  errors?: CallError[]; actions?: ActionEvent[]; decision?: string; contextVersion?: number;
};
export type Conversation = {
  id: string; generation: number; startedAt: string; endedAt?: string;
  mode: "live" | "example"; turns: Turn[]; outcome?: "completed" | "handoff" | "interrupted" | "error";
  client?: { name: string; phone: string; city: string };
  claim?: { id: string; status: string; nextStep: string };
  facts: Fact[]; pendingTopics: string[];
  helper: "disabled" | "waiting" | "searching" | "ready" | "error" | "stale";
  confirmation?: { action: string; params: Record<string, unknown>; turnId: number };
  context?: SessionContext; board?: BoardEntry[];
};
// UI boundary only. This is not a proposed HTTP/Pydantic schema.
export interface VoiceAdapter {
  available: boolean;
  capabilities(signal: AbortSignal): Promise<Capabilities>;
  create(id: string, signal: AbortSignal): Promise<CallStarted>;
  stream(id: string, value: string | Blob, receive: (event: CallEvent) => void, signal: AbortSignal): Promise<void>;
  context(id: string, signal: AbortSignal): Promise<SessionContext>;
  board(id: string, signal: AbortSignal): Promise<BoardEntry[]>;
  debug(id: string, signal: AbortSignal): Promise<RouterDebug>;
  cancel(id: string, turnId: number): Promise<void>;
  playback(id: string, turnId: number, timing: { eos_to_playback_ms: number; eos_to_reply_text_ms?: number }): Promise<void>;
}
