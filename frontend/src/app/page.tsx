"use client";

import { useEffect, useState } from "react";

type Health = { status: string; db: string };

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/health")
      .then((r) => r.json())
      .then(setHealth)
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <main>
      <h1>Tr1k0TaJ</h1>
      <p>Backend: {error ?? (health ? `${health.status}, db: ${health.db}` : "loading…")}</p>
    </main>
  );
}
