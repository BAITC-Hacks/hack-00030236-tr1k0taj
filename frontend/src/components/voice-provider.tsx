"use client";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useRecorder } from "@/hooks/use-recorder";
import { useRealtimeStt, type RealtimeSttResult } from "@/hooks/use-realtime-stt";
import { usePreferences } from "@/hooks/use-preferences";
import { useAudioQueue } from "@/hooks/use-audio-queue";
import { ApiError, voiceAdapter } from "@/lib/api";
import { applyContext, applyEvent } from "@/lib/turn-state";
import { translate, type TranslationKey } from "@/lib/i18n";
import type { Capabilities } from "@/lib/call-contract";
import type { Conversation, Turn, VoiceAdapter } from "@/lib/types";

function useVoiceState(adapter: VoiceAdapter) {
  const { locale, setLocale, sound, setSound, volume, setVolume, reduced, setReduced } = usePreferences();
  const [session, setSession] = useState<Conversation | null>(null);
  const sessionRef = useRef<Conversation | null>(null);
  const [selectedTurn, setSelectedTurn] = useState<number | null>(null);
  const [draft, setDraft] = useState(""); const [textOpen, setTextOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<TranslationKey | null>(null);
  const [toast, setToast] = useState<TranslationKey | null>(null);
  const [health, setHealth] = useState<"loading" | "ok" | "error">("loading");
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const baseRecorder = useRecorder(); const playback = useAudioQueue(sound, volume / 100);
  const sessionIdRef = useRef<string | undefined>(undefined);
  const sttPromise = useRef<Promise<RealtimeSttResult> | null>(null);
  const stt = useRealtimeStt((languageHint, signal) => {
    const sid = sessionIdRef.current;
    if (!sid || !adapter.sttSession) return Promise.reject(new Error("stt_session_unavailable"));
    return adapter.sttSession(sid, languageHint, signal);
  });
  const recorder = {
    ...baseRecorder,
    start: () => {
      sttPromise.current = null;
      if (sessionIdRef.current) void stt.start(locale);
      return baseRecorder.start();
    },
    stop: () => { sttPromise.current = stt.stop(); baseRecorder.stop(); },
    discard: () => { sttPromise.current = null; stt.discard(); baseRecorder.discard(); },
  };
  const request = useRef<{ token: number; busy: boolean; controller?: AbortController; sessionId?: string; turnId?: number; traceparent?: string }>({ token: 0, busy: false });
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
  useEffect(() => { sessionIdRef.current = sessionId; }, [sessionId]);
  const isLive = session?.mode === "live" && !session.endedAt;
  useEffect(() => {
    if (!isLive || !sessionId) return;
    const controller = new AbortController(); let polling = false;
    const timer = setInterval(async () => {
      if (document.hidden || polling || request.current.busy) return;
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
  function stop() {
    const pending = request.current;
    pending.token++; pending.controller?.abort(); pending.busy = false;
    const cancelToken = pending.token;
    playback.player.stop(); setBusy(false);
    if (pending.sessionId && pending.turnId !== undefined) {
      void adapter.cancel(pending.sessionId, pending.turnId, pending.traceparent).catch(() => { if (request.current.token === cancelToken) setNotice("cancelFailed"); });
    }
    pending.turnId = undefined;
    pending.traceparent = undefined;
    const current = sessionRef.current;
    if (current) publish({ ...current, turns: current.turns.map(turn => turn.status === "processing" ? { ...turn, status: "cancelled" } : turn) });
  }
  function reset() {
    stop(); recorder.discard();
    publish(null); setSelectedTurn(null); setDraft(""); setNotice(null);
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
    request.current.traceparent = undefined;
    setBusy(true); setNotice(null);
    const submittedDraft = typeof value === "string" && draft.trim() === value.trim() ? draft : null;
    const eos = typeof value === "string" ? null : recorder.endedAt;
    // Realtime STT (WebRTC, docs/specs/speech-module.md) already ran while recording; its final
    // transcript lets the ход start as text (source stt) instead of uploading and re-transcribing audio.
    let sttResult: RealtimeSttResult = null;
    if (typeof value !== "string" && sttPromise.current) {
      sttResult = await sttPromise.current.catch(() => null);
      sttPromise.current = null;
      if (controller.signal.aborted || token !== request.current.token) return false;
    }
    const effectiveValue: string | Blob = sttResult?.text ? sttResult.text : value;
    const sttSource = Boolean(sttResult?.text) && typeof value !== "string";
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
      void adapter.playback(current.id, turn.id, timing, turn.traceparent).catch(() => { if (valid()) setToast("timingFailed"); });
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
      turn = { id: -Date.now(), text: typeof effectiveValue === "string" ? effectiveValue : "", answer: "", language: "—", channel: typeof value === "string" ? "text" : "voice", scenarios: [], alternatives: [], slots: {}, facts: [], timings: {}, status: "processing" };
      const commitTurn = () => {
        if (!valid() || !turn) return;
        current = { ...current!, turns: [...base.turns, turn] };
        publish(current); setSelectedTurn(turn.id);
      };
      commitTurn();
      await adapter.stream(base.id, effectiveValue, event => {
        if (!valid() || !turn) return;
        if (event.type === "turn.started") {
          if (event.session_id !== base.id || event.generation !== base.generation) throw new Error("Stale session generation");
          request.current.turnId = event.turn_id;
          if (submittedDraft !== null) setDraft(currentDraft => currentDraft === submittedDraft ? "" : currentDraft);
        } else if (request.current.turnId !== undefined && event.turn_id !== request.current.turnId) return;
        if (event.type === "audio") { playback.player.enqueue(audioToken, event); return; }
        if (event.type === "reply.delta" && event.text && firstText === undefined && eos !== null) firstText = Math.max(0, Math.round(performance.now() - eos));
        turn = applyEvent(turn, event);
        if (event.type === "facts") current = { ...current!, facts: turn.facts };
        if (event.type === "action" && event.ok && event.mode === "handoff") current = { ...current!, outcome: "handoff" };
        commitTurn();
      }, controller.signal, metadata => {
        if (!valid() || !turn) return;
        request.current.traceparent = metadata.traceparent;
        turn = { ...turn, ...metadata }; commitTurn();
      }, sttSource ? { source: "stt" } : undefined);
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
      if (turn.status !== "error") recorder.discard();
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
    stop(); recorder.discard();
    const stopped = sessionRef.current!;
    const lastStatus = stopped.turns.at(-1)?.status;
    publish({ ...stopped, endedAt: new Date().toISOString(), outcome: stopped.outcome ?? (lastStatus === "cancelled" || interrupted ? "interrupted" : lastStatus === "error" ? "error" : "completed") });
  }
  async function copy(text: string) { try { await navigator.clipboard.writeText(text); setToast("copied"); } catch { setToast("copyError"); } }
  return { locale, setLocale, t, sound, setSound, volume, setVolume, reduced, setReduced, session, selectedTurn, setSelectedTurn, draft, setDraft, textOpen, setTextOpen, busy, notice, setNotice, toast, setToast, health, capabilities, playback, recorder, available: adapter.available, reset, send, end, stop, copy };
}

type VoiceState = ReturnType<typeof useVoiceState>;
const VoiceContext = createContext<VoiceState | null>(null);
export function VoiceProvider({ children, adapter = voiceAdapter }: { children: ReactNode; adapter?: VoiceAdapter }) { return <VoiceContext.Provider value={useVoiceState(adapter)}>{children}</VoiceContext.Provider>; }
export function useVoice() { const state = useContext(VoiceContext); if (!state) throw new Error("VoiceProvider missing"); return state; }
