"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import type { TranslationKey } from "@/lib/i18n";

export function useRecorder() {
  const [phase, setPhase] = useState<"idle" | "requesting" | "recording" | "ready">("idle");
  const [error, setError] = useState<TranslationKey | null>(null);
  const [blob, setBlob] = useState<Blob | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [seconds, setSeconds] = useState(0);
  const [level, setLevel] = useState(0);
  const [endedAt, setEndedAt] = useState<number | null>(null);
  const state = useRef<{ epoch: number; busy: boolean; stream?: MediaStream; recorder?: MediaRecorder; context?: AudioContext; timer?: ReturnType<typeof setInterval>; url?: string }>({ epoch: 0, busy: false });
  const release = useCallback(() => {
    const s = state.current;
    if (s.timer) clearInterval(s.timer);
    s.stream?.getTracks().forEach(track => track.stop());
    s.stream = undefined;
    if (s.context) void s.context.close().catch(() => {});
    s.context = undefined;
    s.timer = undefined;
  }, []);
  const discard = useCallback(() => {
    const s = state.current;
    s.epoch++; s.busy = false;
    if (s.recorder && s.recorder.state !== "inactive") s.recorder.stop();
    s.recorder = undefined;
    release();
    if (s.url) URL.revokeObjectURL(s.url);
    s.url = undefined;
    setBlob(null); setUrl(null); setPhase("idle"); setSeconds(0); setLevel(0); setError(null); setEndedAt(null);
  }, [release]);
  useEffect(() => () => {
    const s = state.current; s.epoch++;
    if (s.recorder && s.recorder.state !== "inactive") s.recorder.stop();
    release(); if (s.url) URL.revokeObjectURL(s.url);
  }, [release]);
  const stop = useCallback(() => {
    const r = state.current.recorder;
    if (r?.state === "recording") { setEndedAt(performance.now()); r.stop(); }
  }, []);
  const start = useCallback(async () => {
    if (state.current.busy) return;
    discard();
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) { setError("secure"); return; }
    if (typeof MediaRecorder === "undefined") { setError("micError"); return; }
    const s = state.current; const epoch = ++s.epoch; s.busy = true;
    setPhase("requesting");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (s.epoch !== epoch) { stream.getTracks().forEach(t => t.stop()); return; }
      s.stream = stream;
      const mimeType = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"].find(x => MediaRecorder.isTypeSupported(x));
      if (!mimeType) { release(); s.busy = false; setPhase("idle"); setError("audioFormat"); return; }
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      s.recorder = recorder;
      const chunks: BlobPart[] = [];
      recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
      recorder.onstop = () => {
        if (s.epoch !== epoch) return;
        release(); s.busy = false; setLevel(0);
        const audio = new Blob(chunks, { type: recorder.mimeType });
        if (!audio.size) { setError("noSpeech"); setPhase("idle"); return; }
        s.url = URL.createObjectURL(audio); setUrl(s.url); setBlob(audio); setPhase("ready");
      };
      recorder.onerror = () => { if (s.epoch === epoch) { discard(); setError("micError"); } };
      // Meter is driven by actual microphone samples, not decorative random values.
      const context = new AudioContext(); s.context = context;
      const analyser = context.createAnalyser(); analyser.fftSize = 256;
      context.createMediaStreamSource(stream).connect(analyser);
      const samples = new Uint8Array(analyser.fftSize);
      const started = performance.now();
      recorder.start(); setPhase("recording");
      s.timer = setInterval(() => {
        if (s.epoch !== epoch) return;
        const elapsed = (performance.now() - started) / 1000;
        setSeconds(Math.floor(elapsed));
        analyser.getByteTimeDomainData(samples);
        setLevel(Math.min(1, Math.sqrt(samples.reduce((sum, x) => sum + ((x - 128) / 128) ** 2, 0) / samples.length) * 5));
        if (elapsed >= 60) stop();
      }, 100);
    } catch (cause) {
      if (s.epoch !== epoch) return;
      release(); s.busy = false; setPhase("idle");
      const name = cause instanceof DOMException ? cause.name : "";
      setError(name === "NotAllowedError" ? "denied" : name === "NotFoundError" ? "noDevice" : "micError");
    }
  }, [discard, release, stop]);
  return { phase, error, blob, url, seconds, level, endedAt, start, stop, discard };
}
