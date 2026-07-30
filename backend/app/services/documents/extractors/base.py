"""Parser contract and registry.

Every format parser returns the same :class:`ParsedDocument`. Nothing above
this layer branches on file type — the lesson Module 6 learned the hard way
with LLM providers, applied to file formats.

Registration is by declared :class:`FileFormat`, not by inspecting class names,
so adding a format is one new module plus one decorator.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, ClassVar

from app.domain.documents.types import (
    EXTENSION_MAP, FileFormat, ParsedDocument, UnsupportedFormat,
)


class DocumentParser(ABC):
    """Turns raw bytes into pages, blocks and tables. No interpretation."""

    #: Formats this parser claims. Declared, never inferred.
    formats: ClassVar[tuple[FileFormat, ...]] = ()

    @abstractmethod
    def parse(self, payload: bytes, *, filename: str = "") -> ParsedDocument:
        """Parse ``payload``. Raise :class:`ParseFailure` if unreadable."""

    # -- shared helpers -----------------------------------------------
    @staticmethod
    def format_for(filename: str) -> FileFormat:
        """Resolve a filename to a supported format, or refuse it."""
        lowered = filename.lower()
        for extension, fmt in EXTENSION_MAP.items():
            if lowered.endswith(extension):
                return fmt
        raise UnsupportedFormat(
            f"'{filename}' has no supported extension; "
            f"accepted: {', '.join(sorted(EXTENSION_MAP))}"
        )


_REGISTRY: dict[FileFormat, DocumentParser] = {}


def register(parser_cls: type[DocumentParser]) -> type[DocumentParser]:
    """Class decorator binding a parser to each format it declares."""
    if not parser_cls.formats:
        raise ValueError(f"{parser_cls.__name__} declares no formats")
    instance = parser_cls()
    for fmt in parser_cls.formats:
        _REGISTRY[fmt] = instance
    return parser_cls


def parser_for(fmt: FileFormat) -> DocumentParser:
    parser = _REGISTRY.get(fmt)
    if parser is None:
        raise UnsupportedFormat(f"no parser registered for '{fmt.value}'")
    return parser


def registered_formats() -> tuple[FileFormat, ...]:
    return tuple(sorted(_REGISTRY, key=lambda f: f.value))


def parse_document(payload: bytes, filename: str) -> ParsedDocument:
    """Single entry point: bytes plus a name in, :class:`ParsedDocument` out."""
    fmt = DocumentParser.format_for(filename)
    document = parser_for(fmt).parse(payload, filename=filename)
    document.file_format = fmt
    return document
