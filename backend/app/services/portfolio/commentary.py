"""Portfolio AI commentary.

Same architecture as Module 6, for the same reason: **the platform generates
the numbers, the model explains them**. Every figure in every sentence here
comes from a `PortfolioView` that was computed before any prose was written, so
the commentary is structurally incapable of inventing a weight, a return or a
risk statistic.

Where a live LLM is configured, Module 6's router writes the prose from this
evidence. Where one is not — which is the case in this environment — the
deterministic composer below produces the same content from the same evidence.
That is not a degraded fallback: it is the ground truth the LLM is asked to
paraphrase, and it is what the tests assert against.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.domain.ai.types import Citation, EvidenceKind
from app.domain.portfolio.types import AlertEvaluation, AllocationDimension
from app.services.portfolio.engine import HoldingView, PortfolioView

#: Below this weight a holding is too small to move the portfolio, so it is
#: excluded from "top" lists however striking its own return.
MATERIAL_WEIGHT = 0.02


@dataclass(slots=True)
class CommentarySection:
    """One block of commentary, with the evidence behind it."""

    key: str
    title: str
    body: str
    citations: list[Citation] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.body.strip()


@dataclass(slots=True)
class PortfolioCommentary:
    """The full AI view of a portfolio."""

    portfolio_id: int
    sections: list[CommentarySection] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    provider: str = "offline"
    #: Stated plainly on every response, per the Module 6 guardrail.
    disclosure: str = (
        "This is analysis of the platform's own figures, not investment "
        "advice. Allocation and risk statistics are model outputs; any "
        "judgement about what to do with them is interpretation."
    )

    def section(self, key: str) -> CommentarySection | None:
        return next((s for s in self.sections if s.key == key), None)


def _pct(value: float | None, places: int = 1) -> str:
    return "unavailable" if value is None else f"{value * 100:.{places}f}%"


def _money(value: float | None) -> str:
    if value is None:
        return "unavailable"
    return f"₹{value:,.0f}"


class CommentaryEngine:
    """Composes grounded portfolio commentary from a resolved view."""

    def generate(
        self, view: PortfolioView, alerts: Sequence[AlertEvaluation] = ()
    ) -> PortfolioCommentary:
        citations = self._citations(view)
        commentary = PortfolioCommentary(
            portfolio_id=view.portfolio_id, citations=citations
        )
        commentary.sections = [
            self._health(view, alerts),
            self._risks(view, alerts),
            self._opportunities(view),
            self._rebalancing(view),
            self._positions(view),
        ]
        return commentary

    # ------------------------------------------------------------------
    def _citations(self, view: PortfolioView) -> list[Citation]:
        """Every figure the commentary is allowed to use."""
        out: list[Citation] = [
            Citation("pf_value", "Portfolio value", EvidenceKind.MARKET,
                     view.total_value, "₹", "Portfolio engine"),
            Citation("pf_cost", "Cost basis", EvidenceKind.MARKET,
                     view.cost_basis, "₹", "Portfolio engine"),
            Citation("pf_unrealised", "Unrealised P&L", EvidenceKind.MARKET,
                     view.unrealised_pnl, "₹", "Portfolio engine"),
            Citation("pf_realised", "Realised P&L", EvidenceKind.MARKET,
                     view.realised_pnl, "₹", "Portfolio engine"),
            Citation("pf_positions", "Open positions", EvidenceKind.MARKET,
                     view.position_count, "", "Portfolio engine"),
        ]
        if view.total_return is not None:
            out.append(Citation("pf_return", "Total return", EvidenceKind.MARKET,
                                view.total_return, "%", "Portfolio engine"))
        metrics = view.portfolio_metrics()
        labels = {
            "diversification_score": ("pf_diversification", "Diversification score", ""),
            "effective_positions": ("pf_effective", "Effective positions", ""),
            "top_5_concentration": ("pf_top5", "Top-5 concentration", "%"),
            "largest_sector_weight": ("pf_sector_max", "Largest sector weight", "%"),
            "cash_weight": ("pf_cash", "Cash weight", "%"),
        }
        for metric, (key, label, unit) in labels.items():
            value = metrics.get(metric)
            if value is not None:
                out.append(Citation(
                    key, label, EvidenceKind.SCORING, value, unit,
                    "Portfolio analytics",
                ))
        if view.risk:
            for attr, key, label, unit in (
                ("annualised_volatility", "pf_vol", "Annualised volatility", "%"),
                ("sharpe", "pf_sharpe", "Sharpe ratio", ""),
                ("sortino", "pf_sortino", "Sortino ratio", ""),
                ("max_drawdown", "pf_drawdown", "Maximum drawdown", "%"),
                ("var_95", "pf_var", "Value at risk (95%)", "%"),
                ("cvar_95", "pf_cvar", "Conditional VaR (95%)", "%"),
                ("beta", "pf_beta", "Beta", ""),
            ):
                value = getattr(view.risk, attr)
                if value is not None:
                    out.append(Citation(
                        key, label, EvidenceKind.SCORING, value, unit, "Risk engine"
                    ))
        for holding in view.holdings[:12]:
            out.append(Citation(
                f"pos_{holding.ticker.lower()}",
                f"{holding.position.name or holding.ticker} weight",
                EvidenceKind.MARKET, holding.weight, "%", "Portfolio engine",
            ))
        return out

    # ------------------------------------------------------------------
    def _health(
        self, view: PortfolioView, alerts: Sequence[AlertEvaluation]
    ) -> CommentarySection:
        metrics = view.portfolio_metrics()
        triggered = [a for a in alerts if a.is_triggered]
        critical = [a for a in triggered if a.severity.value in {"critical", "high"}]

        lines = [
            f"The book holds {view.position_count} positions worth "
            f"{_money(view.market_value)} [pf_value], against a cost basis of "
            f"{_money(view.cost_basis)} [pf_cost]. Unrealised profit and loss "
            f"stands at {_money(view.unrealised_pnl)} [pf_unrealised]"
            + (f", a return of {_pct(view.total_return)} [pf_return]."
               if view.total_return is not None else "."),
        ]
        if view.realised_pnl:
            lines.append(
                f"Realised gains of {_money(view.realised_pnl)} [pf_realised] "
                f"and dividends of {_money(view.dividends)} bring total profit "
                f"and loss to {_money(view.total_pnl)}."
            )

        diversification = metrics.get("diversification_score")
        effective = metrics.get("effective_positions")
        if diversification is not None and effective is not None:
            verdict = (
                "adequately diversified" if diversification >= 60
                else "concentrated" if diversification >= 45
                else "highly concentrated"
            )
            lines.append(
                f"On the platform's measure the portfolio is {verdict}: a "
                f"diversification score of {diversification:.0f} "
                f"[pf_diversification] and {effective:.1f} effective positions "
                f"[pf_effective] against {view.position_count} nominal holdings."
            )

        if view.risk and view.risk.sharpe is not None:
            lines.append(
                f"Risk-adjusted return is a Sharpe ratio of "
                f"{view.risk.sharpe:.2f} [pf_sharpe] on annualised volatility "
                f"of {_pct(view.risk.annualised_volatility)} [pf_vol]."
            )
        elif view.risk and view.risk.unavailable:
            lines.append(
                "Risk-adjusted statistics are unavailable: "
                f"{view.risk.unavailable[0].lower()}. The platform will not "
                "estimate a Sharpe ratio it cannot compute."
            )

        if critical:
            lines.append(
                f"**{len(critical)} high-severity alert"
                f"{'s' if len(critical) != 1 else ''}** "
                f"{'are' if len(critical) != 1 else 'is'} open: "
                + "; ".join(
                    f"{a.label}" + (f" ({a.ticker})" if a.ticker else "")
                    for a in critical[:4]
                ) + "."
            )
        elif triggered:
            lines.append(
                f"{len(triggered)} alerts are open, none of them high severity."
            )
        else:
            lines.append("No alert rules are currently triggered.")

        return CommentarySection(
            "health", "Portfolio health", "\n\n".join(lines)
        )

    def _risks(
        self, view: PortfolioView, alerts: Sequence[AlertEvaluation]
    ) -> CommentarySection:
        risks: list[str] = []
        metrics = view.portfolio_metrics()

        largest = view.largest_position
        if largest and largest.weight > largest.max_position_size:
            risks.append(
                f"**Position concentration.** {largest.position.name or largest.ticker} "
                f"is {_pct(largest.weight)} of the book [pos_{largest.ticker.lower()}], "
                f"above the {_pct(largest.max_position_size)} ceiling its "
                f"{largest.rating or 'unrated'} grade implies. A single holding "
                f"of that size makes portfolio outcome dependent on one thesis."
            )

        sector = view.allocations.get(AllocationDimension.SECTOR.value)
        if sector and sector.largest and sector.largest.weight > 0.35:
            risks.append(
                f"**Sector concentration.** {sector.largest.label} accounts for "
                f"{_pct(sector.largest.weight)} of market value "
                f"[pf_sector_max], beyond the 35% policy limit. Sector risk of "
                f"this size is not diversified away by holding several names "
                f"within it."
            )

        top5 = metrics.get("top_5_concentration")
        if top5 is not None and top5 > 0.50:
            risks.append(
                f"**Breadth.** The largest five positions are {_pct(top5)} of "
                f"the book [pf_top5]. The remaining holdings cannot materially "
                f"offset a loss in the top five."
            )

        if view.risk:
            if view.risk.max_drawdown is not None and view.risk.max_drawdown < -0.15:
                recovered = (
                    "and has since recovered" if view.risk.drawdown_recovered
                    else "and has not yet recovered"
                )
                risks.append(
                    f"**Drawdown.** The portfolio has fallen "
                    f"{_pct(abs(view.risk.max_drawdown))} peak to trough "
                    f"[pf_drawdown] {recovered}."
                )
            if view.risk.cvar_95 is not None:
                risks.append(
                    f"**Tail risk.** On the worst 5% of observed days the "
                    f"average loss was {_pct(abs(view.risk.cvar_95), 2)} "
                    f"[pf_cvar]. This is measured from history, not modelled, "
                    f"so it describes what has happened rather than a bound on "
                    f"what could."
                )
            if view.risk.illiquid_positions:
                risks.append(
                    f"**Liquidity.** {view.risk.illiquid_positions} position"
                    f"{'s' if view.risk.illiquid_positions != 1 else ''} would "
                    f"take more than thirty trading days to exit at a fifth of "
                    f"average traded value."
                )

        downgrades = [
            a for a in alerts
            if a.is_triggered and a.key in {"score_downgraded", "rating_below_a"}
        ]
        if downgrades:
            names = ", ".join(sorted({a.ticker for a in downgrades if a.ticker}))
            risks.append(
                f"**Quality.** The institutional score has fallen below the "
                f"threshold for {names}. These are the platform's own scores, "
                f"not a market view."
            )

        if view.unpriced:
            risks.append(
                f"**Unpriced holdings.** {', '.join(view.unpriced)} "
                f"{'have' if len(view.unpriced) != 1 else 'has'} no current "
                f"price, so {'their' if len(view.unpriced) != 1 else 'its'} "
                f"market value is excluded from every figure above. The "
                f"portfolio is larger than it appears."
            )

        body = "\n\n".join(risks) if risks else (
            "No concentration, drawdown or liquidity risk breaches the "
            "platform's thresholds on current data."
        )
        return CommentarySection("risks", "Top risks", body)

    def _opportunities(self, view: PortfolioView) -> CommentarySection:
        items: list[str] = []

        undervalued = sorted(
            (
                h for h in view.holdings
                if h.upside is not None and h.upside > 0.15
                and h.weight < h.max_position_size
            ),
            key=lambda h: -(h.upside or 0),
        )
        for holding in undervalued[:3]:
            items.append(
                f"**{holding.position.name or holding.ticker}** trades at "
                f"{_money(holding.position.current_price)} against a platform "
                f"fair value of {_money(holding.target_price)}, an upside of "
                f"{_pct(holding.upside)}. At {_pct(holding.weight)} it sits "
                f"below the {_pct(holding.max_position_size)} its "
                f"{holding.rating or 'unrated'} grade permits."
            )

        cash_weight = view.cash_weight
        if cash_weight is not None and cash_weight > 0.15:
            items.append(
                f"**Cash of {_pct(cash_weight)}** [pf_cash] "
                f"({_money(view.cash.balance)}) is uninvested. That is a "
                f"deliberate position if intended and a drag if not."
            )

        sector = view.allocations.get(AllocationDimension.SECTOR.value)
        if sector and len(sector.slices) < 6:
            represented = ", ".join(s.label for s in sector.slices)
            items.append(
                f"**Sector breadth.** The book spans {len(sector.slices)} "
                f"sectors ({represented}). Adding uncorrelated sectors would "
                f"raise the diversification score more cheaply than trimming "
                f"existing winners."
            )

        body = "\n\n".join(items) if items else (
            "No holding is both materially undervalued on platform figures and "
            "below its permitted weight."
        )
        return CommentarySection("opportunities", "Top opportunities", body)

    def _rebalancing(self, view: PortfolioView) -> CommentarySection:
        if not view.rebalance:
            return CommentarySection(
                "rebalancing", "Rebalancing",
                "No position breaches its weight ceiling and no target drift "
                "exceeds the two-percent band. No trades are suggested.",
            )
        lines = [
            f"{len(view.rebalance)} adjustment"
            f"{'s' if len(view.rebalance) != 1 else ''} would bring the book "
            f"back within policy:",
        ]
        for trade in view.rebalance[:8]:
            direction = "Trim" if trade.value_delta < 0 else "Add"
            shares = (
                f" (~{abs(trade.shares):,.0f} shares)"
                if trade.shares is not None else ""
            )
            lines.append(
                f"- **{direction} {trade.name}** by "
                f"{_money(abs(trade.value_delta))}{shares} — "
                f"{_pct(trade.current_weight)} against "
                f"{_pct(trade.target_weight)}. {trade.reason}."
            )
        lines.append(
            "These are mechanical consequences of the weight policy, not a "
            "view on the businesses. Trading costs and tax are not modelled."
        )
        return CommentarySection(
            "rebalancing", "Rebalancing suggestions", "\n".join(lines)
        )

    def _positions(self, view: PortfolioView) -> CommentarySection:
        material = [h for h in view.holdings if h.weight >= MATERIAL_WEIGHT]
        if not material:
            return CommentarySection(
                "positions", "Position commentary",
                "No position is large enough to comment on individually.",
            )

        ranked = sorted(
            material,
            key=lambda h: -(h.position.unrealised_pnl or 0),
        )
        lines: list[str] = []
        for holding in ranked[:3]:
            lines.append(self._position_line(holding, "contributor"))
        for holding in reversed(ranked[-2:]):
            if (holding.position.unrealised_pnl or 0) < 0:
                lines.append(self._position_line(holding, "detractor"))
        return CommentarySection(
            "positions", "Position commentary", "\n\n".join(lines)
        )

    @staticmethod
    def _position_line(holding: HoldingView, role: str) -> str:
        position = holding.position
        pieces = [
            f"**{position.name or holding.ticker}** ({_pct(holding.weight)}, "
            f"{role}) — {_money(position.market_value)} at "
            f"{_money(position.current_price)}, "
            f"{_pct(position.unrealised_return)} on cost "
            f"({_money(position.unrealised_pnl)})."
        ]
        if holding.score is not None:
            pieces.append(
                f"Institutional score {holding.score:.0f}"
                + (f" ({holding.rating})" if holding.rating else "") + "."
            )
        if holding.upside is not None:
            pieces.append(f"Platform upside {_pct(holding.upside)}.")
        elif holding.intrinsic_value is None:
            pieces.append(
                "No usable valuation: the data-quality gate declined to "
                "certify one for this holding."
            )
        if position.dividends:
            pieces.append(f"Dividends received {_money(position.dividends)}.")
        return " ".join(pieces)
