"use client";
import { useCallback, useRef, useState } from "react";

export type RealtimeSttResult = { text: string; language?: string } | null;
export type SttSecret = { client_secret: string; expires_at: number; model: string };
type FetchSession = (languageHint: "ru" | "kk" | undefined, signal: AbortSignal) => Promise<SttSecret>;

// Streaming STT via OpenAI Realtime over WebRTC, straight from the browser (docs/specs/speech-module.md):
// audio never touches our backend, only the ephemeral client_secret does. Best-effort — any failure
// (mock mode, no key, WebRTC blocked) resolves to null so the caller falls back to upload+STT.
// TODO(hack): opens its own mic stream, separate from useRecorder's; acceptable for the hackathon.
export function useRealtimeStt(fetchSession: FetchSession) {
  const [partial, setPartial] = useState("");
  const state = useRef<{
    pc?: RTCPeerConnection; dc?: RTCDataChannel; stream?: MediaStream;
    text: string; resolve?: (r: RealtimeSttResult) => void; active: boolean;
  }>({ text: "", active: false });

  const cleanup = useCallback(() => {
    const s = state.current;
    try { s.dc?.close(); } catch { /* already closed */ }
    try { s.pc?.close(); } catch { /* already closed */ }
    s.stream?.getTracks().forEach(track => track.stop());
    s.dc = undefined; s.pc = undefined; s.stream = undefined; s.active = false; s.resolve = undefined;
  }, []);

  const start = useCallback(async (languageHint?: "ru" | "kk") => {
    cleanup();
    state.current.text = ""; setPartial("");
    try {
      const [secret, stream] = await Promise.all([
        fetchSession(languageHint, AbortSignal.timeout(5000)),
        navigator.mediaDevices.getUserMedia({ audio: true }),
      ]);
      const pc = new RTCPeerConnection();
      pc.addTrack(stream.getAudioTracks()[0], stream);
      const dc = pc.createDataChannel("oai-events");
      dc.onmessage = event => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "conversation.item.input_audio_transcription.delta" && typeof msg.delta === "string") {
            state.current.text += msg.delta; setPartial(state.current.text);
          } else if (msg.type === "conversation.item.input_audio_transcription.completed" && typeof msg.transcript === "string") {
            state.current.text = msg.transcript; setPartial(state.current.text);
            state.current.resolve?.(state.current.text.trim() ? { text: state.current.text.trim(), language: languageHint } : null);
            state.current.resolve = undefined;
          }
        } catch { /* malformed realtime event, ignore */ }
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const response = await fetch("https://api.openai.com/v1/realtime/calls", {
        method: "POST", body: offer.sdp, signal: AbortSignal.timeout(8000),
        headers: { Authorization: `Bearer ${secret.client_secret}`, "Content-Type": "application/sdp" },
      });
      if (!response.ok) throw new Error("realtime connect failed");
      await pc.setRemoteDescription({ type: "answer", sdp: await response.text() });
      state.current.pc = pc; state.current.dc = dc; state.current.stream = stream; state.current.active = true;
      return true;
    } catch {
      cleanup();
      return false;
    }
  }, [cleanup, fetchSession]);

  const stop = useCallback((): Promise<RealtimeSttResult> => {
    const s = state.current;
    if (!s.active || !s.dc || s.dc.readyState !== "open") { const text = s.text.trim(); cleanup(); return Promise.resolve(text ? { text } : null); }
    return new Promise(resolve => {
      const timer = setTimeout(() => { const text = s.text.trim(); cleanup(); resolve(text ? { text } : null); }, 4000);
      s.resolve = result => { clearTimeout(timer); cleanup(); resolve(result); };
      try { s.dc!.send(JSON.stringify({ type: "input_audio_buffer.commit" })); }
      catch { clearTimeout(timer); cleanup(); resolve(null); }
    });
  }, [cleanup]);

  const discard = useCallback(() => cleanup(), [cleanup]);

  return { partial, start, stop, discard };
}
