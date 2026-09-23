"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";
import { useVoice, VoiceProvider } from "./voice-provider";
import { Icon } from "./icon";
import { Modal } from "./ui";
import { CatalogProvider, useCatalog } from "./catalog-provider";

function Shell({ children }: { children: ReactNode }) {
  const v = useVoice(); const { t } = v; const path = usePathname();
  const catalog = useCatalog();
  const [settings, setSettings] = useState(false); const [guide, setGuide] = useState(false); const [menu, setMenu] = useState(false);
  const links = [{ href: "/", icon: "call", key: "call" }, { href: "/history", icon: "history", key: "history" }, { href: "/scenarios", icon: "grid", key: "scenarios" }] as const;
  return <div className="app-shell">
    <a className="skip-link" href="#main">Skip to content / К содержимому</a>
    {menu && <button className="nav-backdrop" aria-label={t("close")} onClick={() => setMenu(false)} />}
    <aside className={`sidebar ${menu ? "sidebar-open" : ""}`}>
      <Link className="brand" href="/" onClick={() => setMenu(false)}><span className="brand-mark"><i /><i /><i /><i /></span><span>Saqta<span className="brand-voice"> Voice</span><small>INSURANCE INTELLIGENCE</small></span></Link>
      <div className="nav-label">VOICE WORKSPACE</div>
      <nav className="primary-nav" aria-label="Main navigation">{links.map(link => <Link key={link.href} href={link.href} aria-current={path === link.href ? "page" : undefined} className={path === link.href ? "active" : ""} onClick={() => setMenu(false)}><Icon name={link.icon} /><span>{t(link.key)}</span>{link.key === "scenarios" && catalog.status === "ready" && <small>{catalog.scenarios.length}</small>}{path === link.href && <span className="nav-dot" />}</Link>)}</nav>
      <div className="sidebar-bottom"><div className="sidebar-note"><span className="note-symbol"><Icon name="shield" size={21} /></span><strong>{t("explainable")}</strong><p>{t("explainText")}</p><button onClick={() => { setGuide(true); setMenu(false); }}>{t("guide")}<Icon name="arrow" size={16} /></button></div><button className="settings-button" onClick={() => { setSettings(true); setMenu(false); }}><Icon name="settings" /><span>{t("settings")}</span></button><div className="sidebar-footer"><span className="tiny-dot" />Saqta Insurance <span>v0.1</span></div></div>
    </aside>
    <div className="main-shell"><header className="topbar"><div className="breadcrumbs"><button className="icon-button mobile-menu" onClick={() => setMenu(true)} aria-label="Menu"><Icon name="menu" /></button><span>Saqta Insurance</span><Icon name="chevron" size={13} /><strong>{t(links.find(l => l.href === path)?.key ?? "call")}</strong></div><div className="topbar-right"><span className="environment-tag"><span className="tiny-dot" />{t("synthetic")}</span><label className="language-select"><Icon name="globe" size={17} /><span className="sr-only">{t("interfaceLanguage")}</span><select value={v.locale} onChange={e => v.setLocale(e.target.value as "ru" | "kk")}><option value="ru">RU</option><option value="kk">ҚАЗ</option></select></label><button className="icon-button" aria-label={t("guide")} onClick={() => setGuide(true)}><Icon name="info" size={19} /></button></div></header>
      {path !== "/" && (v.recorder.phase === "recording" || v.busy || (v.session && !v.session.endedAt)) && <div className="ongoing-banner"><span className="tiny-dot" /><Link href="/">{t("return")}</Link>{v.recorder.phase === "recording" ? <button onClick={v.recorder.stop}>{t("finishRecording")}</button> : v.busy && <button onClick={() => void v.stop()}>{t("stop")}</button>}</div>}
      <main id="main" className="page-content">{children}</main>
    </div>
    {settings && <Modal title={t("settings")} onClose={() => setSettings(false)}><div className="settings-form"><label>{t("interfaceLanguage")}<select value={v.locale} onChange={e => v.setLocale(e.target.value as "ru" | "kk")}><option value="ru">Русский</option><option value="kk">Қазақша</option></select></label><p className="muted">{t("languageHint")}</p><label className="switch-row">{t("sound")}<input type="checkbox" role="switch" checked={v.sound} onChange={e => v.setSound(e.target.checked)} /></label><label>{t("volume")} · {v.volume}%<input type="range" min="0" max="100" value={v.volume} onChange={e => v.setVolume(Number(e.target.value))} /></label><label className="switch-row">{t("reduceMotion")}<input type="checkbox" role="switch" checked={v.reduced} onChange={e => v.setReduced(e.target.checked)} /></label></div></Modal>}
    {guide && <Modal title={t("guide")} onClose={() => setGuide(false)}><div className="guide-steps">{["step1", "step2", "step3", "step4"].map((key, i) => <div key={key}><span>{i + 1}</span><strong>{t(key as "step1")}</strong></div>)}</div><p>{t("guideText")}</p><div className="notice"><Icon name="info" /><p>{t(v.capabilities?.mock_mode ? "mockMode" : v.health === "error" ? "apiError" : "apiOk")}</p></div></Modal>}
    {v.toast && <div className="toast" role="status"><Icon name={v.toast === "copyError" ? "info" : "check"} />{t(v.toast)}<button className="icon-button" onClick={() => v.setToast(null)} aria-label={t("close")}><Icon name="close" size={16} /></button></div>}
  </div>;
}
export function AppShell({ children }: { children: ReactNode }) { return <CatalogProvider><VoiceProvider><Shell>{children}</Shell></VoiceProvider></CatalogProvider>; }
