"""Loader package — registers format handlers as a side effect of import.

Importing this package populates ``loaders.registry.REGISTRY`` with every
built-in format handler. The ingestion pipeline only needs ``import loaders``
to enable dispatch.
"""

from . import (  # noqa: F401  (side-effect: register handlers)
    pdf,
    markdown,
    text,
    docx,
    excel,
    html,
    csv,
    pptx,
    email_doc,
    json_doc,
)
from .registry import (
    FormatHandler,
    handler_for,
    register,
    supported_extensions,
)

__all__ = [
    "FormatHandler",
    "handler_for",
    "register",
    "supported_extensions",
]
