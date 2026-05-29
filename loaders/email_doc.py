"""Email format handler (.eml / .msg).

``.eml`` uses the stdlib ``email`` package with ``policy.default`` so RFC-5322
parsing comes for free. ``.msg`` uses ``extract_msg`` (Outlook MAPI binary
format). Both routes converge on the same shape: one ``Document`` per message
with headers in metadata and the body as ``page_content``. Attachment
*filenames* are surfaced in ``attachments`` metadata — content is not parsed
(documented limitation, deferred to a future sprint).

File name ``email_doc.py`` mirrors ``json_doc.py`` and avoids shadowing the
stdlib ``email`` package on the import path.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document

from .registry import FormatHandler, register


def _header(message, name: str) -> str:
    """Read a header from a stdlib ``email.message.EmailMessage`` safely."""
    value = message.get(name)
    if value is None:
        return ""
    return str(value).strip()


def _eml_body(message) -> str:
    """Prefer text/plain; fall back to text/html stripped via BS4."""
    try:
        plain_part = message.get_body(preferencelist=("plain",))
        if plain_part is not None:
            return plain_part.get_content().strip()
    except Exception:
        pass

    try:
        html_part = message.get_body(preferencelist=("html",))
        if html_part is not None:
            html_text = html_part.get_content()
            from bs4 import BeautifulSoup

            return BeautifulSoup(html_text, "lxml").get_text("\n", strip=True)
    except Exception:
        pass

    payload = message.get_payload(decode=False)
    if isinstance(payload, str):
        return payload.strip()
    return ""


def _eml_attachments(message) -> list[str]:
    names: list[str] = []
    try:
        for part in message.iter_attachments():
            filename = part.get_filename()
            if filename:
                names.append(filename)
    except Exception:
        pass
    return names


def _load_eml(path: str) -> list[Document]:
    import email
    from email import policy

    raw = Path(path).read_bytes()
    message = email.message_from_bytes(raw, policy=policy.default)

    subject = _header(message, "Subject")
    sender = _header(message, "From")
    recipient = _header(message, "To")
    sent_date = _header(message, "Date")
    body = _eml_body(message)
    attachments = _eml_attachments(message)

    header_block_lines = []
    if subject:
        header_block_lines.append(f"Subject: {subject}")
    if sender:
        header_block_lines.append(f"From: {sender}")
    if recipient:
        header_block_lines.append(f"To: {recipient}")
    if sent_date:
        header_block_lines.append(f"Date: {sent_date}")
    if attachments:
        header_block_lines.append("Attachments: " + ", ".join(attachments))
    header_block = "\n".join(header_block_lines)

    page_content = f"{header_block}\n\n{body}".strip() if header_block else body
    metadata: dict = {
        "source": path,
        "section_title": subject or Path(path).name,
    }
    if subject:
        metadata["email_subject"] = subject
    if sender:
        metadata["email_from"] = sender
    if recipient:
        metadata["email_to"] = recipient
    if sent_date:
        metadata["email_date"] = sent_date
    if attachments:
        metadata["attachments"] = attachments

    return [Document(page_content=page_content, metadata=metadata)]


def _load_msg(path: str) -> list[Document]:
    try:
        import extract_msg
    except ImportError as exc:
        raise ImportError(
            "Reading .msg files requires the 'extract-msg' package. "
            "Install it with: pip install extract-msg"
        ) from exc

    message = extract_msg.Message(path)
    try:
        subject = (message.subject or "").strip()
        sender = (message.sender or "").strip()
        recipient = (message.to or "").strip()
        sent_date = str(message.date).strip() if message.date else ""
        body = (message.body or "").strip()
        attachments = [att.longFilename or att.shortFilename for att in message.attachments]
        attachments = [name for name in attachments if name]
    finally:
        try:
            message.close()
        except Exception:
            pass

    header_block_lines = []
    if subject:
        header_block_lines.append(f"Subject: {subject}")
    if sender:
        header_block_lines.append(f"From: {sender}")
    if recipient:
        header_block_lines.append(f"To: {recipient}")
    if sent_date:
        header_block_lines.append(f"Date: {sent_date}")
    if attachments:
        header_block_lines.append("Attachments: " + ", ".join(attachments))
    header_block = "\n".join(header_block_lines)

    page_content = f"{header_block}\n\n{body}".strip() if header_block else body
    metadata: dict = {
        "source": path,
        "section_title": subject or Path(path).name,
    }
    if subject:
        metadata["email_subject"] = subject
    if sender:
        metadata["email_from"] = sender
    if recipient:
        metadata["email_to"] = recipient
    if sent_date:
        metadata["email_date"] = sent_date
    if attachments:
        metadata["attachments"] = attachments

    return [Document(page_content=page_content, metadata=metadata)]


def _load(path: str) -> list[Document]:
    suffix = Path(path).suffix.lower()
    if suffix == ".msg":
        return _load_msg(path)
    return _load_eml(path)


def _split(docs: list[Document]) -> list[Document]:
    from ingest import _recursive_splitter

    splitter = _recursive_splitter(chunk_size=900, chunk_overlap=100)
    chunks: list[Document] = []
    for doc in docs:
        for piece in splitter.split_documents([doc]):
            piece.metadata = {**doc.metadata, **piece.metadata}
            chunks.append(piece)
    return chunks


register(
    FormatHandler(
        extensions=(".eml", ".msg"),
        loader=_load,
        splitter=_split,
        format_family="text",
    )
)
