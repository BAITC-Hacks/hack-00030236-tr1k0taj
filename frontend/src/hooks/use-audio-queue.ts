"use client";
import { useEffect, useState, useSyncExternalStore } from "react";
import type { AudioEvent } from "@/lib/call-contract";

type State = "idle" | "playing" | "blocked" | "error";
class SentencePlayer {
  private listeners = new Set<() => void>();
  private state: State = "idle";
  private epoch = 0;
  private next = 0;
  private enabled = true;
  private volume = 1;
  private queue = new Map<number, AudioEvent>();
  private audio?: HTMLAudioElement;
  private url?: string;
  private played?: () => void;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  snapshot = () => this.state;
  private publish(state: State) { this.state = state; this.listeners.forEach(listener => listener()); }
  private release() {
    if (this.audio) { this.audio.onplaying = this.audio.onended = this.audio.onerror = null; this.audio.pause(); this.audio.removeAttribute("src"); this.audio.load(); }
    this.audio = undefined;
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = undefined;
  }
  stop = () => { this.epoch++; this.queue.clear(); this.release(); this.publish("idle"); };
  begin(played: () => void) { this.stop(); this.next = 0; this.played = played; return this.epoch; }
  configure(enabled: boolean, volume: number) {
    if (this.enabled && !enabled) this.stop();
    this.enabled = enabled; this.volume = volume;
    if (this.audio) this.audio.volume = volume;
  }
  enqueue(epoch: number, event: AudioEvent) {
    if (epoch !== this.epoch || !this.enabled || event.seq < this.next || this.queue.has(event.seq)) return;
    this.queue.set(event.seq, event); this.pump();
  }
  private pump() {
    if (this.audio || !this.enabled) return;
    const event = this.queue.get(this.next);
    if (!event) return;
    this.queue.delete(this.next++);
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
