import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, X } from "lucide-react";
import type {
  ArchivedSession,
  ChatMessage,
  ChatStreamEvent,
  CorpusDocument,
  CorpusStatus,
  WorkspaceId
} from "./types";
import { makeId } from "./lib/answer";
import { DEFAULT_MODE } from "./modes";
import ChatThread from "./components/ChatThread";
import CommandInput from "./components/CommandInput";
import DocumentContextPanel from "./components/DocumentContextPanel";
import EmptyState from "./components/EmptyState";
import ReviewWorkspace from "./components/ReviewWorkspace";
import Sidebar from "./components/Sidebar";
import TopNav from "./components/TopNav";
import WorkspaceNav from "./components/WorkspaceNav";

const BOTTOM_SCROLL_THRESHOLD = 160;
const SESSION_TITLE_LENGTH = 64;

function sessionTitle(messages: ChatMessage[]) {
  const firstQuestion = messages.find((message) => message.role === "user");
  const title = firstQuestion?.content.trim() ?? "Untitled session";
  return title.length > SESSION_TITLE_LENGTH
    ? `${title.slice(0, SESSION_TITLE_LENGTH)}…`
    : title;
}

function App() {
  // Chat and review are sibling workspaces behind one local flag: no router,
  // no deep links, and a refresh resets to chat by design.
  const [activeWorkspace, setActiveWorkspace] = useState<WorkspaceId>("chat");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessions, setSessions] = useState<ArchivedSession[]>([]);
  // The filed session whose thread is loaded in the main panel, if any.
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [streamingStatus, setStreamingStatus] = useState<string | null>(null);
  const [isSidebarOpen, setIsSidebarOpen] = useState(
    () => window.matchMedia("(min-width: 768px)").matches
  );
  const [isContextPanelOpen, setIsContextPanelOpen] = useState(
    () => window.matchMedia("(min-width: 1280px)").matches
  );
  const [documents, setDocuments] = useState<CorpusDocument[]>([]);
  const [corpusStatus, setCorpusStatus] = useState<CorpusStatus>("loading");
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);

  const inputRef = useRef<HTMLTextAreaElement>(null);
  const threadRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const abortRef = useRef<AbortController | null>(null);

  const hasMessages = messages.length > 0;
  // Progressive disclosure: the evidence rail and its toggle exist only once
  // the current thread has an answer with retrieved sources.
  const hasEvidence = messages.some(
    (message) =>
      message.role === "assistant" && (message.sources?.length ?? 0) > 0
  );

  // The backend rejects history entries with empty content (min_length=1),
  // so drop turns that never received an answer.
  const history = useMemo(
    () =>
      messages
        .filter((message) => message.content.trim().length > 0)
        .map(({ role, content }) => ({
          role,
          content
        })),
    [messages]
  );

  // Keep the drawer/column flags in step with the breakpoints the CSS uses,
  // otherwise resizing across md/xl strands an open column as a modal drawer
  // with a full-screen backdrop (or vice versa).
  useEffect(() => {
    const md = window.matchMedia("(min-width: 768px)");
    const xl = window.matchMedia("(min-width: 1280px)");
    const onMdChange = (event: MediaQueryListEvent) =>
      setIsSidebarOpen(event.matches);
    const onXlChange = (event: MediaQueryListEvent) =>
      setIsContextPanelOpen(event.matches);
    md.addEventListener("change", onMdChange);
    xl.addEventListener("change", onXlChange);
    return () => {
      md.removeEventListener("change", onMdChange);
      xl.removeEventListener("change", onXlChange);
    };
  }, []);

  function handleThreadScroll() {
    const element = threadRef.current;
    if (!element) {
      return;
    }
    const isNearBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight <
      BOTTOM_SCROLL_THRESHOLD;
    shouldAutoScrollRef.current = isNearBottom;
    setShowScrollToBottom(hasMessages && !isNearBottom);
  }

  function scrollThreadToBottom(behavior: ScrollBehavior) {
    threadRef.current?.scrollTo({
      top: threadRef.current.scrollHeight,
      behavior
    });
  }

  function handleScrollToBottom() {
    shouldAutoScrollRef.current = true;
    setShowScrollToBottom(false);
    scrollThreadToBottom("smooth");
  }

  useLayoutEffect(() => {
    if (!hasMessages) {
      return;
    }
    if (!shouldAutoScrollRef.current) {
      setShowScrollToBottom(true);
      return;
    }

    const frame = requestAnimationFrame(() => {
      scrollThreadToBottom(isLoading ? "auto" : "smooth");
      setShowScrollToBottom(false);
    });

    return () => cancelAnimationFrame(frame);
  }, [messages, isLoading, streamingStatus, hasMessages]);

  useEffect(() => {
    async function loadDocuments() {
      try {
        const response = await fetch("/api/documents");
        if (!response.ok) {
          throw new Error(`Request failed with status ${response.status}`);
        }
        const data = (await response.json()) as CorpusDocument[];
        setDocuments(data);
        setCorpusStatus("ready");
      } catch {
        setCorpusStatus("error");
      }
    }

    void loadDocuments();
  }, []);

  async function sendMessage(text: string) {
    const message = text.trim();
    if (!message || isLoading) {
      return;
    }

    const userMessage: ChatMessage = {
      id: makeId(),
      role: "user",
      content: message
    };
    const assistantMessage: ChatMessage = {
      id: makeId(),
      role: "assistant",
      content: "",
      sources: [],
      audit_id: null,
      governance_report: null
    };

    function updateAssistant(update: (message: ChatMessage) => ChatMessage) {
      setMessages((current) =>
        current.map((item) =>
          item.id === assistantMessage.id ? update(item) : item
        )
      );
    }

    function handleStreamEvent(event: ChatStreamEvent) {
      if (event.type === "status") {
        setStreamingStatus(event.message);
        return;
      }
      if (event.type === "token") {
        updateAssistant((assistant) => ({
          ...assistant,
          content: `${assistant.content}${event.content}`
        }));
        return;
      }
      if (event.type === "replace") {
        updateAssistant((assistant) => ({
          ...assistant,
          content: event.content
        }));
        return;
      }
      if (event.type === "sources") {
        updateAssistant((assistant) => ({
          ...assistant,
          sources: event.sources ?? []
        }));
        return;
      }
      if (event.type === "audit_id") {
        updateAssistant((assistant) => ({
          ...assistant,
          audit_id: event.audit_id
        }));
        return;
      }
      if (event.type === "governance_report") {
        updateAssistant((assistant) => ({
          ...assistant,
          governance_report: event.report
        }));
        return;
      }
      if (event.type === "warning") {
        console.warn(event.message);
        return;
      }
      if (event.type === "error") {
        throw new Error(event.message);
      }
    }

    shouldAutoScrollRef.current = true;
    setShowScrollToBottom(false);
    setMessages((current) => [...current, userMessage, assistantMessage]);
    setInput("");
    setError(null);
    setStreamingStatus("Starting request...");
    setIsLoading(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          message,
          history
        }),
        signal: controller.signal
      });

      if (!response.ok) {
        throw new Error(`Request failed with status ${response.status}`);
      }

      if (!response.body) {
        throw new Error("Streaming is not supported by this browser.");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          if (!line.trim()) {
            continue;
          }
          handleStreamEvent(JSON.parse(line) as ChatStreamEvent);
        }
      }

      buffer += decoder.decode();
      if (buffer.trim()) {
        handleStreamEvent(JSON.parse(buffer) as ChatStreamEvent);
      }
    } catch (requestError) {
      // A reset during streaming aborts the fetch on purpose; the thread has
      // already been cleared, so there is nothing to report or restore.
      if (!controller.signal.aborted) {
        const detail =
          requestError instanceof Error
            ? requestError.message
            : "The request failed.";
        setError(detail);
        setMessages((current) =>
          current.filter(
            (msg) => msg.id !== userMessage.id && msg.id !== assistantMessage.id
          )
        );
        // Put the failed question back so it can be edited and resent — but
        // never over a draft typed while the answer was streaming.
        setInput((current) => (current.trim() === "" ? message : current));
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
      setIsLoading(false);
      setStreamingStatus(null);
      inputRef.current?.focus();
    }
  }

  function archiveCurrentSession() {
    if (messages.length === 0) {
      return;
    }
    if (activeSessionId) {
      // The thread came from a filed session — refresh that entry in place.
      setSessions((current) =>
        current.map((session) =>
          session.id === activeSessionId
            ? { ...session, title: sessionTitle(messages), messages }
            : session
        )
      );
      return;
    }
    const archived: ArchivedSession = {
      id: makeId(),
      title: sessionTitle(messages),
      messages
    };
    setSessions((current) => [archived, ...current]);
  }

  function startNewAnalysis() {
    // Cancel an in-flight stream so a hung request can't lock the workspace.
    // The partial answer is archived as-is with the rest of the thread.
    abortRef.current?.abort();
    archiveCurrentSession();
    setActiveSessionId(null);
    setMessages([]);
    setInput("");
    setError(null);
    setStreamingStatus(null);
    // Back to the breakpoint default so the next thread's evidence rail
    // auto-shows on desktop even if it was closed in the previous thread.
    setIsContextPanelOpen(window.matchMedia("(min-width: 1280px)").matches);
    shouldAutoScrollRef.current = true;
    setShowScrollToBottom(false);
    inputRef.current?.focus();
  }

  function openSession(id: string) {
    if (isLoading || id === activeSessionId) {
      return;
    }
    const session = sessions.find((item) => item.id === id);
    if (!session) {
      return;
    }
    archiveCurrentSession();
    setMessages(session.messages);
    setActiveSessionId(id);
    setError(null);
    shouldAutoScrollRef.current = true;
  }

  function usePrompt(prompt: string) {
    setInput(prompt);
    inputRef.current?.focus();
  }

  const workspaceNav = (
    <WorkspaceNav active={activeWorkspace} onSelect={setActiveWorkspace} />
  );

  if (activeWorkspace === "review") {
    return (
      <div className="app-shell flex overflow-hidden bg-bg text-ink">
        <ReviewWorkspace workspaceNav={workspaceNav} />
      </div>
    );
  }

  return (
    <div className="app-shell flex overflow-hidden bg-bg text-ink">
      <Sidebar
        isOpen={isSidebarOpen}
        onClose={() => setIsSidebarOpen(false)}
        documents={documents}
        corpusStatus={corpusStatus}
        sessions={sessions}
        activeSessionId={activeSessionId}
        isLoading={isLoading}
        onNewAnalysis={startNewAnalysis}
        onOpenSession={openSession}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        <TopNav
          isSidebarOpen={isSidebarOpen}
          onOpenSidebar={() => setIsSidebarOpen(true)}
          showEvidenceToggle={hasEvidence}
          isContextPanelOpen={isContextPanelOpen}
          onToggleContextPanel={() => setIsContextPanelOpen((open) => !open)}
          workspaceNav={workspaceNav}
        />

        <div className="relative flex min-h-0 flex-1 flex-col">
          <div
            ref={threadRef}
            onScroll={handleThreadScroll}
            className="flex-1 overflow-y-auto px-3 sm:px-6"
          >
            <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col py-5">
              {hasMessages ? (
                <ChatThread
                  messages={messages}
                  isLoading={isLoading}
                  streamingStatus={streamingStatus}
                />
              ) : (
                <EmptyState onUsePrompt={usePrompt} />
              )}
            </div>
          </div>

          {showScrollToBottom && (
            <button
              className="absolute bottom-4 left-1/2 grid h-9 w-9 -translate-x-1/2 place-items-center rounded-full border border-line bg-surface text-ink shadow-md hover:bg-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
              onClick={handleScrollToBottom}
              type="button"
              aria-label="Scroll to latest message"
              title="Scroll to latest message"
            >
              <ArrowDown size={17} />
            </button>
          )}
        </div>

        {error && (
          <div className="shrink-0 px-3 pt-2 sm:px-4">
            <div className="mx-auto flex w-full max-w-3xl items-start justify-between gap-3 rounded-md border border-blocked/30 bg-blocked-bg px-3.5 py-2.5 text-sm text-blocked">
              <span className="min-w-0">
                The request didn't complete: {error}
              </span>
              <button
                className="grid h-6 w-6 shrink-0 place-items-center rounded text-blocked hover:bg-blocked/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blocked/50"
                onClick={() => setError(null)}
                type="button"
                aria-label="Dismiss error"
              >
                <X size={14} />
              </button>
            </div>
          </div>
        )}

        <CommandInput
          value={input}
          onChange={setInput}
          onSend={() => void sendMessage(input)}
          onReset={startNewAnalysis}
          isLoading={isLoading}
          placeholder={DEFAULT_MODE.placeholder}
          inputRef={inputRef}
          showQuickActions={hasMessages}
        />
      </main>

      {hasEvidence && (
        <DocumentContextPanel
          isOpen={isContextPanelOpen}
          onClose={() => setIsContextPanelOpen(false)}
          messages={messages}
          isLoading={isLoading}
        />
      )}
    </div>
  );
}

export default App;
