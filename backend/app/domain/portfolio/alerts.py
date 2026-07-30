"""Alert rule engine.

GENERATED-FROM-SPEC: the fourteen live rules and five AI rules below are the
workbook's `38 Alerts` sheet, rows 9–22 and 38–42, transcribed with their
thresholds, comparators, actions and priorities intact. `docs/module8_spec.json`
holds the extraction.

The design point is that a rule is **data, not code**. Each `AlertRule` names
the metric it reads, the comparator, the threshold and the severity; the engine
does nothing but resolve the metric and apply the comparator. Adding a rule is
a row, not a branch, which is what makes user-defined alerts possible without a
deployment.

The one substantive divergence from the workbook: a rule whose input is missing
evaluates to `UNAVAILABLE`, not `clear`. The workbook's `IF(F<E, ...)` treats a
blank cell as zero and therefore reports "✓ clear" for a company whose data was
never loaded. Silence about a risk is not evidence of its absence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from app.domain.portfolio.types import (
    AlertCategory, AlertEvaluation, AlertSeverity, AlertStatus, Comparator,
)

#: Institutional ratings that trip the "Rating below A" rule (workbook row 13).
SUB_INVESTMENT_RATINGS = frozenset({"BBB", "BB", "B", "C"})

#: Rating → maximum position size, from `30 Institutional Scorecard` H27:H33.
#: Position sizing is a function of quality in this platform, not a fixed cap.
RATING_MAX_POSITION: dict[str, float] = {
    "AAA": 0.08,
    "AA": 0.06,
    "A": 0.04,
    "BBB": 0.025,
    "BB": 0.01,
    "B": 0.0,
    "C": 0.0,
}
#: Applied when a holding has no rating at all — the most permissive band that
#: is still a real limit, so an unrated name cannot quietly become the largest
#: position in the book.
DEFAULT_MAX_POSITION = 0.04


@dataclass(frozen=True, slots=True)
class AlertRule:
    """One evaluable rule."""

    key: str
    label: str
    condition: str
    metric: str
    comparator: Comparator
    threshold: float | str | frozenset
    severity: AlertSeverity
    category: AlertCategory
    action: str
    #: Rules that read a portfolio-level metric rather than a per-holding one.
    scope: str = "position"
    #: When the threshold is itself a metric (e.g. the buy-zone price).
    threshold_metric: str | None = None
    enabled: bool = True

    def evaluate(self, metrics: Mapping[str, Any]) -> AlertEvaluation:
        observed = metrics.get(self.metric)
        threshold = (
            metrics.get(self.threshold_metric)
            if self.threshold_metric else self.threshold
        )

        base = AlertEvaluation(
            key=self.key, label=self.label, category=self.category,
            severity=self.severity, status=AlertStatus.CLEAR,
            condition=self.condition, action=self.action,
            observed=observed, threshold=threshold,
        )
        if observed is None or threshold is None:
            base.status = AlertStatus.UNAVAILABLE
            missing = self.metric if observed is None else self.threshold_metric
            base.detail = (
                f"'{missing}' is not available for this holding, so the rule "
                f"could not be evaluated. This is not a clear result."
            )
            return base

        base.status = (
            AlertStatus.TRIGGERED
            if self.comparator.evaluate(observed, threshold)
            else AlertStatus.CLEAR
        )
        return base


# ---------------------------------------------------------------------------
# The workbook's fourteen live rules — `38 Alerts` rows 9–22
# ---------------------------------------------------------------------------
LIVE_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        "price_below_buy_zone", "Price below buy zone",
        "CMP <= intrinsic × (1 − MoS)", "price", Comparator.LTE, 0.0,
        AlertSeverity.HIGH, AlertCategory.PRICE, "Accumulate",
        threshold_metric="buy_zone",
    ),
    AlertRule(
        "price_above_target", "Price above target",
        "CMP >= blended target", "price", Comparator.GTE, 0.0,
        AlertSeverity.HIGH, AlertCategory.PRICE, "Review / trim",
        threshold_metric="target_price",
    ),
    AlertRule(
        "upside_collapsed", "Upside collapsed",
        "Upside < 5%", "upside", Comparator.LT, 0.05,
        AlertSeverity.MEDIUM, AlertCategory.VALUATION, "Re-underwrite",
    ),
    AlertRule(
        "score_downgraded", "Score downgraded",
        "Score < 55", "score", Comparator.LT, 55.0,
        AlertSeverity.HIGH, AlertCategory.SCORE_CHANGE, "Reduce position",
    ),
    AlertRule(
        "rating_below_a", "Rating below A",
        "Rating is BBB or lower", "rating", Comparator.IN_SET,
        SUB_INVESTMENT_RATINGS,
        AlertSeverity.MEDIUM, AlertCategory.SCORE_CHANGE, "Review thesis",
    ),
    AlertRule(
        "risk_score_deteriorated", "Risk score deteriorated",
        "Risk score < 75%", "risk_score", Comparator.LT, 0.75,
        AlertSeverity.HIGH, AlertCategory.RISK, "Investigate flags",
    ),
    AlertRule(
        "red_flags_failed", "Red flags failed",
        "Severity points > 10", "red_flag_points", Comparator.GT, 10.0,
        AlertSeverity.HIGH, AlertCategory.RISK, "Read the risk dashboard",
    ),
    AlertRule(
        "leverage_stretched", "Leverage stretched",
        "Net debt / EBITDA > 3.0x", "net_debt_to_ebitda", Comparator.GT, 3.0,
        AlertSeverity.HIGH, AlertCategory.RISK, "Credit review",
    ),
    AlertRule(
        "interest_cover_thin", "Interest cover thin",
        "EBIT / interest < 3.0x", "interest_cover", Comparator.LT, 3.0,
        AlertSeverity.HIGH, AlertCategory.RISK, "Credit review",
    ),
    AlertRule(
        "promoter_pledge", "Promoter pledge",
        "Pledge > 0%", "promoter_pledge", Comparator.GT, 0.0,
        AlertSeverity.HIGH, AlertCategory.MANAGEMENT, "Governance review",
    ),
    AlertRule(
        "cash_conversion_weak", "Cash conversion weak",
        "CFO / PAT < 0.8x", "cash_conversion", Comparator.LT, 0.8,
        AlertSeverity.MEDIUM, AlertCategory.RISK, "Forensic review",
    ),
    AlertRule(
        "terminal_value_heavy", "Terminal value heavy",
        "TV > 75% of EV", "terminal_value_share", Comparator.GT, 0.75,
        AlertSeverity.MEDIUM, AlertCategory.DCF_CHANGE, "Stress the DCF",
    ),
    AlertRule(
        "model_integrity", "Model integrity",
        "All internal checks pass", "model_integrity_failures",
        Comparator.GT, 0.0,
        AlertSeverity.CRITICAL, AlertCategory.VALUATION, "Fix before use",
    ),
    AlertRule(
        "position_oversized", "Position oversized",
        "Weight > max position size for its rating", "weight",
        Comparator.GT, DEFAULT_MAX_POSITION,
        AlertSeverity.MEDIUM, AlertCategory.PORTFOLIO, "Rebalance",
        threshold_metric="max_position_size",
    ),
)

# ---------------------------------------------------------------------------
# Portfolio-level rules — `38 Alerts` rows 41–42 plus the brief's categories
# ---------------------------------------------------------------------------
PORTFOLIO_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        "portfolio_risk_low", "Portfolio risk score below 70%",
        "Weighted risk score < 70%", "portfolio_risk_score",
        Comparator.LT, 0.70,
        AlertSeverity.HIGH, AlertCategory.PORTFOLIO, "Review before trading",
        scope="portfolio",
    ),
    AlertRule(
        "diversification_low", "Diversification score below 45",
        "Diversification score < 45", "diversification_score",
        Comparator.LT, 45.0,
        AlertSeverity.HIGH, AlertCategory.PORTFOLIO, "Review before trading",
        scope="portfolio",
    ),
    AlertRule(
        "sector_overweight", "Sector overweight",
        "Any sector above 35%", "largest_sector_weight", Comparator.GT, 0.35,
        AlertSeverity.MEDIUM, AlertCategory.PORTFOLIO, "Rebalance sector exposure",
        scope="portfolio",
    ),
    AlertRule(
        "top5_concentrated", "Top-5 concentration above 50%",
        "Top-5 weight > 50%", "top_5_concentration", Comparator.GT, 0.50,
        AlertSeverity.MEDIUM, AlertCategory.PORTFOLIO, "Broaden the book",
        scope="portfolio",
    ),
    AlertRule(
        "too_few_effective_positions", "Fewer than 8 effective positions",
        "1/HHI < 8", "effective_positions", Comparator.LT, 8.0,
        AlertSeverity.MEDIUM, AlertCategory.PORTFOLIO, "Add breadth",
        scope="portfolio",
    ),
    AlertRule(
        "drawdown_deep", "Drawdown beyond 20%",
        "Max drawdown worse than −20%", "max_drawdown", Comparator.LT, -0.20,
        AlertSeverity.HIGH, AlertCategory.RISK, "Review risk posture",
        scope="portfolio",
    ),
    AlertRule(
        "cash_drag", "Cash above 25% of the book",
        "Cash weight > 25%", "cash_weight", Comparator.GT, 0.25,
        AlertSeverity.LOW, AlertCategory.PORTFOLIO, "Deploy or return capital",
        scope="portfolio",
    ),
)

# ---------------------------------------------------------------------------
# Event rules — the brief's document, result and corporate-action categories
# ---------------------------------------------------------------------------
EVENT_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        "new_document", "New filing ingested",
        "A document was processed since the last review", "days_since_document",
        Comparator.LTE, 7.0,
        AlertSeverity.LOW, AlertCategory.DOCUMENT, "Read the summary",
    ),
    AlertRule(
        "auditor_qualification", "Auditor qualification found",
        "An audit qualification was extracted", "auditor_qualification_found",
        Comparator.GT, 0.0,
        AlertSeverity.CRITICAL, AlertCategory.DOCUMENT, "Read the auditor's report",
    ),
    AlertRule(
        "quarterly_result_due", "Quarterly result imminent",
        "Result expected within 7 days", "days_to_result", Comparator.LTE, 7.0,
        AlertSeverity.LOW, AlertCategory.QUARTERLY_RESULT, "Prepare the review",
    ),
    AlertRule(
        "corporate_action_pending", "Corporate action pending",
        "An unprocessed corporate action exists", "pending_corporate_actions",
        Comparator.GT, 0.0,
        AlertSeverity.MEDIUM, AlertCategory.CORPORATE_ACTION, "Record the action",
    ),
)

ALL_RULES: tuple[AlertRule, ...] = LIVE_RULES + PORTFOLIO_RULES + EVENT_RULES
RULES_BY_KEY: dict[str, AlertRule] = {rule.key: rule for rule in ALL_RULES}


def max_position_for_rating(rating: str | None) -> float:
    """Position cap implied by an institutional rating."""
    if not rating:
        return DEFAULT_MAX_POSITION
    return RATING_MAX_POSITION.get(rating.upper().strip(), DEFAULT_MAX_POSITION)


class AlertEngine:
    """Evaluates rules against holdings and portfolio metrics."""

    def __init__(self, rules: Sequence[AlertRule] | None = None) -> None:
        self.rules = tuple(rules) if rules is not None else ALL_RULES

    def evaluate_position(
        self, ticker: str, company_id: str | None, metrics: Mapping[str, Any],
        *, name: str = "",
    ) -> list[AlertEvaluation]:
        """Run every position-scoped rule against one holding."""
        out: list[AlertEvaluation] = []
        for rule in self.rules:
            if rule.scope != "position" or not rule.enabled:
                continue
            evaluation = rule.evaluate(metrics)
            evaluation.ticker = ticker
            evaluation.company_id = company_id
            if name and evaluation.detail:
                evaluation.detail = f"{name}: {evaluation.detail}"
            out.append(evaluation)
        return out

    def evaluate_portfolio(
        self, metrics: Mapping[str, Any]
    ) -> list[AlertEvaluation]:
        return [
            rule.evaluate(metrics)
            for rule in self.rules
            if rule.scope == "portfolio" and rule.enabled
        ]

    @staticmethod
    def triggered(evaluations: Sequence[AlertEvaluation]) -> list[AlertEvaluation]:
        return sorted(
            (e for e in evaluations if e.is_triggered),
            key=lambda e: e.sort_key,
        )

    @staticmethod
    def summarise(evaluations: Sequence[AlertEvaluation]) -> dict[str, int]:
        """Counts by status and severity, for the dashboard header."""
        summary = {
            "total": len(evaluations),
            "triggered": 0,
            "clear": 0,
            "unavailable": 0,
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
        }
        for evaluation in evaluations:
            summary[evaluation.status.value] = (
                summary.get(evaluation.status.value, 0) + 1
            )
            if evaluation.is_triggered:
                summary[evaluation.severity.value] += 1
        return summary


def build_position_metrics(
    *,
    price: float | None = None,
    intrinsic_value: float | None = None,
    target_price: float | None = None,
    margin_of_safety: float = 0.20,
    score: float | None = None,
    rating: str | None = None,
    risk_score: float | None = None,
    red_flag_points: float | None = None,
    net_debt_to_ebitda: float | None = None,
    interest_cover: float | None = None,
    promoter_pledge: float | None = None,
    cash_conversion: float | None = None,
    terminal_value_share: float | None = None,
    model_integrity_failures: float | None = None,
    weight: float | None = None,
    days_since_document: float | None = None,
    auditor_qualification_found: float | None = None,
    days_to_result: float | None = None,
    pending_corporate_actions: float | None = None,
) -> dict[str, Any]:
    """Assemble the metric bag a position's rules read.

    Defined once, here, so the alert engine and the API cannot disagree about
    what a metric is called — a rule silently reading a key nobody populates
    would report `UNAVAILABLE` forever and look like missing data.
    """
    buy_zone = (
        intrinsic_value * (1.0 - margin_of_safety)
        if intrinsic_value is not None else None
    )
    upside = (
        (target_price / price - 1.0)
        if target_price is not None and price else None
    )
    return {
        "price": price,
        "buy_zone": buy_zone,
        "target_price": target_price,
        "upside": upside,
        "score": score,
        "rating": rating,
        "risk_score": risk_score,
        "red_flag_points": red_flag_points,
        "net_debt_to_ebitda": net_debt_to_ebitda,
        "interest_cover": interest_cover,
        "promoter_pledge": promoter_pledge,
        "cash_conversion": cash_conversion,
        "terminal_value_share": terminal_value_share,
        "model_integrity_failures": model_integrity_failures,
        "weight": weight,
        "max_position_size": max_position_for_rating(rating),
        "days_since_document": days_since_document,
        "auditor_qualification_found": auditor_qualification_found,
        "days_to_result": days_to_result,
        "pending_corporate_actions": pending_corporate_actions,
    }
