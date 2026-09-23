import type { CSSProperties } from "react";
const paths: Record<string, string> = {
  mic: "M12 15a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v7a3 3 0 0 0 3 3ZM5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8",
  call: "M8 3H4a1 1 0 0 0-1 1c0 9.4 7.6 17 17 17a1 1 0 0 0 1-1v-4l-5-2-2 2a14 14 0 0 1-6-6l2-2-2-5Z",
  grid: "M3 3h7v7H3ZM14 3h7v7h-7ZM3 14h7v7H3ZM14 14h7v7h-7Z",
  history: "M3 11a9 9 0 1 1 2.6 7.4M3 4v7h7M12 7v5l3 2",
  arrow: "M5 12h14M13 6l6 6-6 6", chevron: "m9 5 7 7-7 7", down: "m6 9 6 6 6-6",
  plus: "M12 5v14M5 12h14", close: "m6 6 12 12M6 18 18 6", check: "m5 12 4 4L19 6",
  search: "M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  settings: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1 1-3Z",
  info: "M12 11v6M12 7h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  shield: "M12 3 3 6v6c0 5 9 9 9 9s9-4 9-9V6l-9-3Zm-4 9 3 3 5-6",
  document: "M14 2H5v20h14V7l-5-5ZM14 2v6h5M8 12h8M8 16h5",
  layers: "m12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 16l10 5 10-5",
  spark: "m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3Z",
  stop: "M6 6h12v12H6Z", play: "m8 4 12 8-12 8V4Z", volume: "M11 4 5 9H2v6h3l6 5V4ZM15 8a6 6 0 0 1 0 8M18 4a11 11 0 0 1 0 16",
  keyboard: "M2 5h20v14H2ZM5 9h1m3 0h1m3 0h1m3 0h1M5 12h1m3 0h1m3 0h1m3 0h1M7 16h10",
  copy: "M8 8h13v13H8ZM16 8V3H3v13h5", download: "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
  menu: "M4 6h16M4 12h16M4 18h16", filter: "M4 6h16M7 12h10M10 18h4",
  clock: "M12 7v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0", globe: "M2 12h20M12 2c6 5 6 15 0 20-6-5-6-15 0-20ZM22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
};
export function Icon({ name, size = 20, style }: { name: string; size?: number; style?: CSSProperties }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={style}><path d={paths[name] ?? paths.info} /></svg>;
}
