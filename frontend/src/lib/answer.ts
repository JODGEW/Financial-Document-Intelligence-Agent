import type { CorpusDocument } from "../types";

const PDF_VIEW_QUERY = "?view=inline&v=title";

export function makeId() {
  return crypto.randomUUID();
}

/**
 * The backend's mandated answer shape wraps the "Internal Corpus Answer" and
 * "External Context" labels in HTML spans. Convert them to markdown bold
 * markers and strip any partially streamed span fragments.
 */
export function normalizeAnswerMarkdown(content: string) {
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

const INTERNAL_MARKER = "**Internal Corpus Answer:**";
const EXTERNAL_MARKER = "**External Context:**";
// An availability phrase per the mandated format: starts with an availability
// keyword and contains no further sentence — anything else is answer body.
const STATUS_LINE_RE =
  /^(available|partially available|unavailable|not available)\b[^.!?[\]]*[.!?]?$/i;
const RESULT_SUMMARY_HEADING_RE = /#{1,3}\s*Result Summary/i;

export type AnswerSection = {
  /** Short availability phrase from the marker line, e.g. "available". */
  status: string | null;
  /** Markdown body that follows the availability line. */
  body: string;
};

export type ParsedAnswer = {
  /** Content before the first marker, with the "## Result Summary" heading removed. */
  preamble: string;
  internal: AnswerSection | null;
  external: AnswerSection | null;
};

function toSection(raw: string): AnswerSection {
  const trimmed = raw.trim();
  const newlineIndex = trimmed.indexOf("\n");
  const firstLine =
    newlineIndex === -1 ? trimmed : trimmed.slice(0, newlineIndex).trim();
  // The mandated format puts a short availability phrase on the marker line
  // and the substantive answer on the following lines. If the model folded
  // answer text into the marker line instead, keep it all as body so real
  // content never renders as the muted status chip.
  if (!STATUS_LINE_RE.test(firstLine)) {
    return { status: null, body: trimmed };
  }
  const body = newlineIndex === -1 ? "" : trimmed.slice(newlineIndex + 1).trim();
  return { status: firstLine.replace(/[.\s]+$/, "") || null, body };
}

/**
 * Split a normalized answer into preamble / internal / external sections.
 * Answers without the markers (guardrail blocks, held-for-review notices,
 * partially streamed text) come back as preamble only.
 */
export function parseAnswerSections(normalized: string): ParsedAnswer {
  const internalIndex = normalized.indexOf(INTERNAL_MARKER);
  const externalIndex = normalized.indexOf(EXTERNAL_MARKER);

  if (internalIndex === -1 && externalIndex === -1) {
    // Strip the heading here too: while the first marker is still streaming
    // in, this branch renders — without the strip the "Result Summary" h2
    // would flash and then vanish once the marker completes.
    return {
      preamble: normalized.replace(RESULT_SUMMARY_HEADING_RE, "").trim(),
      internal: null,
      external: null
    };
  }

  const markerIndexes = [internalIndex, externalIndex].filter(
    (index) => index !== -1
  );
  const firstMarker = Math.min(...markerIndexes);

  const preamble = normalized
    .slice(0, firstMarker)
    .replace(RESULT_SUMMARY_HEADING_RE, "")
    .trim();

  let internal: AnswerSection | null = null;
  let external: AnswerSection | null = null;

  if (internalIndex !== -1) {
    const end =
      externalIndex > internalIndex ? externalIndex : normalized.length;
    internal = toSection(
      normalized.slice(internalIndex + INTERNAL_MARKER.length, end)
    );
  }
  if (externalIndex !== -1) {
    const end =
      internalIndex > externalIndex ? internalIndex : normalized.length;
    external = toSection(
      normalized.slice(externalIndex + EXTERNAL_MARKER.length, end)
    );
  }

  return { preamble, internal, external };
}

export function sourceDocumentUrl(sourcePath: string) {
  const url = `/api/documents/${sourcePath
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/")}`;
  return sourcePath.toLowerCase().endsWith(".pdf") ? `${url}${PDF_VIEW_QUERY}` : url;
}

export function documentViewUrl(document: CorpusDocument) {
  return document.file_type === "pdf" ? `${document.url}${PDF_VIEW_QUERY}` : document.url;
}
