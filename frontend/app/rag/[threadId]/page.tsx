import { notFound } from "next/navigation";
import RagWorkspace from "@/components/RagWorkspace";
import { isUuid } from "@/lib/qa-sessions";

export default async function ConversationPage({ params }: { params: Promise<{ threadId: string }> }) {
  const { threadId } = await params;
  if (!isUuid(threadId)) notFound();
  return <RagWorkspace threadId={threadId.toLowerCase()} />;
}
