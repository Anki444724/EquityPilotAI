"""How much a fetched page counts for, and whether it counts at all.

Two separate judgements, kept apart because they fail differently:

* **Classification** — which kind of source this is. Deterministic, derived
  from the pinned host and the company's own records, never from the page's
  own claims about itself.
* **Acceptance** — whether the bytes are usable evidence at all. A cookie
  banner, a "JavaScript required" notice and a 200-with-an-empty-shell are all
  pages the fetcher retrieved successfully and that mean nothing.

The ranking rule, stated once
-----------------------------
**Every web class sits below every filing class.** The lowest filing tier is
``other`` at 0.40, so the highest web class is 0.38. A page the platform
fetched is not a document the company lodged, and no amount of it being on the
company's own domain changes that. The ordering *within* the web tier is:

``regulator > exchange > verified IR > company website > media > unknown``

Regulator and exchange pages rank above the company's own pages because they
are the venue's record rather than the company's account of itself; the
company's own site and its verified IR page are treated as *primary* sources
about what the company has said, which is a weaker claim than being evidence
of what it filed.

Two prohibitions this module exists to keep
-------------------------------------------
* **Nothing here is ever added to** ``TRUSTED_SOURCES``
  (:mod:`app.domain.valuation.data_quality`). That set certifies
  financial-statement provenance for the valuation layer; admitting a fetched
  page to it would let a company's marketing page make a valuation
  investment-grade. ``tests/test_web_quality.py`` asserts the two sets stay
  disjoint.
* **No second scorer.** Recency comes from
  :func:`app.data.filings.base.recency_factor`, the same function the filings
  layer uses, so a two-year-old web page and a two-year-old filing age at the
  same rate. This module owns only the base weight.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime

from app.domain.web.types import (
    WebContentClass,
    WebFetchPolicy,
    WebRejectionReason,
    WebSourceClass,
)

#: Exchange hosts, matching the ones the filing providers already read
#: (``app/data/filings/indian.py``, ``sec.py``). A test asserts every host
#: here still appears in those modules, so the two lists cannot drift apart
#: without the suite saying so.
EXCHANGE_HOSTS: frozenset[str] = frozenset({
    "www.nseindia.com",
    "www.bseindia.com",
    "api.bseindia.com",
})

#: Regulator and government filing hosts, from the same provider modules.
REGULATOR_HOSTS: frozenset[str] = frozenset({
    "www.sec.gov",
    "data.sec.gov",
})

#: Recognised financial media. Deliberately empty in Phase 1: no media host is
#: pinned, and inventing a list of "reputable" outlets is a judgement this
#: layer has no basis for. The class exists so the ordering is complete and so
#: a Phase-2 allowlist has somewhere to land.
MEDIA_HOSTS: frozenset[str] = frozenset()

#: The confidence at or above which a discovered IR URL counts as verified.
#: Mirrors ``app.services.filings.ir_discovery.CONFIDENCE_VERIFIED`` (0.90 —
#: fetched and confirmed with a 200). Mirrored rather than imported because
#: that module pulls in SQLAlchemy models, and ``tests/test_web_quality.py``
#: asserts the two stay equal so the mirror cannot silently drift.
IR_URL_VERIFIED_CONFIDENCE = 0.90

#: Base weight per web class, before recency. Bounded above by
#: :data:`_LOWEST_FILING_AUTHORITY` and asserted below.
WEB_AUTHORITY: dict[WebSourceClass, float] = {
    WebSourceClass.REGULATOR: 0.38,
    WebSourceClass.EXCHANGE: 0.36,
    WebSourceClass.VERIFIED_IR: 0.34,
    WebSourceClass.COMPANY_WEBSITE: 0.32,
    WebSourceClass.REPUTABLE_MEDIA: 0.24,
    WebSourceClass.UNKNOWN: 0.15,
}

#: The authority the vault assigns a document type it does not recognise —
#: ``other``, the lowest *filing* tier in
#: :data:`app.domain.knowledge.vault.SOURCE_AUTHORITY`. Hardcoded here so this
#: module stays free of the knowledge package, and asserted against the live
#: table in the tests.
_LOWEST_FILING_AUTHORITY = 0.40

assert max(WEB_AUTHORITY.values()) < _LOWEST_FILING_AUTHORITY, (
    "a web source class is ranked at or above a filing class — web evidence "
    "must never outrank a filed document"
)

#: Classes that speak for the company or the venue itself. Used by callers
#: that need to know whether a page is a primary account or commentary.
_PRIMARY_CLASSES = frozenset({
    WebSourceClass.COMPANY_WEBSITE,
    WebSourceClass.VERIFIED_IR,
    WebSourceClass.EXCHANGE,
    WebSourceClass.REGULATOR,
})

#: Markers of a page that is not content: a consent wall, a bot notice, an
#: error page produced with a 200 status. Matched case-insensitively.
_REJECT_MARKERS = re.compile(
    r"(accept (?:all )?cookies|we use cookies|enable javascript|"
    r"javascript is (?:required|disabled)|verify you are human|"
    r"are you a robot|captcha|access denied|403 forbidden|"
    r"page not found|404 not found|just a moment\.\.\.|"
    r"checking your browser|enable cookies to continue)",
    re.IGNORECASE,
)

#: Characters that carry no meaning in extracted text and would otherwise
#: become part of a citation value.
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_WHITESPACE = re.compile(r"\s+")


def classify(
    host: str,
    *,
    company_hosts: frozenset[str] = frozenset(),
    ir_hosts: frozenset[str] = frozenset(),
    media_hosts: frozenset[str] = MEDIA_HOSTS,
) -> WebSourceClass:
    """Classify a host by role, most authoritative first.

    ``company_hosts`` and ``ir_hosts`` come from the company's own row —
    ``Company.website`` and a *verified* ``CompanyCrawlState.ir_url``. The
    page's own claims are not consulted: a page can call itself anything.
    """
    candidate = (host or "").strip().lower().lstrip(".")
    if not candidate:
        return WebSourceClass.UNKNOWN
    if candidate in REGULATOR_HOSTS:
        return WebSourceClass.REGULATOR
    if candidate in EXCHANGE_HOSTS:
        return WebSourceClass.EXCHANGE
    if candidate in {h.lower().lstrip(".") for h in ir_hosts}:
        return WebSourceClass.VERIFIED_IR
    if candidate in {h.lower().lstrip(".") for h in company_hosts}:
        return WebSourceClass.COMPANY_WEBSITE
    if candidate in {h.lower().lstrip(".") for h in media_hosts}:
        return WebSourceClass.REPUTABLE_MEDIA
    return WebSourceClass.UNKNOWN


def is_primary_source(source_class: WebSourceClass) -> bool:
    """Whether the class is the company or the venue speaking."""
    return source_class in _PRIMARY_CLASSES


def host_is_pinned_by(host: str, allowed: frozenset[str]) -> bool:
    """Whether a host is on a pinned allowlist at a label boundary.

    ``www.tcs.com`` is under ``tcs.com``; ``eviltcs.com`` is not. The same
    rule the safety layer enforces, applied here to the classification lists
    so a caller sees one answer to "is this host ours" rather than two.
    """
    candidate = (host or "").strip().lower().rstrip(".")
    if not candidate:
        return False
    normalised = {h.strip().lower().lstrip(".") for h in allowed if h}
    return candidate in normalised or any(
        candidate.endswith(f".{pinned}") for pinned in normalised
    )


def web_authority(
    source_class: WebSourceClass, *, published_at: datetime | date | None = None,
) -> float:
    """Base weight for a class, discounted by the platform's own recency.

    The recency factor is imported from the filings layer rather than
    reimplemented — the platform should age a web page exactly as it ages a
    filing, and two curves would eventually disagree about which of two
    sources is stale.
    """
    base = WEB_AUTHORITY.get(
        source_class, WEB_AUTHORITY[WebSourceClass.UNKNOWN]
    )
    filed: date | None
    if isinstance(published_at, datetime):
        filed = published_at.date()
    else:
        filed = published_at
    try:
        from app.data.filings.base import recency_factor

        factor = recency_factor(filed)
    except Exception:  # noqa: BLE001 - a weight must never break a fetch
        factor = 0.85 if filed is None else 1.0
    return round(base * factor, 4)


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    """Whether a fetched page is usable evidence, and why not if it is not."""

    accepted: bool
    reason: WebRejectionReason | None = None
    detail: str = ""

    @property
    def rejected(self) -> bool:
        return not self.accepted


def assess(
    text: str, *, policy: WebFetchPolicy | None = None,
    content_class: WebContentClass = WebContentClass.HTML,
) -> QualityAssessment:
    """Decide whether extracted text is evidence or an artefact.

    The order is deliberate: an empty extraction is reported as such, because
    "the page was a shell" and "the page was too short" lead an operator to
    different fixes.
    """
    settings = policy or WebFetchPolicy()
    collapsed = _WHITESPACE.sub(" ", _ZERO_WIDTH.sub("", text or "")).strip()

    if not collapsed:
        return QualityAssessment(
            accepted=False, reason=WebRejectionReason.EXTRACTION_EMPTY,
            detail=f"no text could be extracted from the {content_class.value} body",
        )

    # Walls are checked before the length floor so that the reason names what
    # the page actually is. "A consent banner" and "a stub" lead an operator to
    # different fixes, and the floor would otherwise claim every short wall.
    marker = _REJECT_MARKERS.search(collapsed)
    if marker and len(collapsed) < settings.min_content_chars * 20:
        # The length clause is what separates a wall from an article *about*
        # consent: a long page that merely mentions cookies is content.
        return QualityAssessment(
            accepted=False, reason=WebRejectionReason.UNSUPPORTED_CONTENT,
            detail=f"page is a wall, not content (matched {marker.group(0)!r})",
        )

    if len(collapsed) < settings.min_content_chars:
        return QualityAssessment(
            accepted=False, reason=WebRejectionReason.CONTENT_TOO_SHORT,
            detail=(
                f"{len(collapsed)} characters is below the "
                f"{settings.min_content_chars}-character floor"
            ),
        )

    return QualityAssessment(accepted=True)


def near_duplicate_key(text: str, *, limit: int = 20_000) -> str:
    """A key that groups pages differing only in case and punctuation.

    Exact duplicates are already handled by ``content_hash`` at ingestion
    (the same bytes are one document per company). This catches the
    *near*-duplicates that hash comparison cannot: the same article served
    with different whitespace, a stray cookie line, or curly versus straight
    quotes. It is a normalisation, not a similarity measure — a paraphrase is
    a different page and is meant to be kept.
    """
    normalised = re.sub(r"[^a-z0-9]+", "", (text or "").lower())[:limit]
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()
