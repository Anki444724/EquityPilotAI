"""Canonical financial data resolution.

This is the software translation of `0C Data Map` — the workbook sheet that
normalises raw imported data into 54 canonical line items across N years.

The workbook resolves each of its 540 value cells through a strict 4-tier
precedence chain. We reproduce that chain exactly, with one deliberate
divergence documented in `Precedence.SAMPLE_DEFAULT`.

Nothing here performs I/O. `CanonicalFinancials` is a plain value object built
once per request (the software equivalent of the workbook's single
`ActiveOffset` resolution) and shared by every downstream engine, so no engine
repeats a lookup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable, Mapping

from .line_items import CANONICAL_ORDER, LineItem, Statement, ITEMS_BY_STATEMENT


class Precedence(IntEnum):
    """Resolution tiers, mirroring `0C Data Map`.

    Lower value wins. The workbook's chain is:

        =IF(<override U..AD><>"", <override>,
            IF(ActiveOffset>0, INDEX(StoreVals, item, ActiveOffset+y),
               <legacy alias match>))
    """

    #: Analyst override — workbook columns U..AD. Always wins.
    OVERRIDE = 1
    #: Imported/stored company facts — workbook `StoreVals`.
    STORE = 2
    #: Derived via an alias mapping — the workbook's `$M` alias-match path.
    ALIAS = 3
    #: DELIBERATE DIVERGENCE. The workbook falls back to sample constants so a
    #: demo file never looks empty. A commercial product must never present a
    #: fabricated figure as real, so this tier resolves to ``None`` and the UI
    #: renders an explicit "no data" state.
    SAMPLE_DEFAULT = 4


@dataclass(frozen=True, slots=True)
class Fact:
    """A single resolved data point."""

    line_item: LineItem
    fiscal_year: int
    value: float | None
    precedence: Precedence
    source: str | None = None

    @property
    def is_present(self) -> bool:
        return self.value is not None


@dataclass(frozen=True, slots=True)
class CanonicalFinancials:
    """54 canonical line items across N fiscal years for one company.

    Built once per request and passed to every engine. This is the direct
    analogue of the workbook resolving `ActiveOffset` a single time and having
    540 cells consume that one scalar.
    """

    company_id: str
    fiscal_years: tuple[int, ...]
    _facts: Mapping[tuple[LineItem, int], Fact] = field(repr=False)

    # ---------------------------------------------------------------- access
    def get(self, item: LineItem, year: int) -> float | None:
        """Resolved value, or ``None`` when genuinely unavailable."""
        fact = self._facts.get((item, year))
        return fact.value if fact else None

    def at(self, item: LineItem, year: int) -> float:
        """Value coerced to 0.0 for arithmetic.

        Mirrors the workbook's `IFERROR(..., 0)` guards, which let a partially
        populated model still compute rather than cascading errors. Use
        :meth:`get` when the caller must distinguish zero from missing.
        """
        v = self.get(item, year)
        return 0.0 if v is None else v

    def fact(self, item: LineItem, year: int) -> Fact | None:
        return self._facts.get((item, year))

    def series(self, item: LineItem) -> tuple[float | None, ...]:
        """The item across all fiscal years, oldest first."""
        return tuple(self.get(item, y) for y in self.fiscal_years)

    def series_at(self, item: LineItem) -> tuple[float, ...]:
        return tuple(self.at(item, y) for y in self.fiscal_years)

    # ------------------------------------------------------------ properties
    @property
    def latest_year(self) -> int | None:
        return self.fiscal_years[-1] if self.fiscal_years else None

    @property
    def earliest_year(self) -> int | None:
        return self.fiscal_years[0] if self.fiscal_years else None

    def has_data(self) -> bool:
        """True when at least one canonical item resolved.

        The workbook equivalent is the statement gate
        `OR('0C Data Map'!$M$n>0, ActiveOffset>0)` — the test for whether real
        data reached the statements at all.
        """
        return any(f.is_present for f in self._facts.values())

    def coverage(self) -> float:
        """Share of the 54 x N grid that resolved to real data (0..1)."""
        total = len(CANONICAL_ORDER) * len(self.fiscal_years)
        if not total:
            return 0.0
        present = sum(1 for f in self._facts.values() if f.is_present)
        return present / total

    def statement_items(self, statement: Statement) -> tuple[LineItem, ...]:
        return ITEMS_BY_STATEMENT[statement]


class CanonicalFinancialsBuilder:
    """Accumulates facts and applies the precedence chain.

    Facts may be added in any order and from any tier; the builder keeps the
    strongest tier per (item, year) cell.
    """

    def __init__(self, company_id: str, fiscal_years: Iterable[int]) -> None:
        self._company_id = company_id
        self._years = tuple(sorted(set(fiscal_years)))
        self._facts: dict[tuple[LineItem, int], Fact] = {}

    def add(
        self,
        item: LineItem,
        year: int,
        value: float | None,
        precedence: Precedence,
        source: str | None = None,
    ) -> "CanonicalFinancialsBuilder":
        if year not in self._years or value is None:
            return self
        key = (item, year)
        existing = self._facts.get(key)
        # lower Precedence value wins; equal tiers keep first writer
        if existing is None or precedence < existing.precedence:
            self._facts[key] = Fact(item, year, float(value), precedence, source)
        return self

    def add_many(
        self,
        rows: Iterable[tuple[LineItem, int, float | None]],
        precedence: Precedence,
        source: str | None = None,
    ) -> "CanonicalFinancialsBuilder":
        for item, year, value in rows:
            self.add(item, year, value, precedence, source)
        return self

    def build(self) -> CanonicalFinancials:
        # Materialise every cell so callers can distinguish "absent" cleanly.
        complete: dict[tuple[LineItem, int], Fact] = {}
        for item in CANONICAL_ORDER:
            for year in self._years:
                key = (item, year)
                complete[key] = self._facts.get(
                    key, Fact(item, year, None, Precedence.SAMPLE_DEFAULT)
                )
        return CanonicalFinancials(
            company_id=self._company_id,
            fiscal_years=self._years,
            _facts=complete,
        )
