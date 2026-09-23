import { Suspense } from "react";
import { TraceJournal } from "@/components/trace-journal";

export default function TracesPage() {
  return <Suspense fallback={<p role="status">Загрузка журнала… / Журнал жүктелуде…</p>}><TraceJournal /></Suspense>;
}
