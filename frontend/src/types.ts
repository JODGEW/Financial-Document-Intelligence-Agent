import type { GovernanceReport } from "./components/GovernanceReport";

export type Role = "user" | "assistant";

export type ChatMessage = {
  id: string;
  role: Role;
  content: string;
  sources?: RetrievedSource[];
  audit_id?: string | null;
  governance_report?: GovernanceReport | null;
};

export type ChatStreamEvent =
  | { type: "status"; message: string }
  | { type: "token"; content: string }
  | { type: "replace"; content: string }
  | { type: "sources"; sources: RetrievedSource[] }
  | { type: "audit_id"; audit_id: string | null }
  | { type: "governance_report"; report: GovernanceReport | null }
  | { type: "warning"; message: string }
  | { type: "error"; message: string }
  | { type: "done" };

export type RetrievedSource = {
  rank: number;
  source: string;
  source_name: string;
  source_path: string;
  page: number | null;
  excerpt: string;
};

export type CorpusDocument = {
  name: string;
  path: string;
  file_type: string;
  url: string;
};

export type CorpusStatus = "loading" | "ready" | "error";

export type ArchivedSession = {
  id: string;
  title: string;
  messages: ChatMessage[];
};
