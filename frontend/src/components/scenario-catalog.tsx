"use client";
import { useEffect, useState } from "react";
import { scenarios, systemIntents, scenarioTitle, domainTitle, type Scenario } from "@/lib/catalog";
import { useVoice } from "./voice-provider";
import { Icon } from "./icon";
import { EmptyState, Modal, Pagination } from "./ui";
import { ListFilters, type FilterValues } from "./list-filters";

export function ScenarioCatalog() {
  const { t, locale, capabilities, copy } = useVoice();
  const [kind, setKind] = useState<"business" | "systems">("business");
  const [queries, setQueries] = useState({ business: "", systems: "" });
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<FilterValues>({});
  const [sort, setSort] = useState("id"); const [page, setPage] = useState(1); const [size, setSize] = useState(20);
  const [detail, setDetail] = useState<string[]>([]);
  const input = queries[kind];
  useEffect(() => { const timer = setTimeout(() => setQuery(input), 250); return () => clearTimeout(timer); }, [input]);
  const text = query.trim().toLocaleLowerCase().replace(/\s+/g, " ");
  const match = (key: string, value: string) => !filters[key]?.length || filters[key].includes(value);
  const groups = [
    { key: "domain", label: t("domain"), options: [...new Set(scenarios.map(s => s.domain))].map(value => ({ value, label: domainTitle(value, locale) })) },
    { key: "category", label: t("category"), options: [...new Set(scenarios.map(s => s.category))].map(value => ({ value, label: t(value as "sales" | "claims" | "servicing" | "contact" | "feedback" | "info" | "security") })) },
    { key: "priority", label: t("priority"), options: ["normal", "high", "urgent"].map(value => ({ value, label: t(value as "normal" | "high" | "urgent") })) },
    ...["identification", "confirmation"].map(key => ({ key, label: t(key as "identification" | "confirmation"), single: true, options: [{ value: "true", label: t("yes") }, { value: "false", label: t("no") }] })),
  ];
  const rows = scenarios.filter(s => `${s.scenario_id} ${s.name} ${scenarioTitle(s.scenario_id, locale)} ${s.description} ${s.examples.ru.join(" ")} ${s.examples.kk.join(" ")}`.toLocaleLowerCase().includes(text) && match("domain", s.domain) && match("category", s.category) && match("priority", s.priority) && match("identification", String(s.requires_identification)) && match("confirmation", String(s.requires_confirmation))).sort((a, b) => {
    if (sort === "name") return scenarioTitle(a.scenario_id, locale).localeCompare(scenarioTitle(b.scenario_id, locale), locale);
    if (sort === "priority") { const rank = (p: string) => p === "urgent" ? 0 : p === "high" ? 1 : 2; return rank(a.priority) - rank(b.priority) || a.scenario_id.localeCompare(b.scenario_id); }
    return a.scenario_id.localeCompare(b.scenario_id);
  });
  const systems = systemIntents.filter(s => `${s.id} ${scenarioTitle(s.id, locale)} ${s.description} ${s.behavior} ${s.response[locale]}`.toLocaleLowerCase().includes(text));
  const currentPage = Math.min(page, Math.max(1, Math.ceil(rows.length / size)));
  const selected = scenarios.find(s => s.scenario_id === detail.at(-1));
  const selectedSystem = systemIntents.find(s => s.id === detail.at(-1));
  function reset() { setQueries({ business: "", systems: "" }); setQuery(""); setFilters({}); setSort("id"); setPage(1); }
  function support(s: Scenario) {
    if (!capabilities) return t("supportUnknown");
    const count = s.actions.filter(action => capabilities.supported_actions.includes(action)).length;
    return count === s.actions.length ? t("supportAll") : count ? t("supportPartial") : t("supportNone");
  }
  return <><div className="page-heading"><div><div className="eyebrow">SCENARIO LIBRARY</div><h1>{t("scenarios")}</h1><p>{t("catalogSubtitle")}</p></div><div className="catalog-counts"><span className="badge green">{scenarios.length} {t("business")}</span><span className="badge">{systemIntents.length} {t("systems")}</span></div></div>
    <div className="segmented">{(["business", "systems"] as const).map(value => <button key={value} aria-pressed={kind === value} onClick={() => { setKind(value); setQuery(queries[value]); setPage(1); }}>{t(value)}</button>)}</div>
    <section className="list-card"><div className="filters"><label className="search-field"><Icon name="search" size={17} /><input aria-label={t("searchScenarios")} placeholder={t("searchScenarios")} value={input} onChange={e => { setQueries({ ...queries, [kind]: e.target.value }); setPage(1); }} onKeyDown={e => { if (e.key === "Enter") setQuery(input); }} />{input && <button aria-label={t("reset")} onClick={() => { setQueries({ ...queries, [kind]: "" }); setQuery(""); setPage(1); }}><Icon name="close" size={14} /></button>}</label>{kind === "business" && <><label className="sort-select"><span>{t("sort")}</span><select value={sort} onChange={e => { setSort(e.target.value); setPage(1); }}><option value="id">ID</option><option value="name">{t("byName")}</option><option value="priority">{t("urgentFirst")}</option></select></label><ListFilters groups={groups} value={filters} onChange={value => { setFilters(value); setPage(1); }} /></>}</div>
    <div className="filter-summary"><span>{t("found")}: {kind === "business" ? rows.length : systems.length}</span><button className="button quiet" onClick={reset}>{t("reset")}</button></div>
    {(kind === "business" ? rows.length : systems.length) === 0 ? <EmptyState icon="search" title={t("noResults")} description={t("changeFilters")}><button className="button secondary" onClick={reset}>{t("reset")}</button></EmptyState> : <div className="catalog-list">{kind === "business" ? rows.slice((currentPage - 1) * size, currentPage * size).map(s => <button className="scenario-card" key={s.scenario_id} onClick={() => setDetail([s.scenario_id])}><div className="scenario-card-top"><span className="scenario-id">{s.scenario_id}</span><span className="badge">{domainTitle(s.domain, locale)}</span>{s.priority !== "normal" && <span className="badge amber">{t(s.priority as "high" | "urgent")}</span>}</div><h3>{scenarioTitle(s.scenario_id, locale)}</h3><p>{s.examples[locale][0]}</p><div className="scenario-card-footer">{s.requires_identification && <span><Icon name="shield" size={12} />{t("identification")}</span>}{s.requires_confirmation && <span><Icon name="check" size={12} />{t("confirmation")}</span>}<Icon name="arrow" size={15} /></div><small className="support-note">{support(s)}</small></button>) : systems.map(s => <button className="scenario-card" key={s.id} onClick={() => setDetail([s.id])}><span className="scenario-id">{s.id}</span><h3>{scenarioTitle(s.id, locale)}</h3><p>{s.response[locale]}</p><span className="scenario-card-footer">{t("details")}<Icon name="arrow" size={15} /></span></button>)}</div>}
    {kind === "business" && <Pagination page={currentPage} size={size} total={rows.length} onPage={setPage} onSize={value => { setSize(value); setPage(1); }} options={[10, 20, 40]} t={t} />}</section><p className="catalog-disclaimer">{t("capabilityNote")}</p>
    {(selected || selectedSystem) && <Modal title={scenarioTitle(detail.at(-1)!, locale)} onClose={() => setDetail([])} wide drawer>{detail.length > 1 && <button className="button quiet" onClick={() => setDetail(detail.slice(0, -1))}>{t("prev")} · {detail.at(-2)}</button>}<div className="detail-title"><span className="scenario-id">{detail.at(-1)}</span>{selected && <><span className="badge">{domainTitle(selected.domain, locale)}</span><span className="badge">{t(selected.priority as "normal" | "high" | "urgent")}</span></>}</div>{selected ? <><p>{selected.description}</p><p className="support-note">{support(selected)}</p><section className="detail-section"><h3>{t("boundaries")}</h3><p>{t("original")}</p>{selected.not_this_if.map((boundary, index) => <div className="boundary" key={index}><p>{boundary.condition}</p><button className="button quiet" onClick={() => setDetail([...detail, boundary.use_instead])}>{boundary.use_instead}<Icon name="arrow" size={14} /></button></div>)}</section>{(["required", "optional"] as const).map(kind => <section className="detail-section" key={kind}><h3>{t(kind)}</h3><div className="token-list">{selected.slots[kind].length ? selected.slots[kind].map(slot => <span key={slot}>{slot}</span>) : "—"}</div></section>)}<section className="detail-section"><h3>{t("actions")}</h3>{selected.actions.map(action => <p key={action}>{action} · {capabilities ? capabilities.supported_actions.includes(action) ? t("supportAll") : t("supportNone") : t("supportUnknown")}</p>)}{selected.handoff && <p>{t("handoff")}: {JSON.stringify(selected.handoff)}</p>}</section><section className="detail-section"><h3>{t("phrases")}</h3>{(["ru", "kk"] as const).map(lang => <div key={lang}><h4>{lang.toUpperCase()}</h4>{selected.examples[lang].map(phrase => <button className="phrase-button" key={phrase} title={t("copy")} onClick={() => void copy(phrase)}>{phrase}<Icon name="copy" size={16} /></button>)}</div>)}</section></> : <><p>{selectedSystem!.description}</p><section className="detail-section"><p>{selectedSystem!.behavior}</p><p>{selectedSystem!.response[locale]}</p></section></>}<details className="disclosure"><summary>{t("source")}: scenarios.json</summary><pre>{JSON.stringify(selected ?? selectedSystem, null, 2)}</pre></details></Modal>}
  </>;
}
