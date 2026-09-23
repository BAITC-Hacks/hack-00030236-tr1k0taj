"use client";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useRecorder } from "@/hooks/use-recorder";
import { usePreferences } from "@/hooks/use-preferences";
import { useAudioQueue } from "@/hooks/use-audio-queue";
import { ApiError, voiceAdapter } from "@/lib/api";
import { applyContext, applyEvent } from "@/lib/turn-state";
import { makeExample } from "@/lib/examples";
import { translate, type TranslationKey } from "@/lib/i18n";
import type { Capabilities } from "@/lib/call-contract";
import type { Conversation, Turn, VoiceAdapter } from "@/lib/types";

function useVoiceState(adapter: VoiceAdapter) {
  const { locale, setLocale, sound, setSound, volume, setVolume, reduced, setReduced } = usePreferences();
  const [session, setSession] = useState<Conversation | null>(null);
  const sessionRef = useRef<Conversation | null>(null);
  const [history, setHistory] = useState<Conversation[]>([]);
  const [selectedTurn, setSelectedTurn] = useState<number | null>(null);
  const [draft, setDraft] = useState(""); const [textOpen, setTextOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<TranslationKey | null>(null);
  const [toast, setToast] = useState<TranslationKey | null>(null);
  const [health, setHealth] = useState<"loading" | "ok" | "error">("loading");
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const recorder = useRecorder(); const playback = useAudioQueue(sound, volume / 100);
  const request = useRef<{ token: number; busy: boolean; controller?: AbortController; sessionId?: string; turnId?: number }>({ token: 0, busy: false });
  const publish = useCallback((next: Conversation | null) => { sessionRef.current = next; setSession(next); }, []);
  useEffect(() => {
    document.documentElement.lang = locale; document.documentElement.dataset.reducedMotion = String(reduced);
  }, [locale, reduced]);
  useEffect(() => {
    const controller = new AbortController();
    async function refresh() {
      try {
        const caps = await adapter.capabilities(AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]));
        if (!controller.signal.aborted) { setCapabilities(caps); setHealth("ok"); }
      } catch { if (!controller.signal.aborted) setHealth("error"); }
    }
    void refresh(); const timer = setInterval(() => void refresh(), 15000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [adapter]);
  useEffect(() => { if (!toast) return; const timeout = setTimeout(() => setToast(null), 4000); return () => clearTimeout(timeout); }, [toast]);
  useEffect(() => () => { request.current.token++; request.current.controller?.abort(); }, []);
  const sessionId = session?.id; const generation = session?.generation;
  const isLive = session?.mode === "live" && !session.endedAt;
  useEffect(() => {
    if (!isLive || !sessionId) return;
    const controller = new AbortController(); let polling = false;
    const timer = setInterval(async () => {
      if (polling || request.current.busy) return;
      polling = true; const token = request.current.token;
      try {
        const context = await adapter.context(sessionId, controller.signal);
        const current = sessionRef.current;
        if (controller.signal.aborted || token !== request.current.token || request.current.busy || !current || current.id !== sessionId || current.generation !== generation) return;
        if (context.context_version > (current.context?.context_version ?? -1)) publish(applyContext(current, context));
      } catch { /* A background refresh must not interrupt foreground input. */ }
      finally { polling = false; }
    }, 2000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [adapter, sessionId, generation, isLive, publish]);
  const t = (key: TranslationKey) => translate(key, locale);
  function archive(current: Conversation, outcome: Conversation["outcome"] = "completed") {
    const ended = { ...current, endedAt: new Date().toISOString(), outcome: current.outcome ?? (current.turns.at(-1)?.status === "error" ? "error" : outcome) };
    if (ended.mode === "live" && ended.turns.length) setHistory(old => [ended, ...old.filter(s => s.id !== ended.id)]);
    return ended;
  }
  function stop() {
    const pending = request.current;
    pending.token++; pending.controller?.abort(); pending.busy = false;
    const cancelToken = pending.token;
    playback.player.stop(); setBusy(false);
    if (pending.sessionId && pending.turnId !== undefined) {
      void adapter.cancel(pending.sessionId, pending.turnId).catch(() => { if (request.current.token === cancelToken) setNotice("cancelFailed"); });
    }
    pending.turnId = undefined;
    const current = sessionRef.current;
    if (current) publish({ ...current, turns: current.turns.map(turn => turn.status === "processing" ? { ...turn, status: "cancelled" } : turn) });
  }
  function reset() {
    const interrupted = request.current.busy;
    stop(); recorder.discard();
    const current = sessionRef.current;
    if (current && !current.endedAt) archive(current, interrupted ? "interrupted" : "completed");
    publish(null); setSelectedTurn(null); setDraft(""); setNotice(null);
  }
  function loadExample(kind: "claim" | "payment") {
    reset(); const example = makeExample(kind); publish(example); setSelectedTurn(example.turns.at(-1)?.id ?? null);
  }
  async function send(value: string | Blob) {
    if (request.current.busy || (typeof value === "string" && !value.trim())) return false;
    if (!adapter.available) { setNotice("notConnected"); return false; }
    if (typeof value !== "string") {
      if (value.size > 10 * 1024 * 1024) { setNotice("audioTooLarge"); return false; }
      if (capabilities && !capabilities.audio_input.includes(value.type.split(";")[0])) { setNotice("audioFormat"); return false; }
    }
    request.current.controller?.abort();
    const controller = new AbortController(); const token = ++request.current.token;
    request.current.controller = controller; request.current.busy = true; request.current.turnId = undefined;
    setBusy(true); setNotice(null);
    const eos = typeof value === "string" ? null : recorder.endedAt;
    let firstText: number | undefined;
    let current = sessionRef.current;
    let turn: Turn | undefined;
    const valid = () => token === request.current.token && !controller.signal.aborted;
    const audioToken = playback.player.begin(() => {
      if (!valid() || eos === null || !current || !turn || turn.id < 0) return;
      const elapsed = Math.max(0, Math.round(performance.now() - eos));
      turn = { ...turn, timings: { ...turn.timings, browser_eos_to_playback: elapsed } };
      const timing = { eos_to_playback_ms: elapsed, eos_to_reply_text_ms: firstText };
      const visible = sessionRef.current;
      if (visible?.id === current.id) publish({ ...visible, turns: visible.turns.map(t => t.id === turn!.id ? { ...t, timings: { ...t.timings, browser_eos_to_playback: elapsed } } : t) });
      void adapter.playback(current.id, turn.id, timing).catch(() => { if (valid()) setToast("timingFailed"); });
    });
    try {
      if (!current || current.mode !== "live" || current.endedAt) {
        const id = crypto.randomUUID();
        const started = await adapter.create(id, controller.signal);
        if (!valid()) return false;
        if (started.session_id !== id) throw new Error("Session mismatch");
        setCapabilities(started.capabilities); setHealth("ok");
        current = applyContext({ id, generation: started.context.generation, startedAt: new Date().toISOString(), mode: "live", turns: [], facts: [], pendingTopics: [], helper: ["mock", "off"].includes(started.capabilities.providers.background) ? "disabled" : "waiting" }, started.context);
      }
      const base = current;
      request.current.sessionId = base.id;
      turn = { id: -Date.now(), text: typeof value === "string" ? value : "", answer: "", language: "—", channel: typeof value === "string" ? "text" : "voice", scenarios: [], alternatives: [], slots: {}, facts: [], timings: {}, status: "processing" };
      const commitTurn = () => {
        if (!valid() || !turn) return;
        current = { ...current!, turns: [...base.turns, turn] };
        publish(current); setSelectedTurn(turn.id);
      };
      commitTurn();
      await adapter.stream(base.id, value, event => {
        if (!valid() || !turn) return;
        if (event.type === "turn.started") {
          if (event.session_id !== base.id || event.generation !== base.generation) throw new Error("Stale session generation");
          request.current.turnId = event.turn_id;
        } else if (request.current.turnId !== undefined && event.turn_id !== request.current.turnId) return;
        if (event.type === "audio") { playback.player.enqueue(audioToken, event); return; }
        if (event.type === "reply.delta" && event.text && firstText === undefined && eos !== null) firstText = Math.max(0, Math.round(performance.now() - eos));
        turn = applyEvent(turn, event);
        if (event.type === "facts") current = { ...current!, facts: turn.facts };
        if (event.type === "action" && event.ok && event.mode === "handoff") current = { ...current!, outcome: "handoff" };
        commitTurn();
      }, controller.signal);
      if (!valid()) return false;
      const results = await Promise.allSettled([
        adapter.context(base.id, controller.signal), adapter.debug(base.id, controller.signal), adapter.board(base.id, controller.signal),
      ]);
      if (!valid()) return false;
      const [context, debug, board] = results;
      if (context.status === "fulfilled") current = applyContext(current!, context.value);
      if (turn.decision && debug.status === "fulfilled" && debug.value.turn_id === turn.id && debug.value.result) turn = { ...turn, prompt: debug.value.result.prompt ?? undefined, raw: debug.value.result };
      if (board.status === "fulfilled") current = { ...current!, board: board.value.filter(entry => entry.session_id === base.id && entry.generation === base.generation) };
      commitTurn();
      if (turn.status !== "error") { setDraft(""); recorder.discard(); }
      return true;
    } catch (cause) {
      if (valid()) {
        playback.player.stop();
        setNotice(cause instanceof ApiError && cause.status === 409 ? "turnBusy" : "sendFailed");
        if (current && turn) publish({ ...current, turns: [...current.turns.slice(0, -1), { ...turn, status: "error" }] });
      }
      return false;
    } finally { if (valid()) { request.current.busy = false; setBusy(false); } }
  }
  function end() {
    const current = sessionRef.current; if (!current || current.endedAt) return;
    const interrupted = request.current.busy;
    stop(); recorder.discard(); publish(archive(sessionRef.current!, interrupted ? "interrupted" : "completed"));
  }
  function sampleHistory() {
    setHistory(old => [...old.filter(s => s.mode === "live"), ...Array.from({ length: 28 }, (_, index) => {
      const example = makeExample(index % 3 === 0 ? "payment" : "claim");
      const started = new Date(Date.UTC(2026, 9, 1, 7, 0) - index * 3_600_000);
      return { ...example, id: `demo-session-${String(index + 1).padStart(3, "0")}`, startedAt: started.toISOString(), endedAt: new Date(started.getTime() + (60 + index * 7) * 1000).toISOString() };
    })]);
  }
  async function copy(text: string) { try { await navigator.clipboard.writeText(text); setToast("copied"); } catch { setToast("copyError"); } }
  return { locale, setLocale, t, sound, setSound, volume, setVolume, reduced, setReduced, session, selectedTurn, setSelectedTurn, history, sampleHistory, draft, setDraft, textOpen, setTextOpen, busy, notice, setNotice, toast, setToast, health, capabilities, playback, recorder, available: adapter.available, reset, loadExample, send, end, stop, copy };
}

type VoiceState = ReturnType<typeof useVoiceState>;
const VoiceContext = createContext<VoiceState | null>(null);
export function VoiceProvider({ children, adapter = voiceAdapter }: { children: ReactNode; adapter?: VoiceAdapter }) { return <VoiceContext.Provider value={useVoiceState(adapter)}>{children}</VoiceContext.Provider>; }
export function useVoice() { const state = useContext(VoiceContext); if (!state) throw new Error("VoiceProvider missing"); return state; }
