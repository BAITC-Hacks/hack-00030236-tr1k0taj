"use client";
import Link from "next/link";
import { useCallback, useState } from "react";
import { useTraceResource } from "@/hooks/use-trace-resource";
import { traceApi } from "@/lib/trace-api";
import { formatTraceDate, formatTraceMs, traceErrorText, traceStageTitle, traceText, type TraceTextKey } from "@/lib/trace-i18n";
import type { ModelUsage, SpanView, TraceError } from "@/lib/trace-contract";
import type { Locale } from "@/lib/types";
import { useCatalog } from "./catalog-provider";
import { useVoice } from "./voice-provider";
import { Icon } from "./icon";

type Props = { sessionId: string; live?: boolean; traceId?: string; compact?: boolean };
type Translator = (key: TraceTextKey) => string;

function ModelTokens({ models, locale }: { models: Record<string, ModelUsage>; locale: Locale }) {
  const tr: Translator = key => traceText(locale, key);
  return <details className="server-trace-disclosure"><summary>{tr("tokens")} <span>{Object.keys(models).length}</span></summary>
    {Object.keys(models).length ? <div className="server-trace-table-wrap"><table className="server-trace-token-table"><thead><tr><th>{tr("model")}</th><th>{tr("input")}</th><th>{tr("output")}</th><th>{tr("calls")}</th></tr></thead><tbody>{Object.entries(models).map(([name, usage]) => <tr key={name}><th>{name}</th><td>{usage.input.toLocaleString(locale)}</td><td>{usage.output.toLocaleString(locale)}</td><td>{usage.calls.toLocaleString(locale)}</td></tr>)}</tbody></table></div> : <p className="server-trace-note">{tr("noUsage")}</p>}
  </details>;
}

function Errors({ errors, locale }: { errors: TraceError[]; locale: Locale }) {
  if (!errors.length) return null;
  return <details className="server-trace-disclosure server-trace-errors"><summary>{traceText(locale, "rawErrors")} <span>{errors.length}</span></summary>{errors.map(error => <div className="server-trace-error-item" key={`${error.trace_id}:${error.span_id}`}><strong>{traceStageTitle(error.stage ?? error.span_name, locale)}{error.code ? ` · ${error.code}` : ""}</strong>{error.message && <p>{error.message}</p>}<small>{formatTraceDate(error.time, locale)}</small></div>)}</details>;
}

function spanErrors(spans: SpanView[]): TraceError[] {
  return spans.flatMap(span => {
    const errors: TraceError[] = span.status === "error" || typeof span.attributes["error.code"] === "string" ? [{
      trace_id: span.trace_id, span_id: span.span_id, span_name: span.name,
      code: typeof span.attributes["error.code"] === "string" ? span.attributes["error.code"] : null,
      stage: typeof span.attributes["error.stage"] === "string" ? span.attributes["error.stage"] : null,
      message: span.status_message, time: span.end_time,
    }] : [];
    return [...errors, ...spanErrors(span.children)];
  });
}

function SpanNode({ span, depth, locale, selectTrace }: { span: SpanView; depth: number; locale: Locale; selectTrace: (id: string) => void }) {
  const tr: Translator = key => traceText(locale, key);
  const status = span.status === "ok" ? tr("statusOk") : span.status === "error" ? tr("statusError") : span.status === "unset" ? tr("statusUnset") : span.status;
  return <details className={`server-trace-span ${span.status === "error" ? "server-trace-span-error" : ""}`} open={depth === 0}>
    <summary><span className="server-trace-span-name">{traceStageTitle(span.name, locale)}{traceStageTitle(span.name, locale) !== span.name && <small>{span.name}</small>}</span><span className="server-trace-ms">{formatTraceMs(span.duration_ms, locale)}</span></summary>
    <div className="server-trace-span-body"><div className="server-trace-span-meta"><span className="badge">{span.kind}</span><span className={`badge ${span.status === "error" ? "amber" : span.status === "ok" ? "green" : ""}`}>{status}</span></div>
      {span.status_message && <details className="server-trace-disclosure"><summary>{tr("status")}</summary><p className={span.status === "error" ? "server-trace-error-text" : ""}>{span.status_message}</p></details>}
      <dl className="server-trace-data"><div><dt>{tr("first")}</dt><dd>{formatTraceDate(span.start_time, locale)}</dd></div><div><dt>{tr("last")}</dt><dd>{formatTraceDate(span.end_time, locale)}</dd></div><div><dt>span_id</dt><dd className="mono">{span.span_id}</dd></div><div><dt>{tr("parent")}</dt><dd className="mono">{span.parent_span_id ?? "—"}</dd></div></dl>
      {!!Object.keys(span.attributes).length && <details className="server-trace-disclosure"><summary>{tr("attributes")} <span>{Object.keys(span.attributes).length}</span></summary><pre>{JSON.stringify(span.attributes, null, 2)}</pre></details>}
      {!!span.events.length && <details className="server-trace-disclosure"><summary>{tr("events")} <span>{span.events.length}</span></summary>{span.events.map((event, index) => <div className="server-trace-event" key={`${event.name}:${event.time}:${index}`}><strong>{event.name}</strong><small>{formatTraceDate(event.time, locale)}</small><pre>{JSON.stringify(event.attributes, null, 2)}</pre></div>)}</details>}
      {!!span.links.length && <details className="server-trace-disclosure"><summary>{tr("links")} <span>{span.links.length}</span></summary>{span.links.map(link => <div className="server-trace-link" key={`${link.trace_id}:${link.span_id}`}><button onClick={() => selectTrace(link.trace_id)} className="button quiet">{tr("linked")}<Icon name="arrow" size={13} /></button><code>{link.trace_id}</code><small>span_id: {link.span_id}</small></div>)}</details>}
      {!!Object.keys(span.resource).length && <details className="server-trace-disclosure"><summary>{tr("resource")}</summary><pre>{JSON.stringify(span.resource, null, 2)}</pre></details>}
      {!!span.children.length && <div className="server-trace-children">{span.children.map(child => <SpanNode key={child.span_id} span={child} depth={depth + 1} locale={locale} selectTrace={selectTrace} />)}</div>}
    </div>
  </details>;
}

function SessionTraceContent({ sessionId, live = false, traceId, compact = false }: Props) {
  const { locale, t, copy } = useVoice();
  const { scenarioTitle } = useCatalog();
  const tr: Translator = key => traceText(locale, key);
  const [selection, setSelection] = useState<{ input?: string; id?: string }>({ input: traceId });
  const loadSession = useCallback((signal: AbortSignal) => traceApi.session(sessionId, signal), [sessionId]);
  const session = useTraceResource(sessionId, loadSession, live);
  const selected = (selection.input === traceId ? selection.id : undefined) ?? traceId ?? session.data?.turns.at(-1)?.trace_id ?? session.data?.traces.at(-1)?.trace_id;
  const loadTrace = useCallback((signal: AbortSignal) => traceApi.trace(selected ?? "", signal), [selected]);
  const tree = useTraceResource(selected ? `${sessionId}:${selected}` : null, loadTrace, live);
  const selectTrace = (id: string) => setSelection({ input: traceId, id });
  const summary = session.data?.summary;
  const turn = session.data?.turns.find(item => item.trace_id === selected);
  const refresh = () => { void session.refresh(); void tree.refresh(); };
  const link = `/history?session=${encodeURIComponent(sessionId)}${selected ? `&trace=${encodeURIComponent(selected)}` : ""}`;
  const sessionIssue = session.status === "missing" || session.status === "error";
  const errors = turn?.errors.length ? turn.errors : spanErrors(tree.data?.spans ?? []);
  const errorMessages = [...new Set(errors.map(error => traceErrorText(error, locale)))];
  const decision = turn?.router.decision;
  const resultTitle = errors.length ? tr("statusError") : decision === "clarify" ? tr("clarify") : decision === "handoff" ? tr("handoff") : turn?.router.scenario_id ? tr("routeSelected") : tr("noDecision");

  return <section className={`server-trace ${compact ? "server-trace-compact" : ""}`} aria-label={tr("title")}>
    <header className="server-trace-heading"><div><h3>{tr("title")}</h3><p><span className={`tiny-dot ${live ? "green" : "gray"}`} />{tr(live ? "live" : "saved")}</p></div><button className="button secondary" disabled={session.refreshing || tree.refreshing} onClick={refresh}>{tr("refresh")}</button></header>
    {session.status === "loading" && !session.data && <p className="server-trace-empty" role="status">{tr("loading")}</p>}
    {sessionIssue && <div className={`server-trace-empty ${session.status === "error" ? "server-trace-error-text" : ""}`} role={session.status === "error" ? "alert" : "status"}><strong>{tr(session.status === "missing" ? "waiting" : "failed")}</strong><p>{tr(session.status === "missing" ? "waitingText" : "failedText")}</p>{session.data && <small>{tr("stale")}</small>}</div>}
    {(turn || tree.data || (selected && !sessionIssue)) && <section className={`server-trace-result ${errors.length ? "server-trace-result-error" : ""}`}>
      <div className="server-trace-result-caption"><span>{tr("result")}{turn?.turn_id != null ? ` · ${turn.turn_id}` : ""}</span>{turn && <span>{formatTraceMs(turn.duration_ms, locale)}</span>}</div>
      {turn?.transcript && <div className="server-trace-question"><span>{tr("question")}</span><p>{turn.transcript}</p></div>}
      <h4>{resultTitle}</h4>
      {errorMessages.map(message => <p className="server-trace-result-explanation" key={message}>{message}</p>)}
      {turn?.router.scenario_id && <p className="server-trace-result-route">{errors.length ? `${tr("route")}: ` : ""}{scenarioTitle(turn.router.scenario_id)}<small>{turn.router.scenario_id}</small></p>}
      {!turn && !errors.length && <p className="server-trace-result-explanation">{tr(live ? "pendingResult" : "serviceTraceText")}</p>}
      <Errors errors={errors} locale={locale} />
    </section>}
    {summary && <>
      <section className="server-trace-section"><h4>{tr("turns")} <span>{session.data!.turns.length}</span></h4>
        {!session.data!.turns.length ? <p className="server-trace-note">{tr("noTurns")}</p> : <div className="server-trace-turns">{session.data!.turns.map(item => <button key={`${item.trace_id}:${item.turn_id}`} className="server-trace-turn" aria-pressed={selected === item.trace_id} onClick={() => selectTrace(item.trace_id)}><span><strong>{tr("turn")} {item.turn_id ?? "—"}</strong><small>{formatTraceMs(item.duration_ms, locale)}</small></span><p>{item.transcript ?? tr("noTranscript")}</p><small>{item.router.scenario_id ? `${item.router.scenario_id} · ${scenarioTitle(item.router.scenario_id)}` : "—"}{item.errors.length > 0 && ` · ${tr("errors")}: ${item.errors.length}`}</small></button>)}</div>}
      </section>
    </>}
    <details className="server-trace-disclosure server-trace-summary"><summary>{tr("sessionSummary")}</summary>
      <div className="server-trace-identity"><span>{tr("session")}</span><code>{sessionId}</code><button className="icon-button" aria-label={`${t("copy")} ${tr("session")}`} onClick={() => void copy(sessionId)}><Icon name="copy" size={14} /></button></div>
      {summary && <><div className="server-trace-metrics">{([ ["turns", summary.turns], ["traces", summary.traces], ["calls", summary.llm_calls], ["errors", summary.errors] ] as const).map(([label, value]) => <div key={label}><span>{tr(label)}</span><strong className={label === "errors" && value ? "server-trace-error-text" : ""}>{value.toLocaleString(locale)}</strong></div>)}</div>
        <dl className="server-trace-data"><div><dt>{tr("period")}</dt><dd>{formatTraceMs(summary.duration_ms, locale)}</dd></div><div><dt>{tr("first")}</dt><dd>{formatTraceDate(summary.first_seen, locale)}</dd></div><div><dt>{tr("last")}</dt><dd>{formatTraceDate(summary.last_seen, locale)}</dd></div></dl>
        <div className="server-trace-token-totals"><span>{tr("input")}: <strong>{summary.tokens.input.toLocaleString(locale)}</strong></span><span>{tr("output")}: <strong>{summary.tokens.output.toLocaleString(locale)}</strong></span><span>Σ <strong>{summary.tokens.total.toLocaleString(locale)}</strong></span></div>
        <ModelTokens models={summary.tokens_by_model} locale={locale} />
      </>}
    </details>
    <details className="server-trace-disclosure server-trace-technical"><summary>{tr("technical")}</summary>
      {turn && <section className="server-trace-section"><h4>{tr("stages")} · {turn.turn_id ?? "—"}</h4><dl className="server-trace-data"><div><dt>{tr("source")}</dt><dd>{turn.input_source === "audio" ? tr("audioInput") : turn.input_source === "text" ? tr("textInput") : turn.input_source ?? "—"}</dd></div><div><dt>{tr("decision")}</dt><dd>{decision === "route" ? tr("routeSelected") : decision === "clarify" ? tr("clarify") : decision === "handoff" ? tr("handoff") : decision ?? "—"}</dd></div><div><dt>{tr("confidence")}</dt><dd>{turn.router.confidence === null ? "—" : turn.router.confidence.toLocaleString(locale, { maximumFractionDigits: 3 })}</dd></div></dl>
        {Object.keys(turn.stages).length ? <dl className="server-trace-data server-trace-stages">{Object.entries(turn.stages).map(([name, duration]) => <div key={name}><dt>{traceStageTitle(name, locale)}<small>{name}</small></dt><dd>{formatTraceMs(duration, locale)}</dd></div>)}</dl> : <p className="server-trace-note">{tr("noStages")}</p>}
        <ModelTokens models={turn.tokens_by_model} locale={locale} />
      </section>}
      <section className="server-trace-section"><h4>{tr("tree")}</h4>
        {!!session.data?.traces.length && <label className="server-trace-select">{tr("trace")}<select value={selected ?? ""} onChange={event => selectTrace(event.target.value)}>{selected && !session.data.traces.some(item => item.trace_id === selected) && <option value={selected}>{tr("linked")} · {selected.slice(0, 12)}</option>}{session.data.traces.map(item => <option key={item.trace_id} value={item.trace_id}>{item.turn_id !== null ? `${tr("turn")} ${item.turn_id} · ` : ""}{traceStageTitle(item.root_name, locale)} · {item.trace_id.slice(0, 8)}</option>)}</select></label>}
        {selected && <code className="server-trace-trace-id">{selected}</code>}
        {!selected && <p className="server-trace-note">{tr("noTree")}</p>}
        {selected && tree.status === "loading" && <p className="server-trace-note" role="status">{tr("loading")}</p>}
        {selected && (tree.status === "missing" || tree.status === "error") && <div className="server-trace-empty" role={tree.status === "error" ? "alert" : "status"}><strong>{tr(tree.status === "missing" ? "waiting" : "failed")}</strong><p>{tr(tree.status === "missing" ? "waitingText" : "failedText")}</p>{tree.data && <small>{tr("stale")}</small>}<button className="button secondary" onClick={() => void tree.refresh()} disabled={tree.refreshing}>{tr("refresh")}</button></div>}
        {tree.data && <><div className="server-trace-tree-meta"><span>{tr("span")}: {tree.data.span_count}</span><span>{formatTraceMs(tree.data.duration_ms, locale)}</span></div>{tree.data.spans.length ? <div className="server-trace-tree">{tree.data.spans.map(span => <SpanNode key={span.span_id} span={span} depth={0} locale={locale} selectTrace={selectTrace} />)}</div> : <p className="server-trace-note">{tr("noTree")}</p>}</>}
        {(tree.data || turn) && <details className="server-trace-disclosure"><summary>{tr("raw")}</summary><pre>{JSON.stringify({ turn, trace: tree.data }, null, 2)}</pre></details>}
      </section>
      <p className="server-trace-note">{tr("timingNote")}</p>
    </details>
    {compact && <Link href={link} className="server-trace-open">{tr("inspect")}<Icon name="arrow" size={14} /></Link>}
    {session.updatedAt && <small className="server-trace-updated">{tr("updated")}: {formatTraceDate(session.updatedAt, locale)}</small>}
  </section>;
}

// Isolate selection and in-flight requests even when the caller omits a React key.
export function SessionTracePanel(props: Props) { return <SessionTraceContent key={props.sessionId} {...props} />; }
