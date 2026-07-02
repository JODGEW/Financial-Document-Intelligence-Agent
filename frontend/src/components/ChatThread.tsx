import type { ChatMessage } from "../types";
import ChatMessageView from "./ChatMessage";

type ChatThreadProps = {
  messages: ChatMessage[];
  isLoading: boolean;
  streamingStatus: string | null;
};

export function ChatThread({
  messages,
  isLoading,
  streamingStatus
}: ChatThreadProps) {
  const lastMessage = messages[messages.length - 1];

  return (
    <div className="space-y-5">
      {messages.map((message) => (
        <ChatMessageView
          key={message.id}
          message={message}
          isStreaming={
            isLoading &&
            message.role === "assistant" &&
            message.id === lastMessage?.id
          }
          streamingStatus={streamingStatus}
        />
      ))}
    </div>
  );
}

export default ChatThread;
