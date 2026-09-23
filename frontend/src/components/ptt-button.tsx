"use client";
import { useEffect, useRef } from "react";
import { Icon } from "./icon";

type Recorder = { phase: "idle" | "requesting" | "recording" | "ready"; level: number; start: () => unknown; stop: () => void };

// Push-to-talk: удержание (мышь, палец, пробел) пишет реплику, отпускание — сразу отправляет.
// Короткий тап (< 250 мс) работает как переключатель: тап — старт, ещё тап — стоп.
export function PushToTalk({ recorder, disabled, label, hint }: { recorder: Recorder; disabled?: boolean; label: string; hint: string }) {
  const pressedAt = useRef(0); const latched = useRef(false); const releasePending = useRef(false);
  const active = recorder.phase === "recording" || recorder.phase === "requesting";

  const press = (at: number) => {
    if (disabled) return;
    if (active) { if (latched.current) { latched.current = false; release(at, true); } return; }
    pressedAt.current = at; latched.current = false; releasePending.current = false;
    void recorder.start();
  };
  const release = (at: number, force = false) => {
    if (!force && !active) return;
    if (!force && at - pressedAt.current < 250) { latched.current = true; return; }
    if (recorder.phase === "requesting") { releasePending.current = true; return; }
    recorder.stop();
  };
  useEffect(() => {
    if (recorder.phase === "recording" && releasePending.current) { releasePending.current = false; recorder.stop(); }
  }, [recorder, recorder.phase]);
  useEffect(() => {
    const typing = (e: KeyboardEvent) => e.target instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
    const down = (e: KeyboardEvent) => { if (e.code !== "Space" || e.repeat || typing(e)) return; e.preventDefault(); press(e.timeStamp); };
    const up = (e: KeyboardEvent) => { if (e.code !== "Space" || typing(e)) return; e.preventDefault(); if (!latched.current) release(e.timeStamp); };
    window.addEventListener("keydown", down); window.addEventListener("keyup", up);
    return () => { window.removeEventListener("keydown", down); window.removeEventListener("keyup", up); };
  });

  const recording = recorder.phase === "recording";
  return (
    <div className={`ptt ${recording ? "ptt-live" : ""}`}>
      <button type="button" className="ptt-button" disabled={disabled} aria-pressed={recording} aria-label={label}
        style={{ ["--level" as string]: String(recording ? Math.min(1, recorder.level * 1.6) : 0) }}
        onPointerDown={e => { e.currentTarget.setPointerCapture(e.pointerId); press(e.timeStamp); }}
        onPointerUp={e => { if (!latched.current) release(e.timeStamp); }}
        onPointerCancel={e => release(e.timeStamp, true)}
        onContextMenu={e => e.preventDefault()}>
        <span className="ptt-ring" /><span className="ptt-core"><Icon name={recording ? "stop" : "mic"} size={24} /></span>
      </button>
      <div className="ptt-label"><strong>{label}</strong><small>{hint}</small></div>
    </div>
  );
}
