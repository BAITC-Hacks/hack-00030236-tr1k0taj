"use client";
import { useEffect, useState, useSyncExternalStore } from "react";
import type { AudioEvent } from "@/lib/call-contract";

type State = "idle" | "playing" | "blocked" | "error";

const PCM_RATE = 24000;

function pcmToFloat32(bytes: Uint8Array): Float32Array {
  const n = bytes.length >> 1;
  const out = new Float32Array(n);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let i = 0; i < n; i++) out[i] = view.getInt16(i * 2, true) / 32768;
  return out;
}

class SentencePlayer {
  private listeners = new Set<() => void>();
  private state: State = "idle";
  private epoch = 0;
  private next = 0;
  private enabled = true;
  private volume = 1;
  private queue = new Map<number, AudioEvent>();
  private played?: () => void;
  // audio/mpeg (whole-sentence, legacy) playback
  private audio?: HTMLAudioElement;
  private url?: string;
  // audio/pcm streaming playback, scheduled gaplessly on a Web Audio timeline
  private ctx?: AudioContext;
  private gain?: GainNode;
  private nextStartTime = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private pending = 0;

  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  snapshot = () => this.state;
  private publish(state: State) { this.state = state; this.listeners.forEach(listener => listener()); }

  private release() {
    if (this.audio) { this.audio.onplaying = this.audio.onended = this.audio.onerror = null; this.audio.pause(); this.audio.removeAttribute("src"); this.audio.load(); }
    this.audio = undefined;
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = undefined;
    for (const source of this.sources) { source.onended = null; try { source.stop(); } catch { /* already stopped */ } }
    this.sources.clear();
    this.pending = 0;
    this.nextStartTime = 0;
  }

  stop = () => { this.epoch++; this.queue.clear(); this.release(); this.publish("idle"); };

  begin(played: () => void) {
    this.stop();
    this.next = 0;
    this.played = played;
    if (!this.ctx) {
      try { this.ctx = new AudioContext({ sampleRate: PCM_RATE }); }
      catch { try { this.ctx = new AudioContext(); } catch { this.ctx = undefined; } }
      if (this.ctx) { this.gain = this.ctx.createGain(); this.gain.connect(this.ctx.destination); this.gain.gain.value = this.enabled ? this.volume : 0; }
    }
    if (this.ctx?.state === "suspended") void this.ctx.resume().catch(() => {});
    return this.epoch;
  }

  configure(enabled: boolean, volume: number) {
    if (this.enabled && !enabled) this.stop();
    this.enabled = enabled; this.volume = volume;
    if (this.audio) this.audio.volume = volume;
    if (this.gain) this.gain.gain.value = enabled ? volume : 0;
  }

  enqueue(epoch: number, event: AudioEvent) {
    if (epoch !== this.epoch || !this.enabled || event.seq < this.next || this.queue.has(event.seq)) return;
    this.queue.set(event.seq, event); this.pump();
  }

  private pump() {
    if (!this.enabled) return;
    for (;;) {
      const event = this.queue.get(this.next);
      if (!event) return;
      this.queue.delete(this.next++);
      if (event.mime.startsWith("audio/pcm")) { this.playPcm(event); continue; }
      this.playBlob(event);
      return; // audio/mpeg plays one element at a time; pump() resumes onended
    }
  }

  private playPcm(event: AudioEvent) {
    const epoch = this.epoch;
    try {
      if (!this.ctx || !this.gain) throw new Error("No AudioContext");
      const bytes = Uint8Array.from(atob(event.data), c => c.charCodeAt(0));
      if (bytes.length < 2) return;
      const samples = pcmToFloat32(bytes);
      const buffer = this.ctx.createBuffer(1, samples.length, PCM_RATE);
      buffer.getChannelData(0).set(samples);
      const source = this.ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(this.gain);
      const startAt = Math.max(this.ctx.currentTime, this.nextStartTime);
      this.nextStartTime = startAt + buffer.duration;
      this.sources.add(source);
      this.pending++;
      source.onended = () => {
        this.sources.delete(source);
        this.pending--;
        if (epoch !== this.epoch) return;
        if (this.pending === 0 && this.queue.size === 0) this.publish("idle");
      };
      source.start(startAt);
      if (this.state !== "playing") { this.publish("playing"); this.played?.(); this.played = undefined; }
    } catch { if (epoch === this.epoch) this.publish("error"); }
  }

  private playBlob(event: AudioEvent) {
    const epoch = this.epoch;
    try {
      if (!event.mime.startsWith("audio/")) throw new Error("Invalid audio MIME");
      const bytes = Uint8Array.from(atob(event.data), c => c.charCodeAt(0));
      this.url = URL.createObjectURL(new Blob([bytes], { type: event.mime }));
      const audio = new Audio(this.url); this.audio = audio; audio.volume = this.volume;
      audio.onplaying = () => { if (epoch !== this.epoch) return; this.publish("playing"); this.played?.(); this.played = undefined; };
      audio.onended = () => { if (epoch !== this.epoch) return; this.release(); this.publish("idle"); this.pump(); };
      audio.onerror = () => { if (epoch !== this.epoch) return; this.release(); this.publish("error"); this.pump(); };
      void audio.play().catch(() => { if (epoch === this.epoch) this.publish("blocked"); });
    } catch { this.publish("error"); }
  }

  resume = () => {
    const epoch = this.epoch;
    if (this.ctx) void this.ctx.resume().catch(() => { if (epoch === this.epoch) this.publish("blocked"); });
    void this.audio?.play().catch(() => { if (epoch === this.epoch) this.publish("blocked"); });
  };
}
export function useAudioQueue(enabled: boolean, volume: number) {
  const [player] = useState(() => new SentencePlayer());
  const status = useSyncExternalStore(player.subscribe, player.snapshot, () => "idle" as State);
  useEffect(() => { player.configure(enabled, volume); }, [enabled, volume, player]);
  useEffect(() => () => player.stop(), [player]);
  return { player, status };
}
