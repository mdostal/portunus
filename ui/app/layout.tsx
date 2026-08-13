import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Portunus Vault",
  description: "Localhost-only Portunus secret manager — tags and state only, never a value.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
