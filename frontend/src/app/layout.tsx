import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Tr1k0TaJ",
  description: "Hackathon project",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
