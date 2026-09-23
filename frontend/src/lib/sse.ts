// fetch POST stream: handles CRLF, multiline data, comments and split UTF-8.
export async function readSSE(body: ReadableStream<Uint8Array>, receive: (data: unknown) => void, signal: AbortSignal) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data: string[] = [];
  function line(value: string) {
    if (!value) {
      if (data.length) { const payload = data.join("\n"); data = []; receive(JSON.parse(payload)); }
    } else if (value.startsWith("data:")) data.push(value.slice(5).replace(/^ /, ""));
  }
  const abort = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener("abort", abort, { once: true });
  try {
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let newline: number;
      while (!signal.aborted && (newline = buffer.indexOf("\n")) >= 0) {
        line(buffer.slice(0, newline).replace(/\r$/, "")); buffer = buffer.slice(newline + 1);
      }
      if (done) break;
    }
    signal.throwIfAborted();
    // Unfinished event at EOF is deliberately not dispatched (SSE contract).
  } finally { signal.removeEventListener("abort", abort); await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
