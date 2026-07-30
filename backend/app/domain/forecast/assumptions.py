"""Forecast assumption model.

Every number the forecast engine consumes lives here. Nothing is hard-coded in
the projection services: if a value influences the model, it is a field on
:class:`ForecastAssumptions` and can be overridden per company, per scenario or
per period.

Design notes
------------
**Per-period overrides.** Each driver is a scalar *default* plus an optional
``by_year`` map. A flat CAGR is the degenerate case, not the design centre —
institutional forecasts fade growth, expand margins and normalise capex over
time, so every driver is addressable year by year.

**Provenance.** Each assumption carries a :class:`Provenance` tag recording
where the value came from — analyst input, historical calibration, or a future
AI extraction from filings. This is what makes the engine AI-ready without any
backend change: an agent writes assumptions with ``source=AI_EXTRACTED`` and a
citation, and the engine consumes them exactly like manual input.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Iterable, Mapping


class Scenario(StrEnum):
    BEAR = "bear"
    BASE = "base"
    BULL = "bull"


class Provenance(StrEnum):
    """Where an assumption value came from."""

    DEFAULT = "default"                # engine fallback
    HISTORICAL = "historical"          # calibrated from reported financials
    ANALYST = "analyst"                # entered by a user
    AI_EXTRACTED = "ai_extracted"      # parsed from a document by the AI layer
    MANAGEMENT_GUIDANCE = "management_guidance"


class RevenueMethod(StrEnum):
    """How top-line growth is built."""

    CAGR = "cagr"                      # single compound rate, optionally fading
    VOLUME_PRICE = "volume_price"      # volume growth x realisation growth
    SEGMENT = "segment"                # bottom-up per business segment
    ORGANIC_ACQUISITION = "organic_acquisition"  # organic + inorganic split


@dataclass(frozen=True, slots=True)
class Driver:
    """A single assumption: a default value plus optional per-year overrides.

    ``by_year`` is keyed by forecast period index (1 = first forecast year).
    """

    value: float
    by_year: Mapping[int, float] = field(default_factory=dict)
    source: Provenance = Provenance.DEFAULT
    citation: str | None = None
    note: str | None = None

    def at(self, period: int) -> float:
        """Value for forecast period ``period`` (1-based)."""
        return self.by_year.get(period, self.value)

    def with_value(self, value: float, source: Provenance = Provenance.ANALYST) -> "Driver":
        return replace(self, value=value, source=source)

    def scaled(self, factor: float) -> "Driver":
        """Proportional shift, used to derive scenarios from a base case."""
        return replace(
            self,
            value=self.value * factor,
            by_year={k: v * factor for k, v in self.by_year.items()},
        )

    def shifted(self, delta: float) -> "Driver":
        """Additive shift, for rates where a proportional move is wrong.

        A 200 bps margin change is meaningful; a 10% *relative* change to a
        margin is not what an analyst means.
        """
        return replace(
            self,
            value=self.value + delta,
            by_year={k: v + delta for k, v in self.by_year.items()},
        )


def driver(value: float, **kw: Any) -> Driver:
    """Terse constructor for literal defaults."""
    return Driver(value=value, **kw)


@dataclass(frozen=True, slots=True)
class SegmentAssumption:
    """One revenue segment in a bottom-up build."""

    name: str
    base_revenue: float
    growth: Driver
    ebitda_margin: Driver | None = None


@dataclass(frozen=True, slots=True)
class ForecastAssumptions:
    """The complete assumption set for one scenario.

    Grouped by the service that consumes it, so ownership is unambiguous.
    """

    # ---------------------------------------------------------- horizon
    years: int = 5
    scenario: Scenario = Scenario.BASE

    # ---------------------------------------------------------- revenue
    revenue_method: RevenueMethod = RevenueMethod.CAGR
    revenue_growth: Driver = field(default_factory=lambda: driver(0.10))
    #: Long-run growth the near-term rate decays toward across the horizon.
    terminal_revenue_growth: Driver = field(default_factory=lambda: driver(0.05))
    #: 0 = no fade (flat CAGR), 1 = full linear fade to terminal growth.
    growth_fade: Driver = field(default_factory=lambda: driver(0.0))

    volume_growth: Driver = field(default_factory=lambda: driver(0.06))
    price_growth: Driver = field(default_factory=lambda: driver(0.04))

    organic_growth: Driver = field(default_factory=lambda: driver(0.09))
    acquisition_growth: Driver = field(default_factory=lambda: driver(0.0))

    segments: tuple[SegmentAssumption, ...] = ()

    # ---------------------------------------------------------- margins
    gross_margin: Driver | None = None
    ebitda_margin: Driver = field(default_factory=lambda: driver(0.18))
    #: Annual margin expansion/(compression) applied cumulatively, in points.
    margin_expansion: Driver = field(default_factory=lambda: driver(0.0))
    other_income_pct_revenue: Driver = field(default_factory=lambda: driver(0.01))

    # ----------------------------------------------------------- capex
    capex_pct_revenue: Driver = field(default_factory=lambda: driver(0.05))
    #: Share of capex that is maintenance rather than growth.
    maintenance_capex_pct: Driver = field(default_factory=lambda: driver(0.55))

    # ---------------------------------------------------- depreciation
    #: D&A as a share of opening net block — an asset-based rate, not a
    #: revenue ratio, so the schedule stays internally consistent.
    depreciation_rate: Driver = field(default_factory=lambda: driver(0.11))

    # -------------------------------------------------- working capital
    inventory_days: Driver = field(default_factory=lambda: driver(60.0))
    receivable_days: Driver = field(default_factory=lambda: driver(45.0))
    payable_days: Driver = field(default_factory=lambda: driver(40.0))
    other_ca_pct_revenue: Driver = field(default_factory=lambda: driver(0.04))
    other_cl_pct_revenue: Driver = field(default_factory=lambda: driver(0.05))

    # ------------------------------------------------------------ debt
    interest_rate: Driver = field(default_factory=lambda: driver(0.085))
    #: Fraction of opening long-term debt repaid each year.
    debt_repayment_pct: Driver = field(default_factory=lambda: driver(0.10))
    new_debt: Driver = field(default_factory=lambda: driver(0.0))
    #: Yield earned on surplus cash.
    cash_yield: Driver = field(default_factory=lambda: driver(0.04))
    #: Minimum operating cash to retain before sweeping to debt repayment.
    min_cash_pct_revenue: Driver = field(default_factory=lambda: driver(0.03))

    # ------------------------------------------------------------ taxes
    effective_tax_rate: Driver = field(default_factory=lambda: driver(0.25))

    # -------------------------------------------------------- dividends
    dividend_payout: Driver = field(default_factory=lambda: driver(0.20))

    # ------------------------------------------------- valuation inputs
    wacc: Driver = field(default_factory=lambda: driver(0.115))
    terminal_growth: Driver = field(default_factory=lambda: driver(0.05))
    exit_ev_ebitda: Driver = field(default_factory=lambda: driver(12.0))
    target_pe: Driver = field(default_factory=lambda: driver(20.0))

    # ------------------------------------------------------ probability
    #: Used to build the probability-weighted expected value across scenarios.
    probability: Driver = field(default_factory=lambda: driver(1.0))

    # ------------------------------------------------------------- meta
    notes: str | None = None

    # ------------------------------------------------------------ utils
    DRIVER_FIELDS: tuple[str, ...] = ()

    def driver_names(self) -> tuple[str, ...]:
        """Names of every Driver-typed field, for serialisation and UI."""
        return tuple(
            name
            for name in self.__slots__  # type: ignore[attr-defined]
            if isinstance(getattr(self, name, None), Driver)
        )

    def get(self, name: str) -> Driver:
        value = getattr(self, name, None)
        if not isinstance(value, Driver):
            raise KeyError(f"{name} is not an assumption driver")
        return value

    def periods(self) -> range:
        """1-based forecast period indices."""
        return range(1, self.years + 1)

    def override(self, **kw: Any) -> "ForecastAssumptions":
        """Return a copy with fields replaced. Assumptions are immutable."""
        return replace(self, **kw)

    def with_drivers(
        self, updates: Mapping[str, float], source: Provenance = Provenance.ANALYST
    ) -> "ForecastAssumptions":
        """Bulk-update driver scalars by name.

        This is the single entry point used by the API for analyst edits and,
        later, by the AI layer for document-derived assumptions — which is why
        no code change is needed to make the engine AI-driven.
        """
        patch: dict[str, Any] = {}
        for name, value in updates.items():
            current = getattr(self, name, None)
            if isinstance(current, Driver):
                patch[name] = current.with_value(float(value), source)
            elif name == "years":
                patch[name] = int(value)
        return replace(self, **patch)

    def provenance_summary(self) -> dict[str, int]:
        """Counts by source, so the UI can show how grounded a forecast is."""
        out: dict[str, int] = {}
        for name in self.driver_names():
            src = self.get(name).source.value
            out[src] = out.get(src, 0) + 1
        return out
