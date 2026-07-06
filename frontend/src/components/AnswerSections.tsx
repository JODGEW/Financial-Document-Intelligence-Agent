import type { ComponentProps } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { normalizeAnswerMarkdown, parseAnswerSections } from "../lib/answer";
import type { AnswerSection } from "../lib/answer";

const markdownComponents: Components = {
  table: (props: ComponentProps<"table">) => (
    <div className="md-table-wrap">
      <table {...props} />
    </div>
  ),
  a: (props: ComponentProps<"a">) => (
    <a target="_blank" rel="noreferrer" {...props} />
  )
};

function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown-answer">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
        {children}
      </ReactMarkdown>
    </div>
  );
}

type SectionBlockProps = {
  label: string;
  accent: "internal" | "external";
  section: AnswerSection;
};

/**
 * The provenance rail: a governance-tinted left rule marks where each answer
 * segment came from — green for internal corpus evidence, blue for labeled
 * external context. The mono origin tag reads like an audit-margin note.
 */
const ACCENT_STYLES = {
  internal: {
    rule: "border-l-grounded",
    label: "text-grounded",
    origin: "corpus"
  },
  external: {
    rule: "border-l-external",
    label: "text-external",
    origin: "web"
  }
} as const;

function SectionBlock({ label, accent, section }: SectionBlockProps) {
  const styles = ACCENT_STYLES[accent];
  return (
    <section className={`border-l-[3px] pl-3.5 ${styles.rule}`}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span
          className={`inline-flex items-baseline gap-1.5 text-[11px] font-semibold uppercase tracking-wide ${styles.label}`}
        >
          <span className="font-mono text-[10px] normal-case tracking-normal opacity-70">
            {styles.origin}
          </span>
          {label}
        </span>
        {section.status && (
          <span className="text-xs text-muted">{section.status}</span>
        )}
      </div>
      {section.body && (
        <div className="mt-1.5">
          <Markdown>{section.body}</Markdown>
        </div>
      )}
    </section>
  );
}

/**
 * An external section that reported nothing usable: no body and an
 * availability status of "unavailable"/"not available". Rendered as a muted
 * one-liner instead of a full ruled block.
 */
function isUnusedExternal(section: AnswerSection) {
  return (
    section.body === "" &&
    section.status !== null &&
    /^(unavailable|not available)\b/i.test(section.status)
  );
}

/**
 * Renders an assistant answer. Answers in the mandated Result Summary shape
 * split into labeled internal-evidence and external-context blocks; anything
 * else (guardrail blocks, held-for-review notices, partial streams) renders
 * as plain markdown.
 */
export function AnswerSections({ content }: { content: string }) {
  const parsed = parseAnswerSections(normalizeAnswerMarkdown(content));

  if (!parsed.internal && !parsed.external) {
    return <Markdown>{parsed.preamble}</Markdown>;
  }

  return (
    <div className="space-y-4">
      {parsed.preamble && <Markdown>{parsed.preamble}</Markdown>}
      {parsed.internal && (
        <SectionBlock
          label="Internal corpus answer"
          accent="internal"
          section={parsed.internal}
        />
      )}
      {parsed.external &&
        (isUnusedExternal(parsed.external) ? (
          <p className="text-xs text-faint">
            External context: {parsed.external.status}.
          </p>
        ) : (
          <SectionBlock
            label="External context"
            accent="external"
            section={parsed.external}
          />
        ))}
    </div>
  );
}

export default AnswerSections;
