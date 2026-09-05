"""Resolve a Blogger post to the company it is about.

The rule that shapes everything here: **a wrong company is worse than no
company.** A post attached to the wrong ticker does not merely fail to help —
it becomes retrievable evidence for that company's questions forever, cited by
the analyst with a page number and a document title that make it look like
exactly the kind of thing the citation framework exists to vouch for. An
unresolved post is skipped, logged, and still on the blog where a reader can
find it.

So matching is exact or word-boundary, never substring. The failure the brief
names — "ITC" matching because those three letters appear inside another word —
is structurally impossible here: labels are compared whole, and title matches
are built from whole normalised words.

Four tiers, in the priority order the brief specifies. Each tier either
resolves, declares the post ambiguous, or says nothing and lets the next tier
try. **Ambiguous stops** rather than falling through: two ticker labels naming
different companies is a post about two companies, and letting a weaker signal
pick one of them would be a guess wearing a stronger signal's clothes.

1. **An explicit ticker in a label.** `SHRIRAMFIN`, `BEL`, `ITC`. Also a label
   whose leading words concatenate to a ticker — the live feed labels posts
   `"BEL Stock Analysis"` and `"JSW Steel Stock Analysis"`, and neither is
   *equal* to a ticker.
2. **A company name in a label**, normalised: accents folded, legal suffixes
   dropped, `&` read as "and". `"Nestlé India"` is `"Nestle India Ltd"`.
3. **The title**, matched against the same company names and against ticker
   tokens.
4. **`BLOGGER_DEFAULT_TICKER`**, if an operator set one. This is the only tier
   that can attach a post it has found no evidence for, which is why it is
   opt-in and empty by default: it is right for a blog that covers one company
   and wrong for one that covers a hundred.

Short forms that no normalisation can derive — `L&T`, `HUL`, `RIL`, `M&M`,
`Zomato` for the company now called Eternal — are listed explicitly in
:data:`CompanyMapper.ALIASES`. An alias that names a ticker absent from the
database resolves to nothing, so the list cannot invent a company.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company

log = structlog.get_logger(__name__)

#: Legal-form suffixes, dropped when normalising a name so that "Shriram
#: Finance" and "Shriram Finance Ltd" are the same key. Ordered longest-first
#: at match time so "private limited" is stripped as one suffix rather than
#: leaving "private" behind.
_LEGAL_SUFFIXES = (
    "private limited", "pvt ltd", "pvt. ltd.", "public limited",
    "limited", "ltd", "llp", "plc", "inc", "corporation", "corp",
    "company", "co",
)

#: Single-word phrases that are ordinary English as often as they are a
#: company. A one-word title match is accepted for an *identifier* — a ticker
#: or a known short form — and refused for one of these, because "Eternal
#: growth in the sector" is a sentence about growth and not a post about
#: Eternal Ltd. Multi-word mentions ("Eternal Ltd", "Titan Company") are
#: unaffected: they are matched as phrases by the name tier.
_AMBIGUOUS_WORDS = frozenset({
    "apollo", "axis", "bank", "capital", "central", "delta", "energy",
    "eternal", "finance", "financial", "first", "future", "general", "global",
    "great", "hero", "india", "indian", "life", "national", "oriental",
    "power", "smart", "standard", "steel", "sun", "titan", "union", "united",
})

#: Whole-word phrases → ticker, for the short forms normalisation cannot reach.
#:
#: Keys are already normalised (accent-folded, lower-cased, `&` read as "and"),
#: so `"l and t"` matches the label `L&T` and the title `Larsen & Toubro (L&T)`
#: alike. Values are tickers, not company ids: an entry whose ticker is not in
#: the database resolves to nothing at all, which is what keeps this list from
#: becoming a second, stale company table.
ALIASES: dict[str, str] = {
    "l and t": "LT",
    "l t": "LT",
    "larsen toubro": "LT",
    "hul": "HINDUNILVR",
    "hindustan lever": "HINDUNILVR",
    "ril": "RELIANCE",
    "m and m": "M&M",
    "mahindra and mahindra": "M&M",
    "sbi": "SBIN",
    "state bank of india": "SBIN",
    "lic": "LIC",
    "life insurance corporation of india": "LIC",
    "hcltech": "HCLTECH",
    "hcl tech": "HCLTECH",
    "hdfc bank": "HDFCBANK",
    "icici bank": "ICICIBANK",
    "adani ports": "ADANIPORTS",
    "adani ports and special economic zone": "ADANIPORTS",
    "airtel": "BHARTIARTL",
    "bharti airtel": "BHARTIARTL",
    "zomato": "ETERNAL",
    "jsw steel": "JSWSTEEL",
    "bajaj finserv": "BAJAJFINSV",
    "shriram finance": "SHRIRAMFIN",
    "nestle india": "NESTLEIND",
    "bharat electronics": "BEL",
    "oil and natural gas corporation": "ONGC",
    "tata consultancy services": "TCS",
}

#: Longest phrase, in words, considered when scanning a title or a label.
#: "Life Insurance Corporation of India" is five; six leaves room for a name
#: plus one trailing word without turning the scan quadratic in practice.
MAX_PHRASE_WORDS = 6

#: Words concatenated when testing whether a label's leading words spell a
#: ticker (`"JSW Steel Stock Analysis"` → `JSWSTEEL`).
MAX_TICKER_WORDS = 3

#: A concatenated multi-word ticker must be at least this long. Single tokens
#: are matched from length 2 (`LT`), but a concatenation that short is two
#: one-letter words, which is coincidence rather than a symbol.
MIN_CONCATENATED_TICKER_CHARS = 4


def normalise_name(value: str) -> str:
    """Canonical form of a company name, label or title phrase.

    Accent folding first, because the live feed labels a post `"Nestlé India"`
    while the database stores `"Nestle India Ltd"` — one combining acute is the
    only difference, and without folding it the match fails silently. `&`
    becomes "and" so that `Larsen & Toubro` and a written-out `Larsen and
    Toubro` agree. Everything else that is not alphanumeric becomes a space,
    which is what makes the result safe to split into whole words: no match
    this module makes can span a word boundary.
    """
    folded = unicodedata.normalize("NFKD", value or "")
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    lowered = folded.lower().replace("&", " and ")
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def strip_legal_suffix(name: str) -> str:
    """Drop trailing legal-form words from an already-normalised name."""
    cleaned = name.strip()
    changed = True
    while changed and cleaned:
        changed = False
        for suffix in sorted(_LEGAL_SUFFIXES, key=len, reverse=True):
            if cleaned == suffix:
                return ""
            if cleaned.endswith(" " + suffix):
                cleaned = cleaned[: -len(suffix) - 1].strip()
                changed = True
                break
    return cleaned


def name_keys(value: str) -> set[str]:
    """Every key a name is indexed and looked up under.

    Both the full normalised name and its suffix-stripped form, because a
    mention can be either: the database holds `"Oil & Natural Gas Corporation
    Ltd"`, a label says `"ONGC"`, and a title says `"Oil & Natural Gas
    Corporation"`. Stripping alone would lose the middle case — "corporation"
    is part of that name and not a suffix of it — and keeping both forms means
    a mention matches whichever of the two it happens to be.
    """
    full = normalise_name(value)
    if not full:
        return set()
    keys = {full}
    stripped = strip_legal_suffix(full)
    if stripped and stripped != full:
        keys.add(stripped)
    return keys


def ticker_key(value: str) -> str:
    """Canonical form of a ticker: upper-case, alphanumerics and `&` only."""
    return re.sub(r"[^A-Z0-9&]", "", (value or "").upper())


def _words_contained(short: str, long: str) -> bool:
    """Is `short` a contiguous run of words inside `long`?

    The specificity rule. `"itc"` is contained in `"itc infotech"`, so a title
    naming ITC Infotech is about ITC Infotech and not about ITC Ltd — even
    though the shorter phrase is also, literally, present. Both matched; the
    less specific one is the one that is wrong.
    """
    if short == long:
        return False
    needle = short.split()
    haystack = long.split()
    if len(needle) >= len(haystack):
        return False
    span = len(needle)
    return any(haystack[i : i + span] == needle for i in range(len(haystack) - span + 1))


def _collapse_rename_pairs(candidates: dict[str, "_Candidate"]) -> dict[str, "_Candidate"]:
    """Collapse two rows that are one company under two symbols.

    A rename leaves the old row behind: `ZOMATO` delisted, `ETERNAL` active,
    both named *"Eternal Ltd"*. A post can reach both at once — the live label
    `"Eternal Ltd Stock Analysis: Zomato"` matches the shared name *and* the old
    ticker — and reporting that as an ambiguity would make every post about a
    renamed company permanently unresolvable. Two rows for one company are not
    two companies.

    Only a listing-status difference collapses. Two rows sharing a name that are
    both active, or both inactive, stay as they are: that is a data problem an
    operator should see reported rather than have one row silently preferred.
    """
    if len(candidates) < 2:
        return candidates

    dropped: set[str] = set()
    items = list(candidates.items())
    for index, (left_id, left) in enumerate(items):
        for right_id, right in items[index + 1 :]:
            if left_id == right_id or left_id in dropped or right_id in dropped:
                continue
            if not (name_keys(left.company.name) & name_keys(right.company.name)):
                continue
            left_active = (left.company.listing_status or "active") == "active"
            right_active = (right.company.listing_status or "active") == "active"
            if left_active == right_active:
                continue
            loser_id = right_id if left_active else left_id
            dropped.add(loser_id)
            log.info(
                "blogger mapping collapsed a renamed company",
                kept=left.company.ticker if left_active else right.company.ticker,
                dropped=candidates[loser_id].company.ticker,
                name=left.company.name,
            )
    return {key: value for key, value in items if key not in dropped}



@dataclass(frozen=True, slots=True)
class CompanyMatch:
    """The outcome of resolving one post.

    Carries the reason it failed as well as the company it found, because the
    sync's job on failure is to log something an operator can act on — "add a
    label" or "this post compares two companies" — and not merely a count.
    """

    company: Company | None = None
    #: Which tier decided it: `ticker_label`, `name_label`, `alias_label`,
    #: `title_name`, `title_alias`, `title_ticker`, `default_ticker`.
    method: str = ""
    #: The label or phrase that decided it, as the feed wrote it.
    matched_on: str = ""
    #: Populated when there is no company: why not.
    reason: str = ""
    #: Tickers that were in play, for an ambiguity an operator has to resolve.
    considered: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.company is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "ticker": self.company.ticker if self.company else None,
            "company_id": self.company.id if self.company else None,
            "company_name": self.company.name if self.company else None,
            "method": self.method or None,
            "matched_on": self.matched_on or None,
            "reason": self.reason or None,
            "considered": list(self.considered),
        }


@dataclass(slots=True)
class _Candidate:
    """One signal that a post is about one company."""

    company: Company
    method: str
    #: Normalised phrase that matched — the unit specificity is measured in.
    phrase: str
    #: The label or title text as the feed wrote it, for the log and metadata.
    raw: str
    #: Lower is stronger. Tier order, so that a ticker label outranks a title
    #: mention even when the title mention is a longer phrase.
    tier: int


class CompanyMapper:
    """Maps posts to companies already in the database.

    Loads the company list once and indexes it, rather than querying per post:
    a hundred posts against a thousand companies is a hundred thousand
    comparisons either way, and doing them in memory over two dictionaries
    costs one query instead of a hundred.
    """

    def __init__(self, db: Session, *, default_ticker: str = "") -> None:
        self.db = db
        self.default_ticker = ticker_key(default_ticker)
        self._by_ticker: dict[str, list[Company]] = {}
        self._by_name: dict[str, list[Company]] = {}
        self._companies: list[Company] = []
        self._loaded = False

    # ------------------------------------------------------------------
    @property
    def company_count(self) -> int:
        self._ensure_loaded()
        return len(self._companies)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        rows = list(
            self.db.execute(
                # Soft-deleted companies are excluded: a document attached to
                # one is invisible to every retrieval path that filters on the
                # company, so mapping to it would be a silent drop.
                select(Company).where(Company.deleted_at.is_(None))
            ).scalars()
        )
        self._companies = rows
        for company in rows:
            ticker = ticker_key(company.ticker)
            if ticker:
                self._by_ticker.setdefault(ticker, []).append(company)
            for key in name_keys(company.name):
                if key:
                    self._by_name.setdefault(key, []).append(company)
        self._loaded = True
        log.info(
            "blogger company index built",
            companies=len(rows), name_keys=len(self._by_name),
            tickers=len(self._by_ticker),
        )

    # ------------------------------------------------------------------
    def resolve(self, *, title: str, labels: Sequence[str] = ()) -> CompanyMatch:
        """The company a post is about, or an honest failure."""
        self._ensure_loaded()
        for decision in (
            self._from_ticker_labels(labels),
            self._from_name_labels(labels),
            self._from_title(title),
            self._from_default(),
        ):
            if decision is not None:
                return decision
        return CompanyMatch(
            reason=(
                "no ticker label, company-name label or title mention matched "
                "a company in the database"
            ),
        )

    # --- tier 1: tickers in labels -------------------------------------
    def _from_ticker_labels(self, labels: Sequence[str]) -> CompanyMatch | None:
        candidates: list[_Candidate] = []
        for label in labels:
            words = normalise_name(label).split()
            if not words:
                continue
            # The label *is* a ticker: "SHRIRAMFIN", "ITC", "BEL".
            key = ticker_key(label)
            for company in self._by_ticker.get(key, ()):
                candidates.append(
                    _Candidate(company, "ticker_label", key.lower(), label, tier=1)
                )
            # The label *starts with* the words of a ticker: "JSW Steel Stock
            # Analysis" → JSWSTEEL. Concatenated whole words compared for
            # equality, so a ticker can only be spelled out, never found
            # inside something else.
            for span in range(2, min(MAX_TICKER_WORDS, len(words)) + 1):
                joined = "".join(words[:span]).upper()
                if len(joined) < MIN_CONCATENATED_TICKER_CHARS:
                    continue
                for company in self._by_ticker.get(joined, ()):
                    candidates.append(
                        _Candidate(company, "ticker_label", joined.lower(), label, tier=1)
                    )
        return self._decide(candidates, tier="ticker label")

    # --- tier 2: company names and short forms in labels ----------------
    def _from_name_labels(self, labels: Sequence[str]) -> CompanyMatch | None:
        candidates: list[_Candidate] = []
        for label in labels:
            candidates.extend(self._phrase_candidates(label, tier=2))
        return self._decide(candidates, tier="label name")

    # --- tier 3: the title ---------------------------------------------
    def _from_title(self, title: str) -> CompanyMatch | None:
        candidates = self._phrase_candidates(title, tier=3)
        return self._decide(candidates, tier="title")

    # --- tier 4: the configured default --------------------------------
    def _from_default(self) -> CompanyMatch | None:
        if not self.default_ticker:
            return None
        companies = self._preferred(self._by_ticker.get(self.default_ticker, []))
        if not companies:
            # Named in the log rather than silently ignored: an operator who
            # set a default ticker and typo'd it would otherwise see every
            # post skipped with no clue why.
            log.warning(
                "configured default ticker is not in the database",
                ticker=self.default_ticker,
            )
            return CompanyMatch(
                reason=(
                    f"the configured default ticker '{self.default_ticker}' is "
                    "not a company in the database"
                ),
            )
        return CompanyMatch(
            company=companies[0], method="default_ticker",
            matched_on=self.default_ticker,
        )

    # ------------------------------------------------------------------
    def _phrase_candidates(self, text: str, *, tier: int) -> list[_Candidate]:
        """Every whole-phrase match of one label or title.

        Phrases are generated from the normalised words, longest first, and
        looked up in dictionaries — so a match is an exact equality against a
        known company name, alias or ticker. There is no substring search
        anywhere in this path, which is the property the brief asks for and the
        one that makes `"ITC"` inside `"BITCOIN"` a non-event.
        """
        words = normalise_name(text).split()
        if not words:
            return []

        found: list[_Candidate] = []
        limit = min(MAX_PHRASE_WORDS, len(words))
        for span in range(limit, 0, -1):
            for start in range(0, len(words) - span + 1):
                phrase = " ".join(words[start : start + span])
                if span == 1 and phrase in _AMBIGUOUS_WORDS:
                    # See the constant: an ordinary English word is not evidence
                    # of a company unless it is also an identifier, which the
                    # ticker and alias lookups below decide on their own.
                    continue

                for company in self._preferred(self._by_name.get(phrase, [])):
                    method = "title_name" if tier == 3 else "name_label"
                    found.append(_Candidate(company, method, phrase, phrase, tier))

                ticker = self._by_ticker.get(phrase.upper())
                if ticker is None and span > 1:
                    ticker = self._by_ticker.get("".join(words[start : start + span]).upper())
                for company in self._preferred(ticker or []):
                    method = "title_ticker" if tier == 3 else "ticker_label"
                    found.append(_Candidate(company, method, phrase, phrase, tier))

                alias = ALIASES.get(phrase)
                if alias:
                    for company in self._preferred(self._by_ticker.get(ticker_key(alias), [])):
                        method = "title_alias" if tier == 3 else "alias_label"
                        found.append(_Candidate(company, method, phrase, phrase, tier))

        return found

    # ------------------------------------------------------------------
    @staticmethod
    def _preferred(companies: Sequence[Company]) -> list[Company]:
        """Prefer an active listing when one key reaches several companies.

        A rename leaves two rows: the old symbol marked delisted and the new
        one active, both carrying the same company name. Treating that as an
        ambiguity would make every post about the renamed company unresolvable,
        which is not what the two rows mean — one of them is the company now.
        """
        if len(companies) <= 1:
            return list(companies)
        active = [c for c in companies if (c.listing_status or "active") == "active"]
        return active or list(companies)

    def _decide(
        self, candidates: Iterable[_Candidate], *, tier: str,
    ) -> CompanyMatch | None:
        """Turn one tier's signals into a decision, or hand over to the next.

        Returns ``None`` when the tier found nothing at all, which is the only
        case in which a weaker tier is allowed to speak.
        """
        pool = list(candidates)
        if not pool:
            return None

        # One company per signal, keeping its most specific phrase: a title can
        # match "shriram finance ltd" and "shriram finance" and they are the
        # same claim, not two.
        best: dict[str, _Candidate] = {}
        for candidate in pool:
            current = best.get(candidate.company.id)
            if current is None or len(candidate.phrase.split()) > len(current.phrase.split()):
                best[candidate.company.id] = candidate

        best = _collapse_rename_pairs(best)

        # Drop a company whose evidence is contained in another's. "ITC" in a
        # title that says "ITC Infotech" is the same three letters doing less
        # work, and keeping both would report an ambiguity where the feed was
        # perfectly clear.
        winners = [
            candidate for candidate in best.values()
            if not any(
                other.company.id != candidate.company.id
                and _words_contained(candidate.phrase, other.phrase)
                for other in best.values()
            )
        ]
        if not winners:
            # Containment cannot remove every candidate — the longest phrase is
            # never contained in a shorter one — so this is a guard against a
            # future edit rather than a reachable state. Falling back to every
            # signal keeps the tier's decision honest: if the specificity rule
            # cannot separate them, the ambiguity check below reports it.
            winners = list(best.values())

        if len(winners) > 1:
            tickers = sorted({winner.company.ticker for winner in winners})
            log.info(
                "blogger post is ambiguous", tier=tier, tickers=tickers,
                phrases=[w.phrase for w in winners],
            )
            return CompanyMatch(
                reason=(
                    f"ambiguous: the {tier} names more than one company "
                    f"({', '.join(tickers)})"
                ),
                considered=tuple(tickers),
            )

        winner = winners[0]
        if (winner.company.listing_status or "active") != "active":
            # Filing against a delisted row is not an error, but it is a hole:
            # retrieval filters on the company, so if the platform has stopped
            # surfacing that listing the post is indexed and never found. Said
            # out loud rather than left to be discovered as missing coverage.
            log.warning(
                "blogger post mapped to a non-active listing",
                ticker=winner.company.ticker,
                listing_status=winner.company.listing_status,
                matched_on=winner.raw,
            )
        return CompanyMatch(
            company=winner.company, method=winner.method, matched_on=winner.raw,
        )


@dataclass(slots=True)
class MappingSummary:
    """What a batch of posts resolved to. Returned by the sync, not used by it."""

    resolved: int = 0
    unresolved: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    by_ticker: dict[str, int] = field(default_factory=dict)

    def add(self, match: CompanyMatch) -> None:
        if match.resolved and match.company is not None:
            self.resolved += 1
            self.by_method[match.method] = self.by_method.get(match.method, 0) + 1
            ticker = match.company.ticker
            self.by_ticker[ticker] = self.by_ticker.get(ticker, 0) + 1
        else:
            self.unresolved += 1

    def as_dict(self) -> dict[str, object]:
        return {
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "by_method": dict(self.by_method),
            "by_ticker": dict(self.by_ticker),
        }
