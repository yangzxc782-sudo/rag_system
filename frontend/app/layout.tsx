import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "铸型工艺知识库 RAG 管理系统",
  description: "铸型工艺知识检索、持久化多轮问答与证据管理",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
