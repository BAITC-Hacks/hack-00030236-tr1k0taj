"use client";
import { useEffect, useRef, useState } from "react";
import { Modal } from "./ui";
import { Icon } from "./icon";
import { useVoice } from "./voice-provider";

export type FilterGroup = { key: string; label: string; options: { value: string; label: string }[]; single?: boolean };
export type FilterValues = Record<string, string[]>;
export function ListFilters({ groups, value, onChange }: { groups: FilterGroup[]; value: FilterValues; onChange: (value: FilterValues) => void }) {
  const { t } = useVoice();
  const [draft, setDraft] = useState<FilterValues | null>(null);
  const [search, setSearch] = useState<Record<string, string>>({});
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const dismiss = (event: Event) => root.current?.querySelectorAll("details[open]").forEach(detail => { if (!detail.contains(event.target as Node)) detail.removeAttribute("open"); });
    document.addEventListener("pointerdown", dismiss); document.addEventListener("focusin", dismiss);
    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("focusin", dismiss); };
  }, []);
  const active = groups.filter(group => value[group.key]?.length).length;
  function controls(values: FilterValues, update: (values: FilterValues) => void, mobile = false) {
    return groups.map(group => <details className="filter-dropdown" key={group.key} open={mobile || undefined}>
      <summary>{group.label}{!!values[group.key]?.length && <span className="badge">{values[group.key].length}</span>}<Icon name="down" size={13} /></summary>
      <div className="filter-options">{group.options.length > 8 && <input className="filter-search" aria-label={`${t("search")} · ${group.label}`} placeholder={t("search")} value={search[group.key] ?? ""} onChange={e => setSearch({ ...search, [group.key]: e.target.value })} />}<button className="filter-clear" onClick={() => update({ ...values, [group.key]: [] })}>{t("all")}</button>{group.options.filter(option => !search[group.key] || option.label.toLocaleLowerCase().includes(search[group.key].toLocaleLowerCase())).map(option => <label key={option.value}><input type="checkbox" checked={values[group.key]?.includes(option.value) ?? false} onChange={e => update({ ...values, [group.key]: e.target.checked ? group.single ? [option.value] : [...(values[group.key] ?? []), option.value] : (values[group.key] ?? []).filter(item => item !== option.value) })} />{option.label}</label>)}</div>
    </details>);
  }
  return <><div ref={root} className="desktop-filter-groups" onKeyDown={e => { if (e.key === "Escape") (e.target as HTMLElement).closest("details")?.removeAttribute("open"); }}>{controls(value, onChange)}</div><button className="button secondary mobile-filter-button" onClick={() => setDraft(structuredClone(value))}><Icon name="filter" size={16} />{t("filters")} {active || ""}</button><div className="filter-chips">{groups.flatMap(group => (value[group.key] ?? []).map(item => <button key={`${group.key}-${item}`} onClick={() => onChange({ ...value, [group.key]: value[group.key].filter(v => v !== item) })}>{group.label}: {group.options.find(option => option.value === item)?.label ?? item}<Icon name="close" size={12} /></button>))}</div>{draft && <Modal title={t("filters")} onClose={() => setDraft(null)}><div className="mobile-filter-groups">{controls(draft, setDraft, true)}</div><div className="dialog-actions"><button className="button secondary" onClick={() => setDraft({})}>{t("reset")}</button><button className="button primary" onClick={() => { onChange(draft); setDraft(null); }}>{t("applyFilters")}</button></div></Modal>}</>;
}

