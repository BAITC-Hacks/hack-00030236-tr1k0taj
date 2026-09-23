import { redirect } from "next/navigation";

export default async function LegacyTracesPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const query = new URLSearchParams();
  for (const key of ["session", "trace"]) {
    const value = params[key];
    if (typeof value === "string") query.set(key, value);
  }
  redirect(`/history${query.size ? `?${query}` : ""}`);
}
