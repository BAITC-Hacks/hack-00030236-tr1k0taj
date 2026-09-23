"use client";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useRecorder } from "@/hooks/use-recorder";
import { usePreferences } from "@/hooks/use-preferences";
import { readHealth, voiceAdapter } from "@/lib/api";
import { makeExample } from "@/lib/examples";
import { translate, type TranslationKey } from "@/lib/i18n";
import type { Conversation, VoiceAdapter } from "@/lib/types";

function useVoiceState(adapter: VoiceAdapter) {
  const { locale, setLocale, sound, setSound, volume, setVolume, reduced, setReduced } = usePreferences();
  const [session, setSession] = useState<Conversation | null>(null); const [history, setHistory] = useState<Conversation[]>([]);
  const [selectedTurn, setSelectedTurn] = useState<number | null>(null); const [draft, setDraft] = useState(""); const [textOpen, setTextOpen] = useState(false);
  const [busy, setBusy] = useState(false); const [notice, setNotice] = useState<TranslationKey | null>(null); const [toast, setToast] = useState<TranslationKey | null>(null);
  const [health, setHealth] = useState<"loading" | "ok" | "error">("loading"); const recorder = useRecorder();
  const request = useRef<{ token: number; controller?: AbortController }>({ token: 0 });
  useEffect(() => {
    document.documentElement.lang = locale; document.documentElement.dataset.reducedMotion = String(reduced);
  }, [locale, reduced]);
  useEffect(() => { const controller = new AbortController();
    readHealth(controller.signal).then(() => setHealth("ok")).catch(() => { if (!controller.signal.aborted) setHealth("error"); });
    return () => controller.abort();
  }, []);
  useEffect(() => { if (!toast) return; const timeout = setTimeout(() => setToast(null), 4000); return () => clearTimeout(timeout); }, [toast]);
  useEffect(() => () => { request.current.token++; request.current.controller?.abort(); }, []);
  const t = (key: TranslationKey) => translate(key, locale);
  function cancelLocal() { request.current.token++; request.current.controller?.abort(); setBusy(false); }
  async function stop() { cancelLocal(); if (session?.mode === "live" && adapter.available) try { await adapter.cancel(session); } catch { setNotice("sendFailed"); } }
  function reset() { cancelLocal(); recorder.discard(); setSession(null); setSelectedTurn(null); setDraft(""); setNotice(null); }
  function loadExample(kind: "claim" | "payment") { reset(); const example = makeExample(kind); setSession(example); setSelectedTurn(example.turns.at(-1)?.id ?? null); }
  async function send(value: string | Blob) {
    if (busy) return false;
    if (!adapter.available) { setNotice("notConnected"); return false; }
    setBusy(true); setNotice(null); const controller = new AbortController(); request.current.controller = controller; const token = ++request.current.token;
    try {
      const current = session?.mode === "live" && !session.endedAt ? session : await adapter.create(controller.signal);
      if (token !== request.current.token) return false;
      const next = typeof value === "string" ? await adapter.text(current, value, controller.signal) : await adapter.audio(current, value, controller.signal);
      if (token !== request.current.token || next.id !== current.id || next.generation !== current.generation) return false;
      setSession(next); setSelectedTurn(next.turns.at(-1)?.id ?? null); setDraft(""); recorder.discard(); return true;
    } catch { if (token === request.current.token) setNotice("sendFailed"); return false; }
    finally { if (token === request.current.token) setBusy(false); }
  }
  async function end() {
    if (!session || session.endedAt) return;
    cancelLocal(); recorder.discard();
    try {
      if (adapter.available) await adapter.end(session);
      const ended = { ...session, endedAt: new Date().toISOString(), outcome: "completed" as const };
      setSession(ended); setHistory(old => [ended, ...old.filter(s => s.id !== ended.id)]);
    } catch { setNotice("sendFailed"); }
  }
  function sampleHistory() {
    setHistory(Array.from({ length: 28 }, (_, index) => {
      const example = makeExample(index % 3 === 0 ? "payment" : "claim");
      const started = new Date(Date.UTC(2026, 9, 1, 7, 0) - index * 3_600_000);
      return { ...example, id: `demo-session-${String(index + 1).padStart(3, "0")}`, startedAt: started.toISOString(), endedAt: new Date(started.getTime() + (60 + index * 7) * 1000).toISOString() };
    }));
  }
  async function copy(text: string) { try { await navigator.clipboard.writeText(text); setToast("copied"); } catch { setToast("copyError"); } }
  return { locale, setLocale, t, sound, setSound, volume, setVolume, reduced, setReduced, session, selectedTurn, setSelectedTurn, history, sampleHistory, draft, setDraft, textOpen, setTextOpen, busy, notice, setNotice, toast, setToast, health, recorder, available: adapter.available, reset, loadExample, send, end, stop, copy };
}
type VoiceState = ReturnType<typeof useVoiceState>;
const VoiceContext = createContext<VoiceState | null>(null);
export function VoiceProvider({ children, adapter = voiceAdapter }: { children: ReactNode; adapter?: VoiceAdapter }) { return <VoiceContext.Provider value={useVoiceState(adapter)}>{children}</VoiceContext.Provider>; }
export function useVoice() { const state = useContext(VoiceContext); if (!state) throw new Error("VoiceProvider missing"); return state; }
