"""Protection of untranslatable tokens.

The brief lists what must never be translated: company names, tickers, ISINs,
financial numbers, ratios, years, document titles, citation and evidence IDs.
This module is the mechanism that guarantees it.

The approach is **mask, translate, restore**. Every protected span is replaced
with an opaque sentinel before the text reaches a translator, and put back
verbatim afterwards. The alternative — instructing the model not to touch
certain things — is not a control. Models transliterate "TCS" to "टीसीएस" and
helpfully convert "₹2,55,324 crore" to "255 billion rupees" with no ill intent
whatsoever, and both destroy the citation audit.

The sentinel design is deliberate and was arrived at by elimination:

* ``{0}`` style — models reformat braces and occasionally renumber them.
* ``[[0]]`` — collides with the citation markers this module exists to protect.
* ``<x0>`` — models close, nest and pretty-print anything resembling XML.
* ``\ue000``-range private-use characters — invisible, so a model that drops
  one leaves no trace and the failure is silent.

What survives is ``§0§``: a printable character that appears in no financial
prose, is not markup, and is visible in a diff when something goes wrong.
Restoration is verified rather than assumed — :func:`restore` reports any
sentinel the translator lost, and the adapter treats a loss as a failed
translation rather than shipping mangled output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Sentinel delimiter. See the module docstring for why this and not braces,
#: brackets, angle brackets or private-use characters.
_SENTINEL = "§"
_SENTINEL_RE = re.compile(r"§(\d+)§")


def sentinel(index: int) -> str:
    return f"{_SENTINEL}{index}{_SENTINEL}"


# ---------------------------------------------------------------------------
# What is protected
# ---------------------------------------------------------------------------
#
# Order matters: the list is applied in sequence and earlier patterns win, so
# the most specific must come first. `[revenue]` has to be captured as a
# citation marker before the bare-word rules could ever see the text inside it.

#: Citation and evidence markers, e.g. `[revenue]`, `[doc_412_p8]`, and the
#: human-readable form `annotate()` produces, e.g. `[FY2025 Annual Report]`.
_CITATION = re.compile(r"\[[^\]\n]{1,120}\]")

#: Markdown links — the URL must survive even when the label is translated.
#: Captured whole and restored whole; a translated label inside a broken link
#: is worse than an untranslated one.
_MARKDOWN_LINK = re.compile(r"\[[^\]\n]{0,120}\]\([^)\s]{1,300}\)")

#: ISIN: two letters, nine alphanumerics, one check digit.
_ISIN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")

#: Any number, with Indian or Western grouping, optional decimals, optional
#: leading currency symbol, optional trailing percent or multiplier.
#: Covers 2,55,324 · 1,234.56 · ₹3,500 · 24.5x · 12.3% · -0.45
_NUMBER = re.compile(
    r"[₹$€£]?\s?-?\d[\d,]*\.?\d*\s?(?:%|x|bps|bn|mn|cr)?",
    re.IGNORECASE,
)

#: Fiscal-year labels: FY2025, FY25, Q1FY26, H1FY25, 2024-25.
_FISCAL = re.compile(
    r"\b(?:[QH][1-4]\s?)?FY\s?\d{2,4}\b|\b\d{4}[-–]\d{2,4}\b",
    re.IGNORECASE,
)

#: Tickers and all-caps identifiers: TCS, BAJAJ-AUTO, M&M, GVT&D, ULTRACEMCO.
#: Two characters minimum so ordinary capitalised words are not caught, and
#: anchored on word boundaries so it does not fire inside a sentence-initial
#: capital.
_TICKER = re.compile(r"\b[A-Z][A-Z0-9]{1,14}(?:[-&][A-Z0-9]{1,14})*\b")

#: Well-known financial acronyms that must never be transliterated even
#: though `_TICKER` would also catch most of them. Listed explicitly so the
#: intent is documented rather than incidental.
_ACRONYMS = frozenset("""
ROE ROCE ROIC EPS PE PB PAT PBT EBIT EBITDA DCF EV FCF CFO CAGR NAV
GST SEBI RBI NSE BSE ISIN CAGR YOY QOQ TTM MOAT ESG BRSR LODR NCLT
IT FMCG NBFC IPO FPO QIP AGM EGM MD CEO CFO CIO COO KMP
USD INR EUR GBP
""".split())


@dataclass(frozen=True, slots=True)
class ProtectedSpan:
    """One masked span and the text it stands for."""

    index: int
    original: str
    kind: str

    @property
    def token(self) -> str:
        return sentinel(self.index)


@dataclass(slots=True)
class Protection:
    """A masked string plus everything needed to restore it."""

    masked: str
    spans: list[ProtectedSpan] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.spans)

    def kinds(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for span in self.spans:
            out[span.kind] = out.get(span.kind, 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"protected": self.count, "kinds": self.kinds()}


@dataclass(frozen=True, slots=True)
class Restoration:
    """The result of putting the protected spans back."""

    text: str
    #: Sentinels the translator failed to return. A non-empty list means the
    #: translation is unusable — a dropped citation marker is a broken
    #: evidence chain, not a cosmetic defect.
    lost: tuple[ProtectedSpan, ...] = ()
    #: Sentinels present in the output that were never issued. Indicates the
    #: model invented one, which is equally disqualifying.
    spurious: tuple[str, ...] = ()

    @property
    def is_intact(self) -> bool:
        return not self.lost and not self.spurious


def _rules() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Protection rules, most specific first."""
    return (
        ("markdown_link", _MARKDOWN_LINK),
        ("citation", _CITATION),
        ("isin", _ISIN),
        ("fiscal_year", _FISCAL),
        ("number", _NUMBER),
        ("ticker", _TICKER),
    )


def protect(text: str, *, extra_terms: Iterable[str] = ()) -> Protection:
    """Mask every untranslatable span.

    ``extra_terms`` carries the things only the caller knows — the company's
    name and ticker, and any document titles in scope. Company names are the
    one category that cannot be recognised by pattern: "Tata Consultancy
    Services" and "Asian Paints" look exactly like ordinary prose, and a
    translator will happily render the second as a sentence about paint.

    **Sentinels are never re-scanned.** PROTECT-001: the first implementation
    ran each rule over the whole working string, so the `number` rule matched
    the digits *inside* an already-issued sentinel — `§0§` became `§§3§§`,
    where span 3 was the digit "0". Restoration then produced garbage, and the
    round trip failed on any text containing both a ticker and a figure, which
    is every sentence this platform generates.

    The fix is to tokenise rather than substitute: the text is held as a list
    of alternating protected and unprotected fragments, and each successive
    rule is applied only to the fragments that are still unprotected. A
    sentinel, once issued, is inert.
    """
    spans: list[ProtectedSpan] = []
    # Fragments: (text, is_protected). Only unprotected fragments are scanned.
    fragments: list[tuple[str, bool]] = [(text or "", False)]

    def apply(pattern: re.Pattern[str], kind: str) -> None:
        rebuilt: list[tuple[str, bool]] = []
        for chunk, protected_already in fragments:
            if protected_already or not chunk:
                rebuilt.append((chunk, protected_already))
                continue

            cursor = 0
            for match in pattern.finditer(chunk):
                original = match.group(0)
                if not original.strip() or original.strip() in {"₹", "$", "€", "£"}:
                    continue
                if match.start() > cursor:
                    rebuilt.append((chunk[cursor:match.start()], False))
                span = ProtectedSpan(
                    index=len(spans), original=original, kind=kind,
                )
                spans.append(span)
                rebuilt.append((span.token, True))
                cursor = match.end()

            if cursor < len(chunk):
                rebuilt.append((chunk[cursor:], False))

        fragments[:] = rebuilt

    # Caller-supplied literals first: they are the most specific of all, and
    # longest-first so "Tata Consultancy Services" is taken before "Tata".
    for term in sorted({t for t in extra_terms if t and len(t.strip()) > 1},
                       key=len, reverse=True):
        apply(
            re.compile(rf"(?<!\w){re.escape(term.strip())}(?!\w)", re.IGNORECASE),
            "entity",
        )

    for kind, pattern in _rules():
        apply(pattern, kind)

    return Protection(masked="".join(chunk for chunk, _ in fragments),
                      spans=spans)


def restore(masked: str, protection: Protection) -> Restoration:
    """Put the protected spans back, and report anything that went missing.

    Verification is the point of this function. A translator that silently
    drops `§7§` has removed a citation, and returning the text without
    noticing would break the evidence chain that the whole platform rests on.
    """
    text = masked or ""
    lost: list[ProtectedSpan] = []

    for span in protection.spans:
        token = span.token
        if token in text:
            text = text.replace(token, span.original)
        else:
            lost.append(span)

    # Anything still matching the sentinel shape was invented by the model.
    spurious = tuple(sorted(set(_SENTINEL_RE.findall(text))))
    for index in spurious:
        text = text.replace(sentinel(int(index)), "")

    return Restoration(text=text, lost=tuple(lost), spurious=spurious)


def verify_preserved(source: str, translated: str) -> list[str]:
    """Independent check that protected content survived a round trip.

    Deliberately does not use the sentinel bookkeeping: it re-extracts the
    protected spans from both strings and compares them. A bug in `protect`
    or `restore` would be invisible to a check built on the same machinery,
    and this is the assertion the test suite and the API rely on.

    Returns a list of human-readable discrepancies; empty means intact.
    """
    problems: list[str] = []

    for kind, pattern in _rules():
        if kind == "ticker":
            # Tickers are checked against the acronym list only. The general
            # pattern matches any capitalised token, including ordinary words
            # that begin a Hindi sentence written in Latin script, so a strict
            # set comparison produces false alarms rather than findings.
            continue
        before = pattern.findall(source or "")
        after = pattern.findall(translated or "")
        missing = _multiset_difference(before, after)
        if missing:
            shown = ", ".join(repr(m) for m in missing[:4])
            problems.append(
                f"{len(missing)} {kind}(s) lost in translation: {shown}"
            )

    return problems


def _multiset_difference(before: list[str], after: list[str]) -> list[str]:
    """Items in `before` that are not matched one-for-one in `after`."""
    remaining = list(after)
    missing: list[str] = []
    for item in before:
        normalised = item.strip()
        match = next(
            (candidate for candidate in remaining
             if candidate.strip() == normalised), None,
        )
        if match is None:
            missing.append(item)
        else:
            remaining.remove(match)
    return missing


def is_acronym(token: str) -> bool:
    return token.upper() in _ACRONYMS
