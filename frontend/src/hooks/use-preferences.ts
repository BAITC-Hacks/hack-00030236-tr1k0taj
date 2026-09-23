"use client";
import { useSyncExternalStore } from "react";
import type { Locale } from "@/lib/types";
type Preferences = { locale: Locale; sound: boolean; volume: number; reduced: boolean };
const defaults: Preferences = { locale: "ru", sound: true, volume: 80, reduced: false };
const key = "voice-router.preferences";
let memory = "{}";
function read() { try { return localStorage.getItem(key) ?? memory; } catch { return memory; } }
function subscribe(notify: () => void) {
  window.addEventListener("voice-preferences", notify); window.addEventListener("storage", notify);
  return () => { window.removeEventListener("voice-preferences", notify); window.removeEventListener("storage", notify); };
}
export function usePreferences() {
  const raw = useSyncExternalStore(subscribe, read, () => "{}");
  let saved: Partial<Preferences> = {};
  try { saved = JSON.parse(raw) ?? {}; } catch { /* Recover from invalid optional preferences. */ }
  const prefs: Preferences = {
    locale: saved.locale === "kk" ? "kk" : "ru",
    sound: typeof saved.sound === "boolean" ? saved.sound : defaults.sound,
    volume: typeof saved.volume === "number" ? Math.max(0, Math.min(100, saved.volume)) : defaults.volume,
    reduced: saved.reduced === true,
  };
  function update(patch: Partial<Preferences>) {
    memory = JSON.stringify({ ...prefs, ...patch });
    try { localStorage.setItem(key, memory); } catch { /* Keep preferences in memory. */ }
    window.dispatchEvent(new Event("voice-preferences"));
  }
  return { ...prefs, setLocale: (locale: Locale) => update({ locale }), setSound: (sound: boolean) => update({ sound }), setVolume: (volume: number) => update({ volume }), setReduced: (reduced: boolean) => update({ reduced }) };
}
