// HTTP types owned by backend modules call/context (docs/specs/call-api.md).
import type { Route } from "./types";

export type Capabilities = {
  mock_mode: boolean; providers: Record<string, string>; supported_actions: string[];
  languages: string[]; audio_input: string[]; dataset_today: string;
};
export type ContextFact = {
  key: string; value: unknown; source: string; source_id: string | null;
  origin: "foreground" | "background"; context_version: number;
};
export type SessionContext = {
  session_id: string; generation: number; context_version: number; turn_id: number;
  client_id: string | null; language: string | null; active_scenario: string | null;
  pending_topics: string[]; slots_by_topic: Record<string, Record<string, unknown>>;
  pending_confirmation: { action: string; params: Record<string, unknown>; topic: string | null; turn_id: number } | null;
  facts: ContextFact[]; history: { turn_id: number; role: "client" | "bot"; text: string; language: string | null }[];
  cancelled_turns: number[];
};
export type BoardEntry = {
  session_id: string; generation: number; turn_id: number; type: string; author: string;
  payload: Record<string, unknown>; source: string | null; source_id: string | null;
  confidence: number | null; ts_start_ms: number; ts_end_ms: number; context_version: number;
};
export type RouterDebug = { turn_id: number | null; result: { output: unknown; model: string; prompt: string | null; raw: string | null } | null };
export type RealtimeSttSession = { client_secret: string; expires_at: number; model: string };
export type CallStarted = { session_id: string; created: boolean; context: SessionContext; capabilities: Capabilities };
export type ActionEvent = { type: "action"; name: string; mode: "read" | "preview" | "execute" | "handoff" | "unsupported"; params: Record<string, unknown>; ok: boolean; result: unknown; error: Record<string, string> | null };
export type CallError = { type: "error"; stage: string; code: string; message: string; fatal: boolean };
export type AudioEvent = {
  type: "audio"; seq: number; mime: string; data: string; text: string;
  chunk?: number; final?: boolean; filler?: boolean;
};
export type CallEvent = { turn_id: number } & (
  | { type: "transcript"; text: string; language: string | null; source: "stt" | "text" }
  | { type: "turn.started"; session_id: string; generation: number; context_version: number }
  | { type: "routing"; decision: "route" | "clarify" | "handoff"; scenarios: Route[]; alternatives: Route[]; language: string; slots: Record<string, unknown>; is_continuation: boolean; clarify_options: string[] }
  | ActionEvent | { type: "facts"; facts: ContextFact[] }
  | { type: "reply.delta"; text: string } | { type: "reply.done"; text: string; language: string }
  | AudioEvent | CallError | { type: "turn.cancelled" }
  | { type: "turn.done"; trace_id?: string | null; transcript: string; language: string | null; scenarios: Route[]; alternatives: Route[]; reason: string; slots: Record<string, unknown>; actions: string[]; reply: string; latency_ms: Record<string, number | null>; context_version: number }
);
