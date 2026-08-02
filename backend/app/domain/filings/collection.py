"""Domain rules for automated filing collection.

Pure decision logic, no I/O. What counts as a document worth collecting, how a
title maps to a document type, which fiscal period a filing belongs to, and
how often a company should be revisited. Keeping this separate is what lets
the crawl be tested without touching NSE.

Three judgements live here and are worth stating plainly.

**Classification is by title, and titles are written by humans.** NSE
announcement titles are free text — "Outcome of Board Meeting", "Analysts/
Institutional Investor Meet/Con. Call Updates". Matching them is inherently
approximate, so every classification carries a confidence and an unmatched
title becomes `OTHER` rather than being forced into a category it does not
belong to. A misfiled annual report is worse than an unfiled one: it would be
cited as an annual report in a research answer.

**Not every announcement is worth storing.** An exchange emits hundreds of
procedural notices — trading window closures, share transfer intimations —
that cost storage and dilute retrieval without informing an analyst. The
`NOISE_PATTERNS` set is the filter, and it is deliberately conservative:
excluding something genuinely useful is a worse error than keeping noise.

**Revisit cadence is tiered.** Crawling 135 companies against a rate-limited
exchange every night is roughly forty minutes of sequential work before any
download. Tiering keeps the nightly run bounded while still covering the whole
universe within a week.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum

from app.data.filings.base import FilingType
from app.domain.documents.types import DocumentType


class CollectionTier(StrEnum):
    """How often a company is revisited."""

    DAILY = "daily"
    WEEKLY = "weekly"
    PAUSED = "paused"


#: Seconds between revisits for each tier.
TIER_INTERVAL_SECONDS: dict[CollectionTier, int] = {
    CollectionTier.DAILY: 24 * 3600,
    CollectionTier.WEEKLY: 7 * 24 * 3600,
    # Paused companies are never due; the value exists so arithmetic on the
    # mapping never raises.
    CollectionTier.PAUSED: 10 ** 9,
}


class CollectionStatus(StrEnum):
    """Lifecycle of one discovered document."""

    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    PROCESSING = "processing"
    EMBEDDING = "embedding"
    COMPLETED = "completed"
    FAILED = "failed"
    #: Already held under the same SHA256. Not an error — the expected
    #: outcome for most of what a daily crawl sees.
    DUPLICATE = "duplicate"
    #: Deliberately not collected: procedural noise.
    SKIPPED = "skipped"


#: Statuses that will not change without intervention.
TERMINAL_STATUSES: frozenset[CollectionStatus] = frozenset({
    CollectionStatus.COMPLETED, CollectionStatus.DUPLICATE,
    CollectionStatus.SKIPPED, CollectionStatus.FAILED,
})


@dataclass(frozen=True, slots=True)
class Classification:
    """What a document is, and how sure we are."""

    filing_type: FilingType
    doc_type: DocumentType
    confidence: float
    matched_on: str = ""

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.6


#: Title patterns, most specific first. Order matters: "annual report" must be
#: tested before the generic "report", and a transcript before a conference
#: call intimation, because the first match wins.
#:
#: Each entry is (regex, FilingType, DocumentType, confidence).
_RULES: tuple[tuple[str, FilingType, DocumentType, float], ...] = (
    # Separator class rather than \s+: a URL spells it "annual-report" or
    # "annual_report", and requiring whitespace silently misses every filing
    # discovered by URL rather than by anchor text.
    (r"\bannual[\s_-]+report\b|\bintegrated[\s_-]+(annual[\s_-]+)?report\b",
     FilingType.ANNUAL_REPORT, DocumentType.ANNUAL_REPORT, 0.95),
    (r"\bbusiness\s+responsibility\b|\bbrsr\b|\bsustainab|\besg\b",
     FilingType.OTHER, DocumentType.ESG_REPORT, 0.90),
    (r"\btranscript\b",
     FilingType.OTHER, DocumentType.CONFERENCE_CALL, 0.92),
    (r"\b(earnings|analyst|investor)\s+(call|meet)\b|\bcon\.?\s*call\b|"
     r"\bconference\s+call\b",
     FilingType.OTHER, DocumentType.CONFERENCE_CALL, 0.75),
    (r"\binvestor\s+(presentation|deck)\b|\bearnings\s+presentation\b|"
     r"\bcorporate\s+presentation\b",
     FilingType.INVESTOR_PRESENTATION, DocumentType.INVESTOR_PRESENTATION, 0.92),
    (r"\b(financial\s+)?results?\b|\bunaudited\b|\baudited\b|\bquarterly\b|"
     r"\bq[1-4]\s*fy\b",
     FilingType.QUARTERLY_RESULTS, DocumentType.QUARTERLY_REPORT, 0.85),
    (r"\bcredit\s+rating\b|\brating\s+(action|update|revision)\b|"
     r"\b(crisil|icra|care\s+ratings|india\s+ratings)\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.CREDIT_RATING, 0.90),
    (r"\bpress\s+release\b|\bmedia\s+release\b",
     FilingType.PRESS_RELEASE, DocumentType.EXCHANGE_FILING, 0.85),
    (r"\bboard\s+meeting\b|\boutcome\s+of\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.EXCHANGE_FILING, 0.80),
    (r"\bdividend\b", FilingType.CORPORATE_ANNOUNCEMENT,
     DocumentType.EXCHANGE_FILING, 0.85),
    (r"\bbonus\s+issue\b|\bstock\s+split\b|\bsub-?division\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.EXCHANGE_FILING, 0.88),
    (r"\bacquisition\b|\bamalgamation\b|\bmerger\b|\bscheme\s+of\s+arrangement\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.EXCHANGE_FILING, 0.88),
    (r"\border\s+win\b|\bnew\s+order\b|\bcontract\s+win\b|\bbags?\s+order\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.EXCHANGE_FILING, 0.85),
    (r"\bshareholding\s+pattern\b|\breg(ulation)?\.?\s*31\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.SHAREHOLDING, 0.92),

    # ---------------------------------------------------------------
    # Rules below were derived from the ACTUAL NSE subject lines in the
    # production corpus, not invented. Before they existed, 1,506 of 2,902
    # discovered filings classified as `other` and 420 as NULL — 66% of the
    # corpus carried no usable type.
    #
    # NSE reuses a small set of fixed subject strings, so a handful of exact
    # rules converts most of that tail. Each pattern below is annotated with
    # the count it addresses, measured on 2026-08-02.
    # ---------------------------------------------------------------

    # 452 rows. NSE's standing subject for an earnings-call intimation. The
    # generic conference-call rule above misses it because the string is
    # "Con. Call" with a full stop inside the abbreviation and "Analysts/"
    # glued to the front with no word boundary.
    (r"analysts?\s*/\s*institutional\s+investor|\bcon\.\s*call\b",
     FilingType.OTHER, DocumentType.CONFERENCE_CALL, 0.80),

    # 173 rows. An AGM/EGM notice carries the resolutions put to owners.
    (r"\bshareholders?\s+meeting\b|\bpostal\s+ballot\b|\b(a|e)gm\b|"
     r"\bannual\s+general\s+meeting\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.EXCHANGE_FILING, 0.80),

    # 93 rows. Substantial-acquisition disclosures move the ownership table.
    (r"\btakeover\s+regulations?\b|\bsast\b|"
     r"\bsubstantial\s+acquisition\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.SHAREHOLDING, 0.85),

    # 65 + 51 + 30 + 27 + 25 + 19 + 9 + 7 rows. Board and KMP changes.
    (r"\bchange\s+in\s+(management|director|auditor|kmp)|"
     r"\bresignation\b|\bcessation\b|\bretirement\b|\bappointment\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.EXCHANGE_FILING, 0.82),

    # 59 + 52 + 33 + 13 rows. Capital and record-date actions.
    (r"\brecord\s+date\b|\besop\b|\besos\b|\besps\b|"
     r"\ballotment\s+of\s+securities\b|\bissue\s+of\s+securities\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.EXCHANGE_FILING, 0.82),

    # 18 + 9 rows. Regulatory action and litigation are material risk events.
    (r"\baction\(?s\)?\s+taken\b|\borders?\s+passed\b|"
     r"\bpendency\s+of\s+litigation\b|\bdispute\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.EXCHANGE_FILING, 0.80),

    # Last resort, and deliberately last: NSE files a great deal under bare
    # "Updates" / "General Updates" (333 + 235 rows). These are genuinely
    # heterogeneous, so they are typed as an exchange filing at LOW
    # confidence rather than left null. A reader can see 0.45 and treat it
    # accordingly; a null tells them nothing at all.
    (r"^\s*(general\s+)?updates?\s*$|\bgeneral\s+updates?\b",
     FilingType.CORPORATE_ANNOUNCEMENT, DocumentType.EXCHANGE_FILING, 0.45),
)

_COMPILED = tuple(
    (re.compile(pattern, re.IGNORECASE), ft, dt, conf)
    for pattern, ft, dt, conf in _RULES
)

#: Procedural notices with no analytical content.
#:
#: Conservative by design — a false exclusion silently loses a document, which
#: is far harder to notice than a stored notice nobody reads.
_NOISE_PATTERNS = (
    r"\btrading\s+window\b",
    r"\bclosure\s+of\s+trading\s+window\b",
    r"\bshare\s+transfer\b",
    r"\bloss\s+of\s+share\s+certificate\b",
    r"\bduplicate\s+share\s+certificate\b",
    r"\bnewspaper\s+(publication|advertisement)\b",
    r"\bcompliance\s+certificate\b",
    r"\breg\.?\s*7\(3\)\b",
    r"\binvestor\s+(complaints|grievance)\b",
    r"\bunclaimed\s+(dividend|shares)\b",
    r"\biepf\b",
)
_NOISE = tuple(re.compile(p, re.IGNORECASE) for p in _NOISE_PATTERNS)


def classify(title: str, *, url: str | None = None) -> Classification:
    """What kind of document this title describes.

    Falls through to `OTHER` at low confidence rather than guessing. A
    document filed as the wrong type is quoted as the wrong type in a research
    answer, which is a correctness failure rather than a tidiness one.
    """
    haystack = f"{title or ''} {url or ''}"
    for pattern, filing_type, doc_type, confidence in _COMPILED:
        match = pattern.search(haystack)
        if match:
            return Classification(filing_type, doc_type, confidence,
                                  matched_on=match.group(0))
    return Classification(FilingType.OTHER, DocumentType.OTHER, 0.30)


def is_noise(title: str) -> bool:
    """True for procedural notices not worth storing."""
    text = title or ""
    return any(p.search(text) for p in _NOISE)


#: Indian fiscal years run April–March, so a filing published in, say, May
#: 2026 usually reports FY2026 (ending 31 March 2026) rather than FY2027.
FY_START_MONTH = 4


def fiscal_year_for(published: date | None, title: str = "") -> int | None:
    """The fiscal year a filing relates to.

    An explicit year in the title wins — "Annual Report 2024-25" is
    unambiguous and the publication date is not. Failing that, the date is
    mapped through the Indian April–March convention.
    """
    match = re.search(r"\b(20\d{2})\s*[-–/]\s*(\d{2,4})\b", title or "")
    if match:
        end = match.group(2)
        return int(end) if len(end) == 4 else int(match.group(1)[:2] + end)

    match = re.search(r"\bFY\s*(20\d{2})\b", title or "", re.IGNORECASE)
    if match:
        return int(match.group(1))

    if published is None:
        return None
    # Before April the filing still belongs to the fiscal year that ends in
    # the current calendar year; from April it belongs to the next.
    return published.year + 1 if published.month >= FY_START_MONTH else published.year


def quarter_for(published: date | None, title: str = "") -> str | None:
    """Q1–Q4 where the filing names one, otherwise inferred from the date."""
    match = re.search(r"\bQ([1-4])\b", title or "", re.IGNORECASE)
    if match:
        return f"Q{match.group(1)}"
    if re.search(r"\bhalf[- ]year|\bh1\b", title or "", re.IGNORECASE):
        return "H1"
    if published is None:
        return None
    # April–June is Q1 of the Indian fiscal year.
    index = ((published.month - FY_START_MONTH) % 12) // 3 + 1
    return f"Q{index}"


def due_for_crawl(
    tier: CollectionTier,
    last_crawled_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Should this company be visited on this pass?"""
    if tier is CollectionTier.PAUSED:
        return False
    if last_crawled_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    if last_crawled_at.tzinfo is None:
        last_crawled_at = last_crawled_at.replace(tzinfo=timezone.utc)
    interval = timedelta(seconds=TIER_INTERVAL_SECONDS[tier])
    return now - last_crawled_at >= interval


#: A discovered document larger than this is not downloaded automatically.
#:
#: Some annual reports genuinely exceed 100 MB, and one of them can exhaust a
#: 500 MB volume. Oversized documents are recorded as discovered and left for
#: an operator, which is visible in the dashboard rather than silently absent.
MAX_AUTO_DOWNLOAD_BYTES = 60 * 1024 * 1024

#: Retry budget for a download that fails transiently.
MAX_DOWNLOAD_ATTEMPTS = 3


def should_retry(attempts: int, status: CollectionStatus) -> bool:
    return (
        status is CollectionStatus.FAILED
        and attempts < MAX_DOWNLOAD_ATTEMPTS
    )
