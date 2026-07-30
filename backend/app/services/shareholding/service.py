"""Shareholding pattern analysis service.

Reads quarterly snapshots and derives promoter/institutional aggregates,
pledge exposure, quarter-on-quarter trends and an ownership signal.

Specification conventions:
  * All percentages are fractions (0.521 == 52.1%).
  * ``promoter_pledged`` is a share of the PROMOTER holding, not of total
    equity — pledge as a share of equity is derived from it.
  * Retail/public is the residual, so the pattern always totals 100%.
"""
from __future__ import annotations

from app.domain.calc import basis_points, safe_div
from app.models.analysis import ShareholdingSnapshot
from app.schemas.common import Flag, MetricRow, MetricSection, Unit


class ShareholdingService:
    def __init__(
        self,
        snapshots: list[ShareholdingSnapshot],
        market_cap: float | None = None,
    ) -> None:
        # Oldest first, so index -1 is always the latest disclosure.
        self.snaps = sorted(snapshots, key=lambda s: (s.fiscal_year, s.quarter))
        self.market_cap = market_cap
        self.n = len(self.snaps)

    # ---------------------------------------------------------- aggregates
    @staticmethod
    def promoter_total(s: ShareholdingSnapshot) -> float:
        return s.promoter_indian + s.promoter_foreign

    @staticmethod
    def dii_total(s: ShareholdingSnapshot) -> float:
        return s.mutual_funds + s.insurance + s.banks_fis_aif

    def institutional_total(self, s: ShareholdingSnapshot) -> float:
        return s.fii_fpi + self.dii_total(s)

    def public_retail(self, s: ShareholdingSnapshot) -> float:
        """Residual, so the pattern always sums to 1.0."""
        return max(
            0.0,
            1.0
            - self.promoter_total(s)
            - s.fii_fpi
            - self.dii_total(s)
            - s.government
            - s.others_custodians,
        )

    def free_float(self, s: ShareholdingSnapshot) -> float:
        return 1.0 - self.promoter_total(s)

    def pledge_of_equity(self, s: ShareholdingSnapshot) -> float:
        """Pledged shares as a share of TOTAL equity."""
        return s.promoter_pledged * self.promoter_total(s)

    def labels(self) -> list[str]:
        return [s.period_label for s in self.snaps]

    # ------------------------------------------------------------- sections
    def pattern_section(self) -> MetricSection:
        s = self.snaps
        return MetricSection(key="pattern", title="A. Shareholding pattern", rows=[
            MetricRow(key="promoter_indian", label="Promoters — Indian", unit=Unit.PERCENT,
                      values=[x.promoter_indian for x in s], indent=1),
            MetricRow(key="promoter_foreign", label="Promoters — foreign", unit=Unit.PERCENT,
                      values=[x.promoter_foreign for x in s], indent=1),
            MetricRow(key="promoter_total", label="Promoter holding — total", unit=Unit.PERCENT,
                      values=[self.promoter_total(x) for x in s], is_subtotal=True),
            MetricRow(key="fii_fpi", label="FII / FPI", unit=Unit.PERCENT,
                      values=[x.fii_fpi for x in s], indent=1),
            MetricRow(key="mutual_funds", label="Mutual funds", unit=Unit.PERCENT,
                      values=[x.mutual_funds for x in s], indent=1),
            MetricRow(key="insurance", label="Insurance companies", unit=Unit.PERCENT,
                      values=[x.insurance for x in s], indent=1),
            MetricRow(key="banks_fis_aif", label="Banks / FIs / AIF", unit=Unit.PERCENT,
                      values=[x.banks_fis_aif for x in s], indent=1),
            MetricRow(key="dii_total", label="DII — total", unit=Unit.PERCENT,
                      values=[self.dii_total(x) for x in s], is_subtotal=True),
            MetricRow(key="institutional_total", label="Institutional (FII + DII)", unit=Unit.PERCENT,
                      values=[self.institutional_total(x) for x in s], is_subtotal=True),
            MetricRow(key="government", label="Government", unit=Unit.PERCENT,
                      values=[x.government for x in s], indent=1),
            MetricRow(key="others", label="Others / custodians / trusts", unit=Unit.PERCENT,
                      values=[x.others_custodians for x in s], indent=1),
            MetricRow(key="public_retail", label="Retail & HNI (public)", unit=Unit.PERCENT,
                      values=[self.public_retail(x) for x in s], indent=1),
            MetricRow(key="total", label="Total", unit=Unit.PERCENT,
                      values=[1.0 for _ in s], is_subtotal=True),
        ])

    def pledge_section(self) -> MetricSection:
        s = self.snaps
        pledged = [x.promoter_pledged for x in s]
        return MetricSection(key="pledge", title="B. Pledge & encumbrance", rows=[
            MetricRow(key="promoter_pledged", label="Promoter shares pledged (% of promoter holding)",
                      unit=Unit.PERCENT, values=pledged),
            MetricRow(key="pledge_of_equity", label="Promoter shares pledged (% of total equity)",
                      unit=Unit.PERCENT, values=[self.pledge_of_equity(x) for x in s]),
            MetricRow(key="pledge_change", label="Encumbrance change QoQ", unit=Unit.BPS,
                      values=[None] + [basis_points(pledged[i], pledged[i - 1]) for i in range(1, self.n)]),
        ])

    def trend_section(self) -> MetricSection:
        """Trailing 4- and 12-quarter movements, in basis points."""
        s = self.snaps

        def move(fn, lag: int) -> float | None:
            if self.n <= lag:
                return None
            return basis_points(fn(s[-1]), fn(s[-1 - lag]))

        latest = s[-1] if s else None
        free_float_mcap = (
            self.free_float(latest) * self.market_cap
            if latest and self.market_cap is not None else None
        )

        def single(v: float | None) -> list[float | None]:
            """Trend metrics are point-in-time; pad so column counts align."""
            return [None] * (self.n - 1) + [v] if self.n else []

        return MetricSection(key="trends", title="C. Ownership trend diagnostics", rows=[
            MetricRow(key="promoter_change_4q", label="Promoter holding change — 4 quarters",
                      unit=Unit.BPS, values=single(move(self.promoter_total, 4))),
            MetricRow(key="promoter_change_12q", label="Promoter holding change — 12 quarters",
                      unit=Unit.BPS, values=single(move(self.promoter_total, 12))),
            MetricRow(key="fii_change_4q", label="FII holding change — 4 quarters",
                      unit=Unit.BPS, values=single(move(lambda x: x.fii_fpi, 4))),
            MetricRow(key="fii_change_12q", label="FII holding change — 12 quarters",
                      unit=Unit.BPS, values=single(move(lambda x: x.fii_fpi, 12))),
            MetricRow(key="dii_change_4q", label="DII holding change — 4 quarters",
                      unit=Unit.BPS, values=single(move(self.dii_total, 4))),
            MetricRow(key="institutional_change_12q", label="Institutional change — 12 quarters",
                      unit=Unit.BPS, values=single(move(self.institutional_total, 12))),
            MetricRow(key="free_float", label="Free float (100% − promoter)", unit=Unit.PERCENT,
                      values=[self.free_float(x) for x in s]),
            MetricRow(key="free_float_mcap", label="Free-float market cap", unit=Unit.CRORE,
                      values=single(free_float_mcap)),
        ])

    # ------------------------------------------------------------- signal
    def ownership_signal(self) -> dict:
        """Directional read on smart-money behaviour over the last four quarters."""
        if self.n < 2:
            return {"signal": "Insufficient history", "score": None, "detail": None}

        lag = min(4, self.n - 1)
        prior, latest = self.snaps[-1 - lag], self.snaps[-1]
        promoter_bps = basis_points(self.promoter_total(latest), self.promoter_total(prior)) or 0
        inst_bps = basis_points(self.institutional_total(latest), self.institutional_total(prior)) or 0
        pledge_bps = basis_points(latest.promoter_pledged, prior.promoter_pledged) or 0

        score = 0
        score += 1 if promoter_bps > 25 else -1 if promoter_bps < -25 else 0
        score += 1 if inst_bps > 50 else -1 if inst_bps < -50 else 0
        score += -1 if pledge_bps > 25 else 1 if pledge_bps < -25 else 0

        signal = (
            "Accumulation" if score >= 2
            else "Mild accumulation" if score == 1
            else "Distribution" if score <= -2
            else "Mild distribution" if score == -1
            else "Stable"
        )
        return {
            "signal": signal,
            "score": score,
            "detail": (
                f"Promoter {promoter_bps:+.0f} bps · institutional {inst_bps:+.0f} bps · "
                f"pledge {pledge_bps:+.0f} bps over {lag} quarters"
            ),
        }

    def flags(self) -> list[Flag]:
        if not self.snaps:
            return []
        latest = self.snaps[-1]
        out: list[Flag] = []

        high_pledge = latest.promoter_pledged > 0.25
        out.append(Flag(
            key="high_pledge", label="Promoter pledge above 25% of holding",
            triggered=high_pledge, severity="alert" if high_pledge else "info",
            detail=f"{latest.promoter_pledged:.1%} pledged" if high_pledge else None,
        ))

        low_promoter = self.promoter_total(latest) < 0.26
        out.append(Flag(
            key="low_promoter_stake", label="Promoter holding below 26%",
            triggered=low_promoter, severity="warn" if low_promoter else "info",
            detail="Below the special-resolution blocking threshold" if low_promoter else None,
        ))

        if self.n > 4:
            drop = basis_points(self.promoter_total(latest), self.promoter_total(self.snaps[-5]))
            falling = bool(drop is not None and drop < -100)
            out.append(Flag(
                key="promoter_selling", label="Promoter stake down over 100 bps in 4 quarters",
                triggered=falling, severity="warn" if falling else "info",
                detail=f"{drop:+.0f} bps" if drop is not None else None,
            ))
        return out

    def all_sections(self) -> list[MetricSection]:
        if not self.snaps:
            return []
        return [self.pattern_section(), self.pledge_section(), self.trend_section()]
