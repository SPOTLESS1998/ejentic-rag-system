import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Ejentic AI",
  description: "Enterprise Hybrid RAG System",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className="h-full antialiased font-sans"
    >
      <head>
        <script src="https://cdn.tailwindcss.com"></script>
      </head>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
