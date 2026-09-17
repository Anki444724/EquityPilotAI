"""Phase 2E A2 — one gate for every external-provider runtime path.

Phase 2E A1 gated the *provider registry*: with
``AI_EXTERNAL_PROVIDERS_ENABLED=false`` the four LLM vendors are never
assembled into `ProviderRouter` rows, so nothing routed through
``complete()`` or ``stream()`` can reach them. That was the right first cut
and it is untouched here.

It was also incomplete, and the reason is architectural rather than an
oversight. The platform reaches an external AI service through five separate
doors, only one of which is the LLM registry:

    completions     ProviderRouter                  ← gated by A1
    embeddings      build_semantic_embedder         ← Jina / OpenRouter / OpenAI
    reranking       build_rerank_provider           ← Jina / Cohere / OpenAI
    reranking       build_reranker (legacy)         ← CrossEncoderReranker
    translation     LLMTranslator                   ← the router, but reached
                                                      through the language layer
    knowledge       SummaryService, TemporalMemoryService,
                    MemoryEnrichmentService         ← the router again
    observability   GET /ai/health                  ← a one-token probe per
                                                      provider

A1 closed the first door. This module is the single predicate the remaining
doors consult, so "no external AI provider is reachable" becomes one
property of the deployment rather than seven separate ones that can drift.

**Why a helper rather than seven `getattr` calls.** Because the default has
to be right in every one of them. `getattr(settings, name, True)` appears
easy to get right until a settings object arrives that predates the flag — a
`SimpleNamespace` in a test, a stub in a benchmark — and then the default is
the whole behaviour. Reading it in one place means the safe default is
stated once, tested once, and cannot be typo'd into a fail-closed default
that would silently disable production AI.

**Default is True.** An absent setting means "this deployment has not opted
out", which must preserve the behaviour that existed before the flag was
introduced. Failing closed on a missing attribute would turn a benign
configuration gap into a platform-wide outage.

**What the gate does NOT do.** It removes no provider class, uninstalls no
dependency, unsets no credential and edits no registry. Flipping the flag
back to true restores every path exactly as it was, which is what makes the
whole of Phase 2E reversible while it is being staged.
"""
from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: The single setting the gate reads. Named once so a rename is one edit.
SETTING_NAME = "AI_EXTERNAL_PROVIDERS_ENABLED"

#: Strings that mean "off" when the value arrives as text rather than as a
#: bool. Pydantic coerces the real `Settings` field to a bool, but stub
#: settings objects built from `os.environ` in tests do not, and `"false"` is
#: truthy in Python — the exact inversion that would make the flag do the
#: opposite of what an operator typed.
_FALSY_TEXT = frozenset({"", "0", "false", "f", "no", "n", "off"})


class ExternalProvidersDisabled(RuntimeError):
    """Raised by a runtime path that would have called an external provider.

    A `RuntimeError` subclass on purpose, for two reasons. Every caller that
    already survives a provider outage catches `Exception` — the summariser
    records the failure against one summary and continues with the rest, the
    temporal series rolls back one year and continues with the next — so a
    disabled deployment degrades along the paths that already exist rather
    than needing new ones. And a distinct type lets a test assert *why*
    something failed instead of pattern-matching a message, which is the
    difference between a test that documents the contract and one that
    breaks when the wording is improved.
    """


def external_providers_enabled(settings: Any | None = None) -> bool:
    """Whether external AI providers may be reached at all.

    Args:
        settings: Any settings-like object. ``None`` resolves the
            application singleton lazily, so importing this module never
            drags `app.core.config` — and therefore the whole provider
            stack — into a module that only needs the predicate.

    Returns:
        ``True`` when external providers may be used. ``True`` is also the
        answer for a settings object with no such attribute, an attribute
        set to ``None``, or a value this function cannot interpret: the
        safe default is the behaviour that predates the flag.
    """
    if settings is None:
        from app.core.config import settings as app_settings

        settings = app_settings

    value = getattr(settings, SETTING_NAME, None)
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY_TEXT
    return bool(value)


def gate_detail(scope: str) -> str:
    """The honest sentence a caller should surface when the gate is closed.

    Every path the gate closes already has somewhere to put an explanation —
    a `TranslationResult.detail`, a `StageOutcome.detail`, an enrichment
    error row, an exception message an operator reads in a log. What it must
    not contain is a guess. Naming the setting and its value tells the reader
    that this is configuration rather than a failure, and what to change.

    Args:
        scope: What was declined, in the caller's own words — "translation",
            "permanent summaries", "jina reranking". Kept free-form because
            the alternative is an enum that grows a member per call site.
    """
    return (
        f"{scope} declined: external AI providers are disabled "
        f"({SETTING_NAME}=false). Set {SETTING_NAME}=true to re-enable."
    )
