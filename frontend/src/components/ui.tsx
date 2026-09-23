"use client";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Icon } from "./icon";

export function Modal({ title, children, onClose, wide = false, drawer = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean; drawer?: boolean }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => { const element = dialog.current; element?.showModal(); return () => element?.close(); }, []);
  return <dialog ref={dialog} className={`modal ${wide ? "modal-wide" : ""} ${drawer ? "modal-drawer" : ""}`} onCancel={e => { e.preventDefault(); onClose(); }} onClick={e => { if (e.target === e.currentTarget) onClose(); }} aria-labelledby={titleId}>
    <div className="modal-content"><header><h2 id={titleId}>{title}</h2><button className="icon-button" aria-label="Close / Закрыть" onClick={onClose}><Icon name="close" /></button></header><div className="modal-body">{children}</div></div>
  </dialog>;
}
export function Dropdown({ label, children, icon = "down" }: { label: string; children: ReactNode; icon?: string }) {
  const [open, setOpen] = useState(false); const root = useRef<HTMLDivElement>(null); const trigger = useRef<HTMLButtonElement>(null); const menu = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    const outside = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside); return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  function close() { setOpen(false); trigger.current?.focus(); }
  return <div className="dropdown" ref={root} onKeyDown={e => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
    if (open && ["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
      e.preventDefault(); const buttons = [...(menu.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
      const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const target = e.key === "Home" ? 0 : e.key === "End" ? buttons.length - 1 : (i + (e.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[target]?.focus();
    }
  }}>
    <button ref={trigger} className="button secondary" aria-expanded={open} aria-haspopup="menu" onClick={() => setOpen(!open)}>{label}<Icon name={icon} size={16} /></button>
    {open && <div ref={menu} role="menu" className="dropdown-menu" onClick={e => { if ((e.target as HTMLElement).closest("button")) close(); }}>{children}</div>}
  </div>;
}
export function EmptyState({ icon = "document", title, description, children }: { icon?: string; title: string; description?: string; children?: ReactNode }) {
  return <div className="empty-state"><span className="empty-icon"><Icon name={icon} size={26} /></span><h3>{title}</h3>{description && <p>{description}</p>}{children}</div>;
}
export function Pagination({ page, size, total, onPage, onSize, options = [10, 25, 50], t }: { page: number; size: number; total: number; onPage: (p: number) => void; onSize: (s: number) => void; options?: number[]; t: (key: "prev" | "next" | "of" | "pageSize") => string }) {
  const pages = Math.ceil(total / size); if (!total) return null;
  const numbers = Array.from({ length: pages }, (_, i) => i + 1).filter(n => n === 1 || n === pages || Math.abs(n - page) <= 1);
  return <footer className="pagination"><span>{(page - 1) * size + 1}–{Math.min(page * size, total)} {t("of")} {total}</span><label className="page-size">{t("pageSize")}<select value={size} onChange={e => onSize(Number(e.target.value))}>{options.map(n => <option key={n}>{n}</option>)}</select></label>{pages > 1 && <nav aria-label="Pagination"><button aria-label={t("prev")} className="icon-button" disabled={page === 1} onClick={() => onPage(page - 1)}><Icon name="chevron" style={{ transform: "rotate(180deg)" }} size={16} /></button>{numbers.map((n, i) => <span key={n} className="page-number">{i > 0 && n - numbers[i - 1] > 1 && <span className="ellipsis">…</span>}<button aria-current={page === n ? "page" : undefined} className={page === n ? "selected" : ""} onClick={() => onPage(n)}>{n}</button></span>)}<button aria-label={t("next")} className="icon-button" disabled={page === pages} onClick={() => onPage(page + 1)}><Icon name="chevron" size={16} /></button></nav>}</footer>;
}
