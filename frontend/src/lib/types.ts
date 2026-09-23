export type Locale = "ru" | "kk";
export type Fact = { key: string; value: string; source: string; source_id: string; origin: "required" | "background"; usedInTurn?: number };
export type Route = { scenario_id: string; confidence?: number; reason?: string };
export type Turn = {
  id: number; text: string; answer: string; language: string; channel: "text" | "voice";
  scenarios: Route[]; alternatives: Route[]; slots: Record<string, unknown>; facts: Fact[];
  timings: Record<string, number>; raw?: unknown; prompt?: string;
};
export type Conversation = {
  id: string; generation: number; startedAt: string; endedAt?: string;
  mode: "live" | "example"; turns: Turn[]; outcome?: "completed" | "handoff" | "interrupted" | "error";
  client?: { name: string; phone: string; city: string };
  claim?: { id: string; status: string; nextStep: string };
  facts: Fact[]; pendingTopics: string[];
  helper: "disabled" | "waiting" | "searching" | "ready" | "error" | "stale";
  confirmation?: { action: string; params: Record<string, unknown>; turnId: number };
};
// UI boundary only. This is not a proposed HTTP/Pydantic schema.
export interface VoiceAdapter {
  available: boolean;
  create(signal: AbortSignal): Promise<Conversation>;
  text(session: Conversation, text: string, signal: AbortSignal): Promise<Conversation>;
  audio(session: Conversation, audio: Blob, signal: AbortSignal): Promise<Conversation>;
  cancel(session: Conversation): Promise<void>;
  end(session: Conversation): Promise<void>;
}
