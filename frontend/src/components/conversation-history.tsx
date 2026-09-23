"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import type { Conversation } from "@/lib/types";
import { scenarios, scenarioTitle, systemIntents } from "@/lib/catalog";
import { useVoice } from "./voice-provider";
import { ContextPanel, TracePanel } from "./conversation-panels";
import { Icon } from "./icon";
import { EmptyState, Modal, Pagination } from "./ui";
import { ListFilters, type FilterValues } from "./list-filters";

function seconds(s: Conversation) { return s.endedAt ? Math.max(0, Math.round((Date.parse(s.endedAt) - Date.parse(s.startedAt)) / 1000)) : 0; }
function duration(s: Conversation) { const value = seconds(s); return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`; }
function localDate(d: Date) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
function redact(text: string) { return text.replace(/\b\d{12}\b/g, "[ИИН скрыт]").replace(/\+?7[\s()-]*\d{3}[\s()-]*\d{3}[\s()-]*\d{2}[\s()-]*\d{2}/g, match => `+7 ••• ••• ${match.replace(/\D/g, "").slice(-4)}`); }
export function ConversationHistory() {
  const v = useVoice(); const { t, locale, history } = v;
  const [input, setInput] = useState(""); const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<FilterValues>({});
  const [sort, setSort] = useState("newest"); const [page, setPage] = useState(1); const [size, setSize] = useState(10);
  const [range, setRange] = useState({ from: "", to: "" }); const [rangeDraft, setRangeDraft] = useState<typeof range | null>(null);
  const [selected, setSelected] = useState<Conversation | null>(null); const [turnId, setTurnId] = useState<number | null>(null);
  const [tab, setTab] = useState<"context" | "trace">("trace");
  useEffect(() => { const timer = setTimeout(() => setQuery(input), 250); return () => clearTimeout(timer); }, [input]);
  const groups = [
    { key: "outcome", label: t("result"), options: ["completed", "handoff", "interrupted", "error"].map(value => ({ value, label: t(value as "completed" | "handoff" | "interrupted" | "error") })) },
    { key: "language", label: t("language"), options: [{ value: "ru", label: "RU" }, { value: "kk", label: "KK" }, { value: "mixed", label: "Mixed" }, { value: "—", label: t("notDetermined") }] },
    { key: "scenario", label: t("scenarios"), options: [...scenarios.map(s => s.scenario_id), ...systemIntents.map(s => s.id)].map(value => ({ value, label: `${value} · ${scenarioTitle(value, locale)}` })) },
    { key: "period", label: t("period"), single: true, options: ["today", "week", "month"].map(value => ({ value, label: t(value as "today" | "week" | "month") })) },
  ];
  const needle = query.trim().toLocaleLowerCase().replace(/\s+/g, " ");
  const rows = history.filter(session => {
    const routes = session.turns.flatMap(turn => turn.scenarios.map(route => route.scenario_id));
    const haystack = redact(`${session.id} ${session.client?.name ?? ""} ${session.claim?.id ?? ""} ${routes.map(id => `${id} ${scenarioTitle(id, locale)}`).join(" ")}`).toLocaleLowerCase();
    if (needle && !haystack.includes(needle)) return false;
    if (filters.outcome?.length && !filters.outcome.includes(session.outcome ?? "completed")) return false;
    if (filters.language?.length && !session.turns.some(turn => filters.language.includes(turn.language))) return false;
    if (filters.scenario?.length && !routes.some(id => filters.scenario.includes(id))) return false;
    const day = localDate(new Date(session.startedAt));
    if (range.from && day < range.from || range.to && day > range.to) return false;
    const period = filters.period?.[0];
    if (period) {
      const today = session.mode === "example" ? "2026-10-01" : localDate(new Date());
      const days = (Date.parse(`${today}T00:00:00Z`) - Date.parse(`${day}T00:00:00Z`)) / 86400000;
      if (days < 0 || days >= (period === "today" ? 1 : period === "week" ? 7 : 30)) return false;
    }
    return true;
  }).sort((a, b) => (sort === "newest" ? Date.parse(b.startedAt) - Date.parse(a.startedAt) : sort === "oldest" ? Date.parse(a.startedAt) - Date.parse(b.startedAt) : sort === "longest" ? seconds(b) - seconds(a) : seconds(a) - seconds(b)) || a.id.localeCompare(b.id));
  const currentPage = Math.min(page, Math.max(1, Math.ceil(rows.length / size)));
  function reset() { setInput(""); setQuery(""); setFilters({}); setRange({ from: "", to: "" }); setSort("newest"); setPage(1); }
  function exportRecord(record: Conversation) {
    const text = redact([`Saqta Voice | ${record.mode === "example" ? "UI DEMO" : "API session"}`, record.id, record.startedAt, `Status: ${record.outcome}`, ...record.turns.flatMap(turn => [`\n${t("client")}: ${turn.text}`, `Saqta Voice: ${turn.answer}`, `Routes: ${turn.scenarios.map(s => s.scenario_id).join(", ")}`, ...turn.facts.map(f => `${f.value} [${f.source}: ${f.source_id}]`), JSON.stringify({ timings: turn.timings, raw: turn.raw }, null, 2)])].join("\n"));
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = `saqta-${record.id}.txt`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000); v.setToast("exportReady");
  }
  const chosenTurn = selected?.turns.find(turn => turn.id === turnId) ?? selected?.turns.at(-1);
  return <><div className="page-heading"><div><div className="eyebrow">CONVERSATION HISTORY</div><h1>{t("historyTitle")}</h1><p>{t("historySubtitle")}</p></div><button className="button secondary" onClick={v.sampleHistory}>{t("sampleHistory")}</button></div>
    {!history.length ? <section className="list-card"><EmptyState icon="history" title={t("noHistory")} description={t("noHistoryText")}><Link href="/" className="button primary">{t("return")}</Link></EmptyState></section> : <section className="list-card"><div className="filters"><label className="search-field"><Icon name="search" size={17} /><input aria-label={t("searchHistory")} placeholder={t("searchHistory")} value={input} onChange={e => { setInput(e.target.value); setPage(1); }} onKeyDown={e => { if (e.key === "Enter") setQuery(input); }} />{input && <button aria-label={t("reset")} onClick={() => { setInput(""); setQuery(""); setPage(1); }}><Icon name="close" size={14} /></button>}</label><label className="sort-select"><span>{t("sort")}</span><select value={sort} onChange={e => { setSort(e.target.value); setPage(1); }}>{["newest", "oldest", "longest", "shortest"].map(value => <option key={value} value={value}>{t(value as "newest" | "oldest" | "longest" | "shortest")}</option>)}</select></label><ListFilters groups={groups} value={filters} onChange={value => { setFilters(value); setPage(1); }} /><button className="button quiet" onClick={() => setRangeDraft({ ...range })}><Icon name="clock" size={15} />{t("customPeriod")}</button>{(range.from || range.to) && <button className="filter-chip" onClick={() => { setRange({ from: "", to: "" }); setPage(1); }}>{range.from || "…"} — {range.to || "…"}<Icon name="close" size={13} /></button>}</div>
    <div className="filter-summary"><span>{t("found")}: {rows.length}</span>{history.some(s => s.mode === "example") && <small>DEMO · {t("today")}: 01.10.2026</small>}<button className="button quiet" onClick={reset}>{t("reset")}</button></div>
    {!rows.length ? <EmptyState icon="search" title={t("noResults")} description={t("changeFilters")}><button className="button secondary" onClick={reset}>{t("reset")}</button></EmptyState> : <div className="table-wrap"><table className="history-table"><thead><tr><th>{t("date")}</th><th>{t("topic")}</th><th>{t("client")}</th><th>{t("language")}</th><th>{t("duration")}</th><th>{t("result")}</th><th><span className="sr-only">{t("details")}</span></th></tr></thead><tbody>{rows.slice((currentPage - 1) * size, currentPage * size).map(session => <tr key={session.id}><td>{new Date(session.startedAt).toLocaleDateString(locale)}<small>{new Date(session.startedAt).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })}{session.mode === "example" && " · DEMO"}</small></td><td><button onClick={() => { setSelected(session); setTurnId(session.turns.at(-1)?.id ?? null); }}>{session.turns.at(-1)?.scenarios[0] ? scenarioTitle(session.turns.at(-1)!.scenarios[0].scenario_id, locale) : t("noRoute")}</button><small className="mono">{session.id.slice(0, 18)}</small></td><td>{session.client?.name ?? t("notDetermined")}<small>{session.claim?.id ?? ""}</small></td><td>{[...new Set(session.turns.map(turn => turn.language))].join(" / ").toUpperCase()}</td><td className="mono">{duration(session)}</td><td><span className={`badge ${session.outcome === "completed" ? "green" : "amber"}`}>{t(session.outcome ?? "completed")}</span></td><td><button className="icon-button" aria-label={`${t("details")} ${session.id}`} onClick={() => { setSelected(session); setTurnId(session.turns.at(-1)?.id ?? null); }}><Icon name="arrow" size={16} /></button></td></tr>)}</tbody></table></div>}
    <Pagination page={currentPage} size={size} total={rows.length} onPage={setPage} onSize={value => { setSize(value); setPage(1); }} t={t} /></section>}
    {rangeDraft && <Modal title={t("period")} onClose={() => setRangeDraft(null)}><div className="date-range"><label>{t("dateFrom")}<input type="date" value={rangeDraft.from} onChange={e => setRangeDraft({ ...rangeDraft, from: e.target.value })} /></label><label>{t("dateTo")}<input type="date" value={rangeDraft.to} onChange={e => setRangeDraft({ ...rangeDraft, to: e.target.value })} /></label></div>{rangeDraft.from && rangeDraft.to && rangeDraft.from > rangeDraft.to && <p role="alert">{t("dateError")}</p>}<div className="dialog-actions"><button className="button secondary" onClick={() => setRangeDraft(null)}>{t("cancel")}</button><button className="button primary" disabled={!!rangeDraft.from && !!rangeDraft.to && rangeDraft.from > rangeDraft.to} onClick={() => { setRange(rangeDraft); setFilters({ ...filters, period: [] }); setRangeDraft(null); setPage(1); }}>{t("applyFilters")}</button></div></Modal>}
    {selected && <Modal title={`${t("history")} · ${new Date(selected.startedAt).toLocaleString(locale)}`} onClose={() => setSelected(null)} wide drawer><div className="history-detail-meta"><span className="badge">{t(selected.outcome ?? "completed")}</span><span className="badge">{duration(selected)}</span>{selected.mode === "example" && <span className="badge">DEMO</span>}<button className="button secondary" onClick={() => exportRecord(selected)}><Icon name="download" size={16} />{t("export")}</button></div><p className="no-audio">{t("noAudio")}</p><p className="no-audio">{t("localOnly")}</p><div className="history-transcript">{selected.turns.map(turn => <div className="turn-group" key={turn.id}><article className="message client-message"><div className="message-meta">{t("client")} · {turn.language.toUpperCase()}</div><p>{turn.text}</p></article><article className="message assistant-message"><div className="message-meta">Saqta Voice</div><p>{turn.answer || t("unavailable")}</p><button className="turn-route" onClick={() => { setTurnId(turn.id); setTab("trace"); }}>{t("trace")} {turn.id}<Icon name="arrow" size={14} /></button></article></div>)}</div><div className="segmented"><button aria-pressed={tab === "context"} onClick={() => setTab("context")}>{t("context")}</button><button aria-pressed={tab === "trace"} onClick={() => setTab("trace")}>{t("trace")}</button></div>{tab === "context" ? <ContextPanel session={selected} /> : <TracePanel session={selected} turn={chosenTurn} onSelect={setTurnId} />}</Modal>}
  </>;
}
