"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { TraceApiError } from "@/lib/trace-api";

type Resource<T> = { key: string | null; status: "loading" | "ready" | "missing" | "error"; data?: T; refreshing: boolean; updatedAt?: string };

export function useTraceResource<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>, live: boolean) {
  const [state, setState] = useState<Resource<T>>({ key, status: "loading", refreshing: false });
  const active = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    if (!key || active.current) return;
    const controller = new AbortController(); active.current = controller;
    setState(previous => previous.key === key ? { ...previous, refreshing: true } : { key, status: "loading", refreshing: true });
    try {
      const data = await load(controller.signal);
      if (!controller.signal.aborted && active.current === controller) setState({ key, data, status: "ready", refreshing: false, updatedAt: new Date().toISOString() });
    } catch (error) {
      if (!controller.signal.aborted && active.current === controller) {
        const missing = error instanceof TraceApiError && error.status === 404;
        setState(previous => ({ key, status: missing ? "missing" : "error", refreshing: false, data: !missing && previous.key === key ? previous.data : undefined, updatedAt: previous.key === key ? previous.updatedAt : undefined }));
      }
    } finally { if (active.current === controller) active.current = null; }
  }, [key, load]);
  useEffect(() => {
    let disposed = false;
    const refreshVisible = () => { if (!disposed && document.visibilityState === "visible") void refresh(); };
    queueMicrotask(() => { if (!disposed && (!live || document.visibilityState === "visible")) void refresh(); });
    const interval = live ? setInterval(refreshVisible, 3000) : undefined;
    if (live) document.addEventListener("visibilitychange", refreshVisible);
    return () => {
      disposed = true;
      if (interval) clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshVisible);
      active.current?.abort(); active.current = null;
    };
  }, [refresh, live]);
  const visible: Resource<T> = state.key === key ? state : { key, status: "loading", refreshing: false };
  return { ...visible, refresh };
}
