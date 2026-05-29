import { FormEvent, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import {
  ArrowDown,
  ArrowUp,
  BookOpenText,
  ExternalLink,
  FileSearch,
  Loader2,
  Menu,
  MessageSquarePlus,
  PanelLeftClose,
  RotateCcw,
  ShieldCheck
} from "lucide-react";
import GovernanceReportPanel, {
  type GovernanceReport
} from "./components/GovernanceReport";

type Role = "user" | "assistant";

type ChatMessage = {
  id: string;
  role: Role;
  content: string;
  sources?: RetrievedSource[];
  audit_id?: string | null;
  governance_report?: GovernanceReport | null;
};

type ChatResponse = {
  answer: string;
  sources: RetrievedSource[];
  audit_id: string | null;
  governance_report: GovernanceReport | null;
};

type ChatStreamEvent =
  | { type: "status"; message: string }
  | { type: "token"; content: string }
  | { type: "replace"; content: string }
  | { type: "sources"; sources: RetrievedSource[] }
  | { type: "audit_id"; audit_id: string | null }
  | { type: "governance_report"; report: GovernanceReport | null }
  | { type: "warning"; message: string }
  | { type: "error"; message: string }
  | { type: "done" };

type RetrievedSource = {
  rank: number;
  source: string;
  source_name: string;
  source_path: string;
  page: number | null;
  excerpt: string;
};

type CorpusDocument = {
  name: string;
  path: string;
  file_type: string;
  url: string;
};

const examples = [
  "Summarize Acme Corp's cybersecurity risk disclosures and cite the source document.",
  "What does the compliance policy say about blackout periods for personal trading?",
  "What were Acme Corp's fiscal year 2025 revenue and earnings per share?",
  "What does the internal research note say about cybersecurity disclosure trends?"
];

const PDF_VIEW_QUERY = "?view=inline&v=title";
const BOTTOM_SCROLL_THRESHOLD = 180;

function makeId() {
  return crypto.randomUUID();
}

function normalizeAnswerMarkdown(content: string) {
  return content
    .replace(/<span[^>]*$/gi, "")
    .replace(/<\/span?[^>]*$/gi, "")
    .replace(
      /<span[^>]*>\s*Internal Corpus Answer:\s*<\/span>/gi,
      "**Internal Corpus Answer:**"
    )
    .replace(
      /<span[^>]*>\s*External Context:\s*<\/span>/gi,
      "**External Context:**"
    )
    .replace(/<\/?span[^>]*>/gi, "");
}

function sourceDocumentUrl(sourcePath: string) {
  const url = `/api/documents/${sourcePath
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/")}`;
  return sourcePath.toLowerCase().endsWith(".pdf") ? `${url}${PDF_VIEW_QUERY}` : url;
}

function documentViewUrl(document: CorpusDocument) {
  return document.file_type === "pdf" ? `${document.url}${PDF_VIEW_QUERY}` : document.url;
}

function pageScrollHeight() {
  return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
}

function isNearPageBottom() {
  return window.innerHeight + window.scrollY >= pageScrollHeight() - BOTTOM_SCROLL_THRESHOLD;
}

function scrollToPageBottom(behavior: ScrollBehavior = "smooth") {
  window.scrollTo({
    top: pageScrollHeight(),
    behavior
  });
}

function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [streamingStatus, setStreamingStatus] = useState<string | null>(null);
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);
  const [documents, setDocuments] = useState<CorpusDocument[]>([]);
  const [documentError, setDocumentError] = useState<string | null>(null);
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const shouldAutoScrollRef = useRef(true);

  const canSubmit = input.trim().length > 0 && !isLoading;
  const hasMessages = messages.length > 0;

  const history = useMemo(
    () =>
      messages.map(({ role, content }) => ({
        role,
        content
      })),
    [messages]
  );

  function handleScrollToBottom() {
    shouldAutoScrollRef.current = true;
    setShowScrollToBottom(false);
    scrollToPageBottom("smooth");
  }

  useEffect(() => {
    function handleScroll() {
      const isNearBottom = isNearPageBottom();
      shouldAutoScrollRef.current = isNearBottom;
      setShowScrollToBottom(hasMessages && !isNearBottom);
    }

    handleScroll();
    window.addEventListener("scroll", handleScroll, { passive: true });
    window.addEventListener("resize", handleScroll);

    return () => {
      window.removeEventListener("scroll", handleScroll);
      window.removeEventListener("resize", handleScroll);
    };
  }, [hasMessages]);

  useLayoutEffect(() => {
    if (!shouldAutoScrollRef.current) {
      setShowScrollToBottom(hasMessages);
      return;
    }

    const frame = requestAnimationFrame(() => {
      scrollToPageBottom(isLoading ? "auto" : "smooth");
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
        setDocumentError(null);
      } catch {
        setDocumentError("Document list unavailable");
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

    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          message,
          history
        })
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
    } finally {
      setIsLoading(false);
      setStreamingStatus(null);
      inputRef.current?.focus();
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void sendMessage(input);
  }

  function startNewChat() {
    setMessages([]);
    setInput("");
    setError(null);
    setStreamingStatus(null);
    shouldAutoScrollRef.current = true;
    setShowScrollToBottom(false);
    inputRef.current?.focus();
  }

  return (
    <main className="flex min-h-screen bg-white text-zinc-900">
      <aside
        className={`${
          isSidebarOpen ? "flex" : "hidden"
        } fixed inset-y-0 left-0 z-30 h-screen w-[280px] flex-col overflow-y-auto border-r border-zinc-200 bg-zinc-50 px-3 py-4`}
      >
        <div className="mb-5 flex items-center justify-between gap-3 px-2">
          <div className="min-w-0">
            <h1 className="truncate text-[15px] font-semibold">
              Financial Document Agent
            </h1>
            <p className="mt-1 text-xs text-zinc-500">Private corpus Q&A</p>
          </div>
          <button
            className="grid h-9 w-9 shrink-0 place-items-center rounded-md text-zinc-600 hover:bg-zinc-200"
            onClick={() => setIsSidebarOpen(false)}
            type="button"
            aria-label="Close sidebar"
          >
            <PanelLeftClose size={18} />
          </button>
        </div>

        <button
          className="mb-5 flex h-10 w-full items-center gap-2 rounded-md border border-zinc-300 bg-white px-3 text-sm font-medium text-zinc-800 hover:bg-zinc-100"
          onClick={startNewChat}
          type="button"
        >
          <MessageSquarePlus size={17} />
          New chat
        </button>

        <section className="border-t border-zinc-200 pt-4">
          <div className="mb-2 px-2 text-xs font-semibold text-zinc-500">
            Corpus documents
          </div>
          <div className="space-y-1 text-[13px] text-zinc-600">
            {documents.map((document) => (
              <a
                className="flex min-h-9 items-center gap-2 rounded-md px-2 py-1.5 hover:bg-zinc-200"
                href={documentViewUrl(document)}
                key={document.path}
                rel="noreferrer"
                target="_blank"
                title={document.path}
              >
                <FileSearch size={15} className="text-emerald-700" />
                <span className="min-w-0 flex-1 truncate">{document.name}</span>
                <span className="rounded border border-zinc-200 bg-white px-1.5 py-0.5 text-[10px] font-semibold uppercase text-zinc-500">
                  {document.file_type}
                </span>
                <ExternalLink size={13} className="shrink-0 text-zinc-400" />
              </a>
            ))}
            {documents.length === 0 && (
              <div className="px-2 py-1.5 text-xs text-zinc-500">
                {documentError ?? "No reviewable documents found"}
              </div>
            )}
          </div>
        </section>
      </aside>

      <section
        className={`flex min-h-screen min-w-0 flex-1 flex-col ${
          isSidebarOpen ? "md:ml-[280px]" : "md:ml-0"
        }`}
      >
        <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b border-zinc-200 bg-white/95 px-4 backdrop-blur">
          <div className="flex items-center gap-2">
            {!isSidebarOpen && (
              <button
                className="grid h-9 w-9 place-items-center rounded-md text-zinc-700 hover:bg-zinc-100"
                onClick={() => setIsSidebarOpen(true)}
                type="button"
                aria-label="Open sidebar"
              >
                <Menu size={19} />
              </button>
            )}
            <div>
              <div className="text-sm font-semibold">Document Q&A</div>
              <div className="text-xs text-zinc-500">
                Internal evidence first. External context labeled separately.
              </div>
            </div>
          </div>
          <div className="hidden items-center gap-2 text-xs text-zinc-500 sm:flex">
            <ShieldCheck size={16} className="text-blue-700" />
            Source-aware responses
          </div>
        </header>

        <div className="mx-auto flex w-full max-w-4xl flex-1 flex-col px-4 pb-36 pt-8">
          {!hasMessages && (
            <section className="mx-auto flex w-full max-w-3xl flex-1 flex-col justify-center">
              <div className="mb-8">
                <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-md bg-emerald-700 text-white">
                  <BookOpenText size={24} />
                </div>
                <h2 className="text-3xl font-semibold tracking-normal text-zinc-950">
                  Ask your financial document corpus
                </h2>
                <p className="mt-3 max-w-2xl text-sm leading-6 text-zinc-600">
                  Query filings, compliance policy, and internal research with
                  citations preserved in the answer format.
                </p>
              </div>

              <div className="grid gap-3 md:grid-cols-2">
                {examples.map((example) => (
                  <button
                    className="min-h-[88px] rounded-md border border-zinc-200 bg-white p-4 text-left text-sm leading-5 text-zinc-700 shadow-sm hover:border-zinc-300 hover:bg-zinc-50"
                    key={example}
                    onClick={() => void sendMessage(example)}
                    type="button"
                  >
                    {example}
                  </button>
                ))}
              </div>
            </section>
          )}

          {hasMessages && (
            <div className="space-y-7">
              {messages.map((message) => (
                <article
                  className={`flex ${
                    message.role === "user" ? "justify-end" : "justify-start"
                  }`}
                  key={message.id}
                >
                  <div
                    className={`max-w-[86%] rounded-md px-4 py-3 text-sm leading-6 shadow-sm ${
                      message.role === "user"
                        ? "bg-zinc-900 text-white"
                        : "border border-zinc-200 bg-white text-zinc-800"
                    }`}
                  >
                    {message.role === "assistant" ? (
                      <div className="markdown-answer">
                        {message.content ? (
                          <ReactMarkdown>
                            {normalizeAnswerMarkdown(message.content)}
                          </ReactMarkdown>
                        ) : isLoading ? (
                          <div className="flex items-center gap-2 text-sm text-zinc-500">
                            <Loader2 size={15} className="animate-spin" />
                            {streamingStatus ?? "Preparing answer..."}
                          </div>
                        ) : (
                          <div className="text-sm text-zinc-500">
                            No answer text was returned for this query.
                          </div>
                        )}
                        {message.sources && message.sources.length > 0 && (
                          <div className="mt-4 border-t border-zinc-200 pt-3">
                            <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-normal text-zinc-500">
                              <FileSearch size={14} className="text-emerald-700" />
                              Retrieved chunks
                            </div>
                            <div className="divide-y divide-zinc-100">
                              {message.sources.map((source) => (
                                <div
                                  className="py-2 text-xs leading-5"
                                  key={`${source.rank}-${source.source}-${source.page ?? "n/a"}`}
                                >
                                  <a
                                    className="inline-flex max-w-full items-center gap-1 font-medium text-emerald-800 hover:text-emerald-900"
                                    href={sourceDocumentUrl(source.source_path)}
                                    rel="noreferrer"
                                    target="_blank"
                                    title={source.source}
                                  >
                                    <span className="truncate">
                                      Source {source.rank}: {source.source_name}
                                      {source.page !== null ? `, page ${source.page}` : ""}
                                    </span>
                                    <ExternalLink size={12} className="shrink-0" />
                                  </a>
                                  <div className="mt-1 break-all font-mono text-[11px] text-zinc-500">
                                    {source.source_path}
                                  </div>
                                  <p className="mt-1 text-zinc-600">{source.excerpt}</p>
                                </div>
                              ))}
                            </div>
                            {message.audit_id && (
                              <div className="mt-2 break-all text-[11px] text-zinc-400">
                                Audit ID: {message.audit_id}
                              </div>
                            )}
                          </div>
                        )}
                        {message.governance_report && (
                          <GovernanceReportPanel report={message.governance_report} />
                        )}
                      </div>
                    ) : (
                      <p className="whitespace-pre-wrap">{message.content}</p>
                    )}
                  </div>
                </article>
              ))}

              {isLoading && (
                <div className="flex items-center gap-2 text-sm text-zinc-500">
                  <Loader2 size={16} className="animate-spin" />
                  {streamingStatus ?? "Searching documents and composing answer"}
                </div>
              )}
            </div>
          )}
        </div>

        {error && (
          <div className="fixed bottom-28 left-1/2 z-30 w-[min(92vw,720px)] -translate-x-1/2 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800 shadow-sm">
            {error}
          </div>
        )}

        {showScrollToBottom && (
          <button
            className={`fixed bottom-28 z-30 grid h-10 w-10 place-items-center rounded-full border border-zinc-200 bg-white text-zinc-700 shadow-lg hover:bg-zinc-50 ${
              isSidebarOpen
                ? "left-[calc(50%+140px)] md:left-[calc(50%+140px)]"
                : "left-1/2"
            } -translate-x-1/2`}
            onClick={handleScrollToBottom}
            type="button"
            aria-label="Scroll to latest message"
            title="Scroll to latest message"
          >
            <ArrowDown size={18} />
          </button>
        )}

        <form
          className={`fixed bottom-0 left-0 right-0 z-20 border-t border-zinc-200 bg-white/95 px-4 py-4 backdrop-blur ${
            isSidebarOpen ? "md:left-[280px]" : "md:left-0"
          }`}
          onSubmit={handleSubmit}
        >
          <div className="mx-auto flex w-full max-w-4xl items-end gap-2 rounded-md border border-zinc-300 bg-white p-2 shadow-lg">
            <textarea
              ref={inputRef}
              className="max-h-36 min-h-[44px] flex-1 resize-none bg-transparent px-2 py-2 text-sm leading-6 text-zinc-900 outline-none placeholder:text-zinc-400"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  if (canSubmit) {
                    void sendMessage(input);
                  }
                }
              }}
              placeholder="Ask about Acme filings, policy, or research notes"
              rows={1}
            />
            <button
              className="grid h-10 w-10 shrink-0 place-items-center rounded-md bg-zinc-900 text-white disabled:cursor-not-allowed disabled:bg-zinc-300"
              disabled={!canSubmit}
              type="submit"
              aria-label="Send message"
            >
              {isLoading ? (
                <Loader2 size={18} className="animate-spin" />
              ) : (
                <ArrowUp size={18} />
              )}
            </button>
            <button
              className="grid h-10 w-10 shrink-0 place-items-center rounded-md text-zinc-500 hover:bg-zinc-100"
              onClick={startNewChat}
              type="button"
              aria-label="Reset chat"
            >
              <RotateCcw size={17} />
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}

export default App;
