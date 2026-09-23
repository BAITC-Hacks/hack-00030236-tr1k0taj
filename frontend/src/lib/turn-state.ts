import type { CallEvent, ContextFact, SessionContext } from "./call-contract";
import type { Conversation, Fact, Turn } from "./types";

export function displayFact(fact: ContextFact): Fact {
  return { ...fact, value: typeof fact.value === "string" ? fact.value : JSON.stringify(fact.value), source_id: fact.source_id ?? fact.source, origin: fact.origin === "background" ? "background" : "required" };
}
export function applyEvent(turn: Turn, event: CallEvent): Turn {
  switch (event.type) {
    case "transcript": return { ...turn, text: event.text, language: event.language ?? "—" };
    case "turn.started": return { ...turn, id: event.turn_id, contextVersion: event.context_version };
    case "routing": return { ...turn, scenarios: event.scenarios, alternatives: event.alternatives, slots: event.slots, language: event.language, decision: event.decision };
    case "facts": return { ...turn, facts: event.facts.map(displayFact) };
    case "action": return { ...turn, actions: [...(turn.actions ?? []), event] };
    case "reply.delta": return { ...turn, answer: turn.answer + event.text };
    case "reply.done": return { ...turn, answer: event.text, language: event.language };
    case "error": return { ...turn, errors: [...(turn.errors ?? []), event], status: event.fatal ? "error" : turn.status };
    case "turn.cancelled": return { ...turn, status: "cancelled" };
    case "turn.done": return { ...turn, traceId: event.trace_id ?? turn.traceId, text: event.transcript, answer: event.reply, language: event.language ?? "—", scenarios: event.scenarios, alternatives: event.alternatives, slots: event.slots, contextVersion: event.context_version, raw: event, status: turn.errors?.length ? "error" : "done", timings: { ...turn.timings, ...Object.fromEntries(Object.entries(event.latency_ms).filter((entry): entry is [string, number] => typeof entry[1] === "number")) } };
    default: return turn;
  }
}
export function applyContext(session: Conversation, context: SessionContext): Conversation {
  if (context.session_id !== session.id || context.generation !== session.generation) return session;
  const confirmation = context.pending_confirmation;
  return { ...session, context, facts: context.facts.map(displayFact), pendingTopics: context.pending_topics,
    client: context.client_id ? { name: context.client_id, phone: "", city: "" } : undefined,
    confirmation: confirmation ? { action: confirmation.action, params: confirmation.params, turnId: confirmation.turn_id } : undefined,
    helper: context.facts.some(fact => fact.origin === "background") ? "ready" : session.helper,
  };
}
