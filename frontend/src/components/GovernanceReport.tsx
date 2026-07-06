import { useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ScrollText,
  ShieldCheck
} from "lucide-react";

export type GovernanceReport = {
  auditId: string | null;
  model: string;
  promptPolicyId: string;
  contextPolicyId: string;
  contextPolicy?: {
    id: string;
    selectedChunks: number;
    droppedChunks: number;
    dropReasons: string[];
    internalTokens: number;
    externalTokens: number;
    totalPromptTokens: number;
  };
  sourceUsage: {
    internalSourcesUsed: number;
    externalSourcesUsed: number;
    documentVersionsUsed: number;
    expiredDocumentsUsed: number;
  };
  validation: {
    citationCoverage: number | null;
    groundingScore: number | null;
    unsupportedClaims: number;
    guardrailOutcome: string;
    piiDetected: boolean;
  };
  risk: {
    riskScore: number;
    riskLevel: string;
    humanReviewRequired: boolean;
  };
  decision: string;
};

// Governance signal, not decoration: each risk level maps to its own chromatic
// token so the level reads at a glance from color alone.
const RISK_STYLES: Record<string, string> = {
  low: "border-grounded/25 bg-grounded-bg text-grounded",
  medium: "border-held/25 bg-held-bg text-held",
  high: "border-blocked/25 bg-blocked-bg text-blocked"
};

const DECISION_LABELS: Record<string, string> = {
  returned: "Returned to user",
  returned_with_warning: "Returned with warning",
  held_for_review: "Held for human review",
  requires_review: "Requires human review",
  blocked: "Blocked"
};

function pct(value: number | null) {
  return value === null ? "N/A" : `${Math.round(value * 100)}%`;
}

function titleCase(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <span className="text-muted">{label}</span>
      <span className="text-right font-mono font-medium tabular-nums text-ink">
        {value}
      </span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-normal text-faint">
        {title}
      </div>
      <div className="text-xs leading-5">{children}</div>
    </div>
  );
}

export function GovernanceReport({ report }: { report?: GovernanceReport | null }) {
  const [isOpen, setIsOpen] = useState(false);

  if (!report) {
    return (
      <div className="mt-4 border-t border-line pt-3 text-xs text-faint">
        <div className="flex items-center gap-2">
          <ShieldCheck size={14} className="text-faint" />
          Governance report unavailable for this answer
        </div>
      </div>
    );
  }

  const riskLevel = report.risk.riskLevel.toLowerCase();
  const riskStyle = RISK_STYLES[riskLevel] ?? RISK_STYLES.low;
  const decisionLabel = DECISION_LABELS[report.decision] ?? titleCase(report.decision);

  return (
    <div className="mt-4 border-t border-line pt-3">
      <button
        className="flex w-full items-center gap-2 text-xs font-semibold uppercase tracking-normal text-muted hover:text-ink"
        onClick={() => setIsOpen((open) => !open)}
        type="button"
        aria-expanded={isOpen}
      >
        {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <ShieldCheck size={14} className="text-external" />
        Governance Report
        <span
          className={`ml-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold normal-case ${riskStyle}`}
        >
          {titleCase(riskLevel)} risk
        </span>
      </button>

      {isOpen && (
        <div className="mt-3 grid gap-4 sm:grid-cols-2">
          <Section title="Source Usage">
            <Row label="Internal sources" value={`${report.sourceUsage.internalSourcesUsed}`} />
            <Row label="External sources" value={`${report.sourceUsage.externalSourcesUsed}`} />
            <Row label="Document versions" value={`${report.sourceUsage.documentVersionsUsed}`} />
            <Row label="Expired documents" value={`${report.sourceUsage.expiredDocumentsUsed}`} />
          </Section>

          {report.contextPolicy && (
            <Section title="Context Policy">
              <Row label="Policy" value={report.contextPolicy.id} />
              <Row
                label="Chunks selected"
                value={`${report.contextPolicy.selectedChunks}/${
                  report.contextPolicy.selectedChunks + report.contextPolicy.droppedChunks
                }`}
              />
              <Row label="Dropped" value={`${report.contextPolicy.droppedChunks}`} />
              <Row
                label="Tokens (int + ext)"
                value={`${report.contextPolicy.internalTokens} + ${report.contextPolicy.externalTokens} = ${report.contextPolicy.totalPromptTokens}`}
              />
              <div className="mt-1 flex flex-wrap gap-1">
                {report.contextPolicy.dropReasons.length === 0 ? (
                  <span className="text-faint">No drops</span>
                ) : (
                  report.contextPolicy.dropReasons.map((reason) => (
                    <span
                      key={reason}
                      className="rounded-full border border-held/25 bg-held-bg px-2 py-0.5 text-[10px] font-medium text-held"
                    >
                      {reason}
                    </span>
                  ))
                )}
              </div>
            </Section>
          )}

          <Section title="Validation">
            <Row label="Citation coverage" value={pct(report.validation.citationCoverage)} />
            <Row label="Grounding score" value={pct(report.validation.groundingScore)} />
            <Row label="Unsupported claims" value={`${report.validation.unsupportedClaims}`} />
            <Row label="Guardrail outcome" value={titleCase(report.validation.guardrailOutcome)} />
            <Row label="PII detected" value={report.validation.piiDetected ? "Yes" : "No"} />
          </Section>

          <Section title="Risk">
            <Row label="Risk score" value={report.risk.riskScore.toFixed(2)} />
            <Row label="Risk level" value={titleCase(riskLevel)} />
            <div className="mt-1 flex items-center gap-1.5">
              {report.risk.humanReviewRequired ? (
                <>
                  <AlertTriangle size={13} className="text-held" />
                  <span className="text-held">Human review required</span>
                </>
              ) : (
                <>
                  <CheckCircle2 size={13} className="text-grounded" />
                  <span className="text-grounded">No review required</span>
                </>
              )}
            </div>
          </Section>

          <Section title="Decision">
            <div className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 ${riskStyle}`}>
              <ScrollText size={13} />
              <span className="font-medium normal-case">{decisionLabel}</span>
            </div>
            <div className="mt-2 font-mono text-[11px] text-faint">
              <div>Model: {report.model}</div>
              <div>Prompt policy: {report.promptPolicyId}</div>
              <div>Context policy: {report.contextPolicyId}</div>
              {report.auditId && <div className="break-all">Audit ID: {report.auditId}</div>}
            </div>
          </Section>
        </div>
      )}
    </div>
  );
}

export default GovernanceReport;
