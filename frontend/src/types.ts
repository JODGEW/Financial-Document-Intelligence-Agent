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
  source_name: string;
  source_path: string;
  section_title?: string | null;
  page: number | null;
  excerpt: string;
};

export type WorkspaceId = "chat" | "review";

export type ReviewStatus = "pending" | "approved" | "rejected";

export type ReviewStatusFilter = ReviewStatus | "all";

export type ReviewSummary = {
  reviewId: string;
  question: string;
  riskScore: number;
  riskLevel: string;
  riskReasons: string[];
  reviewStatus: ReviewStatus;
  createdAt: string;
  reviewedAt: string | null;
  wasWithheld: boolean | null;
};

export type SafeReviewSource = {
  rank: number | null;
  sourceName: string | null;
  sourcePath: string | null;
  sectionTitle: string | null;
  page: number | null;
  excerpt: string | null;
  documentUrl: string | null;
};

export type ReviewDetail = ReviewSummary & {
  auditId: string | null;
  draftAnswer: string;
  retrievedSources: SafeReviewSource[];
  decision: string | null;
  reviewerNote: string | null;
  governanceReport: GovernanceReport | null;
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
