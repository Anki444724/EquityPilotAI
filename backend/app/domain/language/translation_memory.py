"""Translation memory for repeated phrase consistency (Phase 2)."""
from __future__ import annotations


class TranslationMemory:
    """Shared memory for repeated translated phrases."""

    def apply(self, text: str, language) -> str:
        return text


def get_translation_memory() -> TranslationMemory:
    return TranslationMemory()


def apply_translation_memory(text: str, language) -> str:
    return get_translation_memory().apply(text, language)
