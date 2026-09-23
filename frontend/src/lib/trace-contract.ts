// HTTP DTOs owned by app.tracer (docs/specs/tracer-module.md).
export type TraceAttributes = Record<string, unknown>;
export type ModelUsage = { input: number; output: number; calls: number };
export type TokenTotals = { input: number; output: number; total: number };
export type TraceError = {
  trace_id: string; span_id: string; span_name: string;
  code: string | null; stage: string | null; message: string | null; time: string | null;
};
export type TraceSummary = {
  trace_id: string; root_name: string; start_time: string | null;
  duration_ms: number | null; span_count: number;
  session_id: string | null; turn_id: string | null; error: boolean;
};
export type SpanView = {
  trace_id: string; span_id: string; parent_span_id: string | null;
  name: string; kind: string; start_time: string | null; end_time: string | null;
  duration_ms: number | null; status: string; status_message: string | null;
  session_id: string | null; attributes: TraceAttributes;
  events: { name: string; time: string | null; attributes: TraceAttributes }[];
  links: { trace_id: string; span_id: string }[];
  resource: TraceAttributes; children: SpanView[];
};
export type TraceView = TraceSummary & { spans: SpanView[] };
export type TurnTrace = {
  turn_id: number | null; trace_id: string; started_at: string | null;
  duration_ms: number | null; transcript: string | null; input_source: string | null;
  router: { decision: string | null; scenario_id: string | null; confidence: number | null };
  stages: Record<string, number>; tokens_by_model: Record<string, ModelUsage>; errors: TraceError[];
};
export type SessionTraceSummary = {
  session_id: string; first_seen: string | null; last_seen: string | null;
  duration_ms: number | null; traces: number; turns: number; errors: number; llm_calls: number;
  tokens: TokenTotals; tokens_by_model: Record<string, ModelUsage>;
  last_transcript: string | null; scenarios: string[];
};
export type SessionTrace = { summary: SessionTraceSummary; turns: TurnTrace[]; traces: TraceSummary[] };
