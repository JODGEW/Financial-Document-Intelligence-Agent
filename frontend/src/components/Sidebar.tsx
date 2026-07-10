import {
  ExternalLink,
  FileText,
  History,
  MessageSquarePlus,
  PanelLeftClose
} from "lucide-react";
import type { ArchivedSession, CorpusDocument, CorpusStatus } from "../types";
import { documentViewUrl } from "../lib/answer";
import EmptyCorpusState from "./EmptyCorpusState";

type SidebarProps = {
  isOpen: boolean;
  onClose: () => void;
  documents: CorpusDocument[];
  corpusStatus: CorpusStatus;
  sessions: ArchivedSession[];
  /** The filed session currently loaded in the main panel, if any. */
  activeSessionId: string | null;
  isLoading: boolean;
  onNewAnalysis: () => void;
  onOpenSession: (id: string) => void;
};

function SectionHeader({
  icon,
  label,
  count,
  title
}: {
  icon: React.ReactNode;
  label: string;
  count?: number;
  title?: string;
}) {
  return (
    <div
      className="mb-2 flex items-center gap-1.5 px-2 text-[11px] font-semibold uppercase tracking-wide text-muted"
      title={title}
    >
      {icon}
      {label}
      {count !== undefined && (
        <span className="font-mono font-medium tabular-nums text-faint">
          {count}
        </span>
      )}
    </div>
  );
}

export function Sidebar({
  isOpen,
  onClose,
  documents,
  corpusStatus,
  sessions,
  activeSessionId,
  isLoading,
  onNewAnalysis,
  onOpenSession
}: SidebarProps) {
  return (
    <>
      {/* Backdrop for the mobile drawer */}
      {isOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-[272px] shrink-0 flex-col border-r border-line bg-surface transition-transform duration-200 md:static md:z-auto md:transition-none ${
          isOpen ? "translate-x-0" : "-translate-x-full md:hidden"
        }`}
      >
        <div className="flex items-center justify-between gap-3 px-4 pb-3 pt-4">
          <div className="min-w-0">
            <h1 className="text-balance text-[15px] font-semibold leading-snug tracking-tight text-ink">
              Financial Document Intelligence Agent
            </h1>
            <p className="mt-0.5 font-mono text-xs tracking-wide text-faint">
              FDIA
            </p>
          </div>
          <button
            className="grid h-8 w-8 shrink-0 place-items-center rounded-md text-muted hover:bg-raised hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
            onClick={onClose}
            type="button"
            aria-label="Close sidebar"
          >
            <PanelLeftClose size={17} />
          </button>
        </div>

        <div className="px-3">
          <button
            className="flex h-9 w-full items-center justify-center gap-2 rounded-md bg-accent px-3 text-[13px] font-medium text-on-accent hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
            onClick={onNewAnalysis}
            type="button"
            title={
              isLoading
                ? "Stops the current answer and files the thread"
                : undefined
            }
          >
            <MessageSquarePlus size={15} />
            New chat
          </button>
        </div>

        <div className="mt-5 flex-1 overflow-y-auto px-3 pb-4">
          <section>
            <SectionHeader
              icon={<FileText size={12} />}
              label="Corpus documents"
              count={corpusStatus === "ready" ? documents.length : undefined}
              title="Files in the corpus folder. Retrieval uses the last ingested index."
            />
            {documents.length > 0 ? (
              <div className="space-y-0.5 text-[13px] text-muted">
                {documents.map((document) => (
                  <a
                    className="group flex min-h-8 items-center gap-2 rounded-md px-2 py-1.5 hover:bg-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40"
                    href={documentViewUrl(document)}
                    key={document.path}
                    rel="noreferrer"
                    target="_blank"
                    title={document.path}
                  >
                    <span className="min-w-0 flex-1 truncate">
                      {document.name}
                    </span>
                    <span className="shrink-0 rounded border border-line bg-bg px-1 py-px font-mono text-[10px] font-semibold uppercase tabular-nums text-muted">
                      {document.file_type}
                    </span>
                    <ExternalLink
                      size={12}
                      className="shrink-0 text-faint group-hover:text-muted"
                    />
                  </a>
                ))}
              </div>
            ) : (
              <EmptyCorpusState status={corpusStatus} />
            )}
          </section>

          {sessions.length > 0 && (
            <section className="mt-6 border-t border-line pt-4">
              <SectionHeader
                icon={<History size={12} />}
                label="Recent sessions"
                count={sessions.length}
                title="Sessions are kept in this tab only and clear on reload."
              />
              <div className="space-y-0.5 text-[13px]">
                {sessions.map((session) => {
                  const isActive = session.id === activeSessionId;
                  return (
                    <button
                      className={`flex w-full items-baseline gap-2 rounded-md px-2 py-1.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 disabled:cursor-not-allowed disabled:opacity-50 ${
                        isActive
                          ? "bg-raised font-medium text-ink"
                          : "text-muted hover:bg-raised hover:text-ink"
                      }`}
                      key={session.id}
                      onClick={() => onOpenSession(session.id)}
                      type="button"
                      disabled={isLoading}
                      aria-current={isActive ? "true" : undefined}
                      title={
                        isLoading
                          ? "Wait for the current answer to finish"
                          : isActive
                            ? "This session is open"
                            : "Reopen this session"
                      }
                    >
                      <span className="min-w-0 flex-1 truncate">
                        {session.title}
                      </span>
                      <span
                        className={`shrink-0 font-mono text-[11px] tabular-nums ${
                          isActive ? "text-muted" : "text-faint"
                        }`}
                      >
                        {Math.ceil(session.messages.length / 2)}{" "}
                        {session.messages.length > 2 ? "turns" : "turn"}
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>
          )}
        </div>
      </aside>
    </>
  );
}

export default Sidebar;
