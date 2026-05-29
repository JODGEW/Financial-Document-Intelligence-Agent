"""Local retrieval and web search tools for the agent."""

import re
import threading
from functools import lru_cache
from pathlib import Path

from langchain_aws import BedrockEmbeddings
from langchain_chroma import Chroma
from langchain_core.tools import tool

import config
from ingest import _metadata_key, company_metadata_key, infer_company

_vectorstore = None
_vectorstore_lock = threading.Lock()
YEAR_RE = re.compile(r"\b(20\d{2})\b")
UNKNOWN_COMPANY_PATTERNS = (
    re.compile(
        r"\b([A-Z][A-Za-z0-9&.-]+(?:\s+[A-Z][A-Za-z0-9&.-]+){0,2})['’]s\s+"
        r"(?:latest\s+)?(?:filings?|10[- ]?[kq]|annual report|risk factors?)",
    ),
    re.compile(
        r"\b(?:for|about)\s+([A-Z][A-Za-z0-9&.-]+(?:\s+[A-Z][A-Za-z0-9&.-]+){0,2})\s+"
        r"(?:filings?|10[- ]?[kq]|annual report|risk factors?)",
    ),
)


def _get_vectorstore():
    """Return a shared Chroma vectorstore instance (thread-safe singleton)."""
    global _vectorstore
    if _vectorstore is None:
        with _vectorstore_lock:
            if _vectorstore is None:
                embeddings = BedrockEmbeddings(
                    model_id=config.EMBEDDING_MODEL_ID,
                    region_name=config.AWS_REGION,
                )
                _vectorstore = Chroma(
                    collection_name=config.CHROMA_COLLECTION,
                    persist_directory=config.CHROMA_PERSIST_DIR,
                    embedding_function=embeddings,
                )
    return _vectorstore


def get_retriever():
    """Build a Chroma retriever over the persisted collection."""
    return _get_vectorstore().as_retriever(search_kwargs={"k": config.RETRIEVAL_K})


@lru_cache(maxsize=1)
def _available_company_keys() -> set[str]:
    """Infer companies present in the local docs directory."""
    companies = set()
    docs_dir = Path(config.DOCS_DIR)
    if not docs_dir.exists():
        return companies

    for path in docs_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".md", ".pdf", ".txt"}:
            continue
        text_hint = ""
        if path.suffix.lower() in {".md", ".txt"}:
            try:
                text_hint = path.read_text(encoding="utf-8", errors="ignore")[:4000]
            except OSError:
                text_hint = ""
        company = infer_company(path, text_hint)
        if company:
            companies.add(company_metadata_key(company))
    return companies


def _known_company_from_query(query: str) -> str | None:
    """Return a known corpus company key mentioned in the user query."""
    normalized_query = _metadata_key(query)
    for company_key in _available_company_keys():
        aliases = {
            company_key,
            company_key.replace(" corporation", " corp"),
            company_key.replace(" corp", " corporation"),
            company_key.split()[0],
        }
        if any(re.search(rf"\b{re.escape(alias)}\b", normalized_query) for alias in aliases if alias):
            return company_key
    return None


def _unknown_company_from_query(query: str) -> str | None:
    """Infer an explicitly requested company that is not in the local corpus."""
    for pattern in UNKNOWN_COMPANY_PATTERNS:
        match = pattern.search(query)
        if match:
            candidate = _metadata_key(match.group(1))
            if candidate and candidate not in {"what", "which", "tell", "summarize"}:
                return company_metadata_key(candidate)
    return None


def _filing_type_from_query(query: str) -> str | None:
    """Infer one unambiguous document type from a query."""
    query_lower = query.lower()
    detected = set()
    for label, probes in {
        "10-k": (r"\b10[- ]?k\b", r"annual report"),
        "10-q": (r"\b10[- ]?q\b", r"quarterly report"),
        "8-k": (r"\b8[- ]?k\b", r"current report"),
        "policy": (r"\bpolicy\b",),
        "research_note": (r"\bresearch note\b",),
    }.items():
        if any(re.search(probe, query_lower) for probe in probes):
            detected.add(label)
    return next(iter(detected)) if len(detected) == 1 else None


def _year_from_query(query: str) -> int | None:
    """Return a single explicit year from the query."""
    years = {int(match.group(1)) for match in YEAR_RE.finditer(query)}
    return next(iter(years)) if len(years) == 1 else None


def _where_filter(conditions: dict) -> dict | None:
    """Build a Chroma metadata filter from equality conditions."""
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions
    return {"$and": [{key: value} for key, value in conditions.items()]}


def _metadata_filter_from_query(query: str) -> dict | None:
    """Infer a conservative Chroma metadata filter from the user query."""
    conditions = {}

    company_key = _known_company_from_query(query) or _unknown_company_from_query(query)
    if company_key:
        conditions["company_key"] = company_key

    filing_type = _filing_type_from_query(query)
    if filing_type:
        conditions["filing_type"] = filing_type

    year = _year_from_query(query)
    if year:
        conditions["year"] = year

    return _where_filter(conditions)


def _has_unknown_company_filter(where_filter: dict | None) -> bool:
    """Return whether the query asks for a company absent from the local corpus."""
    if not where_filter:
        return False
    filters = where_filter.get("$and", [where_filter])
    for condition in filters:
        company_key = condition.get("company_key")
        if company_key and company_key not in _available_company_keys():
            return True
    return False


def _get_retriever_with_filter(where_filter: dict | None):
    search_kwargs = {"k": config.RETRIEVAL_K}
    if where_filter:
        search_kwargs["filter"] = where_filter
    return _get_vectorstore().as_retriever(search_kwargs=search_kwargs)


def _retrieve_documents(query: str):
    """Retrieve documents with metadata filters when available."""
    where_filter = _metadata_filter_from_query(query)
    if not where_filter:
        return get_retriever().invoke(query)

    docs = _get_retriever_with_filter(where_filter).invoke(query)
    if docs or _has_unknown_company_filter(where_filter):
        return docs

    # Allow older local Chroma stores to keep working until the user re-ingests.
    return get_retriever().invoke(query)


@tool
def local_search(query: str) -> str:
    """Search the local document corpus for relevant information.

    Use this tool as the primary source of truth. It searches internal
    financial documents, compliance policies, and filings stored in the
    local vector database. Always try this tool first before web search.
    """
    docs = _retrieve_documents(query)
    if not docs:
        return "No relevant documents found in the local corpus."

    results = []
    for i, doc in enumerate(docs, 1):
        source = doc.metadata.get("source", "unknown")
        page = doc.metadata.get("page", None)
        section = doc.metadata.get("section_title")
        company = doc.metadata.get("company")
        filing_type = doc.metadata.get("filing_type")
        year = doc.metadata.get("year")
        header = f"[Source {i}: {source}"
        if page is not None:
            header += f", page {page}"
        if section:
            header += f", section: {section}"
        if company:
            header += f", company: {company}"
        if filing_type:
            header += f", type: {filing_type}"
        if year:
            header += f", year: {year}"
        header += "]"
        results.append(f"{header}\n{doc.page_content}")

    return "\n\n---\n\n".join(results)


@tool
def web_search(query: str) -> str:
    """Search the public web for supplemental context.

    Use this tool only when the local document corpus does not contain
    the needed information. Results from this tool are EXTERNAL context
    and should be clearly labeled as such in the final answer.
    """
    if not config.TAVILY_API_KEY:
        return (
            "Web search is not available (TAVILY_API_KEY not set). "
            "Answer based on local documents only."
        )

    from datetime import datetime
    from tavily import TavilyClient

    client = TavilyClient(api_key=config.TAVILY_API_KEY)
    response = client.search(query, max_results=5, include_raw_content=False)

    def _format_date(raw: str | None) -> str:
        """Convert a date string to YYYY-MM-DD. Return None if no date provided."""
        if not raw:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(raw[:19], fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return raw  # return as-is if no format matched

    results = []
    for i, item in enumerate(response.get("results", []), 1):
        title = item.get("title", "No title")
        url = item.get("url", "")
        content = item.get("content", "")
        published = _format_date(item.get("published_date"))
        source = url.split("/")[2] if url.count("/") >= 2 else "unknown source"

        date_part = f" | {published}" if published else ""
        entry = (
            f"- [{source}]({url}){date_part} | {title} | {content}"
        )
        results.append(entry)

    if not results:
        return "No relevant web results found."

    return "\n\n---\n\n".join(results)


ALL_TOOLS = [local_search, web_search]
