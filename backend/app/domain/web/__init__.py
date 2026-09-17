"""Web evidence domain types — Part 3 Phase 1.

Pure, dependency-free types describing what the platform may fetch, what it
refuses to fetch, and what a fetched page is once it has been accepted.
Nothing here performs I/O or knows about HTTP.
"""
from __future__ import annotations

from .types import (
    ACCEPTED_CONTENT_TYPES,
    CITATION_KEY_PATTERN,
    WebContentClass,
    WebDocumentRef,
    WebFetchPolicy,
    WebRejectionReason,
    WebSearchQuery,
    WebSearchResult,
    WebSourceClass,
    content_class_for,
)

__all__ = [
    "ACCEPTED_CONTENT_TYPES", "CITATION_KEY_PATTERN", "WebContentClass",
    "WebDocumentRef", "WebFetchPolicy", "WebRejectionReason", "WebSearchQuery",
    "WebSearchResult", "WebSourceClass", "content_class_for",
]
