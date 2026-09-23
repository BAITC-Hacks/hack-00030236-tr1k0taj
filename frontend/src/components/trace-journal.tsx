"use client";
import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { traceApi } from "@/lib/trace-api";
import type { SessionTraceSummary } from "@/lib/trace-contract";
import { formatTraceDate, formatTraceMs } from "@/lib/trace-i18n";
import { useVoice } from "./voice-provider";
import { useCatalog } from "./catalog-provider";
import { SessionTracePanel } from "./session-trace";
import { EmptyState, Modal } from "./ui";
import { Icon } from "./icon";

export function TraceJournal() {
  const { locale, t, session } = useVoice(); const { scenarioTitle } = useCatalog();
  const router = useRouter(); const params = useSearchParams();
  const selected = params.get("session")?.toLowerCase(); const selectedTrace = params.get("trace") ?? undefined;
  const [page, setPage] = useState(0); const [size, setSize] = useState(10); const [revision, setRevision] = useState(0);
  const [lookup, setLookup] = useState(""); const [invalid, setInvalid] = useState(false);
  const [result, setResult] = useState<{ key: string; rows: SessionTraceSummary[]; error: boolean } | null>(null);
  const key = `${page}:${size}:${revision}`;
  const loading = result?.key !== key;
  const rows = loading ? [] : result?.rows.slice(0, size) ?? [];
  const hasNext = !loading && (result?.rows.length ?? 0) > size;
  const label = (ru: string, kk: string) => locale === "ru" ? ru : kk;
  useEffect(() => {
    const controller = new AbortController();
    traceApi.sessions(size + 1, page * size, controller.signal).then(rows => {
      if (!controller.signal.aborted) setResult({ key, rows, error: false });
    }).catch(() => { if (!controller.signal.aborted) setResult({ key, rows: [], error: true }); });
    return () => controller.abort();
  }, [key, size, page]);
  function open(id: string) { router.push(`/history?session=${encodeURIComponent(id)}`, { scroll: false }); }
  const active = session?.mode === "live" && !session.endedAt && session.id === selected;
  return <>
    <div className="page-heading"><div><div className="eyebrow">CALL OBSERVABILITY</div><h1>{t("history")}</h1><p>{label("Звонки и события с сервера. Выберите сессию, чтобы увидеть весь путь обработки.", "Сервердегі қоңыраулар мен оқиғалар. Өңдеу жолын көру үшін сеансты таңдаңыз.")}</p></div><button className="button secondary" disabled={loading} onClick={() => setRevision(v => v + 1)}><Icon name="history" size={16} />{label("Обновить", "Жаңарту")}</button></div>
    <div className="journal-intro"><Icon name="layers" size={19} /><p>{label("Одна строка — один звонок по UUID. Внутри: реплики, этапы, ошибки и использование моделей.", "Әр жол — UUID бойынша бір қоңырау. Ішінде: сөздер, кезеңдер, қателер және модельдерді пайдалану.")}</p>{session?.mode === "live" && <button className="button quiet" onClick={() => open(session.id)}>{label("Текущий звонок", "Ағымдағы қоңырау")}<Icon name="arrow" size={14} /></button>}</div>
    <section className="list-card" aria-busy={loading}>
      <form className="filters" onSubmit={event => { event.preventDefault(); const id = lookup.trim().toLowerCase(); const valid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id); setInvalid(!valid); if (valid) open(id); }}>
        <label className="search-field"><Icon name="search" size={17} /><input value={lookup} onChange={event => { setLookup(event.target.value); setInvalid(false); }} aria-label={label("UUID звонка", "Қоңырау UUID-і")} placeholder={label("Открыть звонок по UUID", "Қоңырауды UUID бойынша ашу")} spellCheck={false} /></label><button className="button secondary" disabled={!lookup.trim()}>{label("Открыть звонок", "Қоңырауды ашу")}</button>
        {invalid && <p className="journal-input-error" role="alert">{label("Укажите UUID в формате xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.", "UUID-ді xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx пішімінде енгізіңіз.")}</p>}
      </form>
      {loading ? <div role="status"><EmptyState icon="layers" title={label("Загружаем звонки…", "Қоңыраулар жүктелуде…")} /></div> : result?.error ? <div role="alert"><EmptyState icon="info" title={label("Журнал недоступен", "Журнал қолжетімсіз")} description={label("Проверьте соединение и повторите загрузку.", "Байланысты тексеріп, қайта жүктеңіз.")}><button className="button primary" onClick={() => setRevision(v => v + 1)}>{label("Повторить", "Қайталау")}</button></EmptyState></div> : !rows.length ? <EmptyState icon="call" title={label("Здесь пока нет звонков", "Мұнда қоңыраулар әзірге жоқ")} description={label("Трейсы появятся после первых серверных событий звонка. Их сохранение может занять несколько секунд.", "Трейстер қоңыраудың алғашқы серверлік оқиғаларынан кейін пайда болады. Сақтау бірнеше секунд алуы мүмкін.")} /> : <div className="table-wrap"><table className="trace-journal-table"><thead><tr><th>{label("Звонок / последняя реплика", "Қоңырау / соңғы сөз")}</th><th>{label("Активность", "Белсенділік")}</th><th>{label("Ходы", "Кезектер")}</th><th>{label("Ошибки", "Қателер")}</th><th>{label("Модель / токены", "Модель / токендер")}</th><th><span className="sr-only">{t("details")}</span></th></tr></thead><tbody>{rows.map(row => <tr key={row.session_id}>
        <td><button className="journal-call" onClick={() => open(row.session_id)}>{row.last_transcript || label("Сессия звонка", "Қоңырау сеансы")}</button><small className="mono journal-uuid">{row.session_id}</small><small>{row.scenarios.map(scenarioTitle).join(" · ") || label("Маршрут ещё не выбран", "Бағыт әлі таңдалмаған")}</small></td>
        <td>{formatTraceDate(row.last_seen, locale)}<small>{label("Интервал событий", "Оқиғалар аралығы")}: {formatTraceMs(row.duration_ms, locale)}</small></td><td>{row.turns}<small>{row.traces} {label("трасс", "трасса")}</small></td><td><span className={`badge ${row.errors ? "amber" : "green"}`}>{row.errors}</span></td><td>{row.llm_calls} {label("вызовов", "шақыру")}<small>{row.tokens.total.toLocaleString(locale)} {label("токенов", "токен")}</small><small>{Object.keys(row.tokens_by_model).join(", ") || "—"}</small></td><td><button className="icon-button" aria-label={`${t("details")} ${row.session_id}`} onClick={() => open(row.session_id)}><Icon name="arrow" size={16} /></button></td>
      </tr>)}</tbody></table></div>}
      <div className="pagination"><span>{rows.length ? `${page * size + 1}–${page * size + rows.length}` : "0"} {label("звонков", "қоңырау")}</span><label className="page-size">{t("pageSize")}<select value={size} onChange={event => { setSize(Number(event.target.value)); setPage(0); }} disabled={loading}>{[10, 25, 50].map(value => <option key={value}>{value}</option>)}</select></label><nav aria-label={label("Страницы журнала", "Журнал беттері")}><button disabled={page === 0 || loading} onClick={() => setPage(v => v - 1)}>{t("prev")}</button><span className="journal-page">{page + 1}</span><button disabled={!hasNext || loading} onClick={() => setPage(v => v + 1)}>{t("next")}</button></nav></div>
    </section><p className="catalog-disclaimer">{label("Список упорядочен по последней активности. Незавершённые этапы появятся после их сохранения; обновите журнал, чтобы увидеть новые события.", "Тізім соңғы белсенділік бойынша реттелген. Аяқталмаған кезеңдер сақталғаннан кейін пайда болады; жаңа оқиғалар үшін журналды жаңартыңыз.")}</p>
    {selected && <Modal title={label("Трейс звонка", "Қоңырау трейсі")} onClose={() => router.push("/history", { scroll: false })} wide drawer><SessionTracePanel key={selected} sessionId={selected} traceId={selectedTrace} live={active} /></Modal>}
  </>;
}