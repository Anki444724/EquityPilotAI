"""Relative valuation.

Three layers, in increasing order of rigour:

1. **Current trading multiples** — what the market pays today, trailing and
   forward.
2. **Target-price multiples** — a chosen multiple applied to a forward metric.
3. **Justified multiples** — the multiple a company *deserves* given its
   growth, payout and cost of equity, derived from the Gordon model rather
   than borrowed from a peer group.

The third layer is what separates institutional relative valuation from
multiple-matching. A peer trading at 30x tells you nothing about whether 30x is
warranted; the justified multiple does.

    Justified forward P/E  = (1 − b) / (Ke − g)
    Justified trailing P/E = (1 − b)(1 + g) / (Ke − g)
    Justified P/B          = (ROE − g) / (Ke − g)
    Justified EV/EBITDA    = (1 − t)(1 − RR) / (WACC − g)

where b is the retention ratio and RR the reinvestment rate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median

from app.domain.calc import safe_div


class MultipleBasis(StrEnum):
    EQUITY = "equity"        # applied to a per-share or equity metric
    ENTERPRISE = "enterprise"  # applied to an EV metric, needs a bridge


#: A justified multiple explodes as g approaches Ke, exactly as the DCF
#: perpetuity does. The same guard applies.
MIN_SPREAD = 0.005


@dataclass(frozen=True, slots=True)
class MultipleSet:
    """Observed trading multiples for one period."""

    label: str
    pe: float | None = None
    pb: float | None = None
    ev_ebitda: float | None = None
    ev_sales: float | None = None
    ev_ebit: float | None = None
    p_fcfe: float | None = None
    dividend_yield: float | None = None
    peg: float | None = None


@dataclass(frozen=True, slots=True)
class TargetPriceMethod:
    """One valuation method's contribution to the blended target."""

    key: str
    label: str
    basis: str
    target_multiple: float | None
    metric: float | None
    metric_label: str
    implied_value: float | None          # EV or equity value, per basis
    target_price: float | None
    weight: float
    rationale: str


@dataclass(frozen=True, slots=True)
class JustifiedMultiple:
    key: str
    label: str
    formula: str
    justified: float | None
    actual: float | None
    #: actual / justified − 1. Negative means the market pays less than warranted.
    premium_discount: float | None
    verdict: str


@dataclass(frozen=True, slots=True)
class RelativeValuationResult:
    current: MultipleSet
    forward: list[MultipleSet]
    methods: list[TargetPriceMethod]
    justified: list[JustifiedMultiple]

    blended_target_price: float | None
    simple_average_target: float | None
    median_target: float | None
    target_low: float | None
    target_high: float | None
    upside: float | None
    current_price: float | None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RelativeInputs:
    """Market data and forecast metrics needed for relative valuation."""

    current_price: float | None
    shares_outstanding: float
    market_cap: float
    gross_debt: float
    cash_and_equivalents: float

    # trailing (last reported)
    trailing_eps: float | None
    trailing_bvps: float | None
    trailing_ebitda: float | None
    trailing_revenue: float | None
    trailing_ebit: float | None
    trailing_fcfe: float | None
    trailing_dividend_per_share: float | None

    # forward (from the forecast engine)
    forward_eps: tuple[float | None, ...] = ()
    forward_bvps: tuple[float | None, ...] = ()
    forward_ebitda: tuple[float | None, ...] = ()
    forward_revenue: tuple[float | None, ...] = ()
    forward_ebit: tuple[float | None, ...] = ()
    forward_fcfe: tuple[float | None, ...] = ()

    # target multiples (analyst or peer-derived)
    target_pe: float = 20.0
    target_pb: float = 3.0
    target_ev_ebitda: float = 12.0
    target_ev_sales: float = 2.5
    target_peg: float = 1.5

    # inputs for justified multiples
    cost_of_equity: float = 0.14
    wacc: float = 0.115
    terminal_growth: float = 0.05
    payout_ratio: float = 0.20
    roe: float | None = None
    reinvestment_rate: float | None = None
    tax_rate: float = 0.25
    eps_cagr: float | None = None

    #: DCF value per share, included as one method in the blend.
    dcf_value_per_share: float | None = None

    #: Weights per method key. Missing keys default to zero.
    method_weights: dict[str, float] = field(default_factory=dict)


DEFAULT_WEIGHTS = {
    "pe": 0.25,
    "pb": 0.10,
    "ev_ebitda": 0.25,
    "ev_sales": 0.05,
    "peg": 0.05,
    "dcf": 0.30,
}


def _enterprise_value(inputs: RelativeInputs) -> float:
    return inputs.market_cap + inputs.gross_debt - inputs.cash_and_equivalents


def _first(values: tuple[float | None, ...]) -> float | None:
    return values[0] if values else None


def compute_multiples(inputs: RelativeInputs) -> tuple[MultipleSet, list[MultipleSet]]:
    """Trailing and forward trading multiples."""
    ev = _enterprise_value(inputs)
    price = inputs.current_price

    current = MultipleSet(
        label="Trailing",
        pe=safe_div(price, inputs.trailing_eps),
        pb=safe_div(price, inputs.trailing_bvps),
        ev_ebitda=safe_div(ev, inputs.trailing_ebitda),
        ev_sales=safe_div(ev, inputs.trailing_revenue),
        ev_ebit=safe_div(ev, inputs.trailing_ebit),
        p_fcfe=safe_div(inputs.market_cap, inputs.trailing_fcfe),
        dividend_yield=safe_div(inputs.trailing_dividend_per_share, price),
        peg=safe_div(safe_div(price, inputs.trailing_eps), (inputs.eps_cagr or 0) * 100)
        if inputs.eps_cagr else None,
    )

    forward: list[MultipleSet] = []
    horizon = max(
        len(inputs.forward_eps), len(inputs.forward_ebitda), len(inputs.forward_revenue)
    )
    for i in range(min(3, horizon)):
        def pick(seq: tuple[float | None, ...]) -> float | None:
            return seq[i] if i < len(seq) else None

        forward.append(
            MultipleSet(
                label=f"FY+{i + 1}E",
                pe=safe_div(price, pick(inputs.forward_eps)),
                pb=safe_div(price, pick(inputs.forward_bvps)),
                ev_ebitda=safe_div(ev, pick(inputs.forward_ebitda)),
                ev_sales=safe_div(ev, pick(inputs.forward_revenue)),
                ev_ebit=safe_div(ev, pick(inputs.forward_ebit)),
                p_fcfe=safe_div(inputs.market_cap, pick(inputs.forward_fcfe)),
            )
        )
    return current, forward


def compute_target_prices(inputs: RelativeInputs) -> list[TargetPriceMethod]:
    """Target price under each methodology."""
    shares = inputs.shares_outstanding
    net_debt = inputs.gross_debt - inputs.cash_and_equivalents
    weights = {**DEFAULT_WEIGHTS, **inputs.method_weights}

    def equity_method(key, label, multiple, metric, metric_label, rationale):
        target = multiple * metric if metric is not None else None
        return TargetPriceMethod(
            key=key, label=label, basis=MultipleBasis.EQUITY.value,
            target_multiple=multiple, metric=metric, metric_label=metric_label,
            implied_value=target * shares if target is not None else None,
            target_price=target, weight=weights.get(key, 0.0), rationale=rationale,
        )

    def ev_method(key, label, multiple, metric, metric_label, rationale):
        ev = multiple * metric if metric is not None else None
        equity = ev - net_debt if ev is not None else None
        return TargetPriceMethod(
            key=key, label=label, basis=MultipleBasis.ENTERPRISE.value,
            target_multiple=multiple, metric=metric, metric_label=metric_label,
            implied_value=ev,
            target_price=safe_div(equity, shares), weight=weights.get(key, 0.0),
            rationale=rationale,
        )

    methods = [
        equity_method("pe", "P/E based", inputs.target_pe, _first(inputs.forward_eps),
                      "FY+1E EPS", "Target multiple applied to forward earnings."),
        equity_method("pb", "P/B based", inputs.target_pb, _first(inputs.forward_bvps),
                      "FY+1E book value per share",
                      "ROE-justified book multiple."),
        ev_method("ev_ebitda", "EV/EBITDA based", inputs.target_ev_ebitda,
                  _first(inputs.forward_ebitda), "FY+1E EBITDA",
                  "Capital-structure neutral; bridges EV to equity."),
        ev_method("ev_sales", "EV/Sales based", inputs.target_ev_sales,
                  _first(inputs.forward_revenue), "FY+1E revenue",
                  "Cross-check when margins are not yet normalised."),
    ]

    # PEG: price = PEG × growth(%) × EPS
    eps = _first(inputs.forward_eps)
    if inputs.eps_cagr and eps:
        peg_price = inputs.target_peg * (inputs.eps_cagr * 100) * eps
        methods.append(
            TargetPriceMethod(
                key="peg", label="PEG based", basis=MultipleBasis.EQUITY.value,
                target_multiple=inputs.target_peg, metric=eps, metric_label="FY+1E EPS",
                implied_value=peg_price * shares, target_price=peg_price,
                weight=weights.get("peg", 0.0),
                rationale="Growth-adjusted earnings multiple.",
            )
        )

    if inputs.dcf_value_per_share is not None:
        methods.append(
            TargetPriceMethod(
                key="dcf", label="DCF (intrinsic)", basis=MultipleBasis.EQUITY.value,
                target_multiple=None, metric=None, metric_label="discounted cash flow",
                implied_value=inputs.dcf_value_per_share * shares,
                target_price=inputs.dcf_value_per_share,
                weight=weights.get("dcf", 0.0),
                rationale="Cash-flow based intrinsic value.",
            )
        )
    return methods


def sustainable_payout(terminal_growth: float, roe: float | None) -> float | None:
    """Payout ratio consistent with a given growth rate and ROE.

    The Gordon identity is g = ROE × retention, so retention = g / ROE and
    payout = 1 − g / ROE. Supplying an arbitrary payout alongside an unrelated
    growth rate describes a company that cannot exist: a firm earning 18% ROE
    and retaining 75% of earnings grows at 13.5%, not at the 5% an analyst may
    have typed into the terminal-growth box.

    Justified multiples are only meaningful when the two are consistent, so the
    payout is derived from growth rather than taken at face value.
    """
    if roe is None or roe <= 0:
        return None
    retention = terminal_growth / roe
    if retention >= 1:
        # Growth exceeds ROE: unachievable without external equity.
        return None
    return 1 - retention


def compute_justified_multiples(inputs: RelativeInputs) -> list[JustifiedMultiple]:
    """Multiples derived from fundamentals rather than from peers."""
    g = inputs.terminal_growth
    spread_e = inputs.cost_of_equity - g
    spread_f = inputs.wacc - g

    # Prefer the payout implied by the sustainable-growth identity; fall back
    # to the reported payout only when ROE is unavailable.
    derived_payout = sustainable_payout(g, inputs.roe)
    payout = derived_payout if derived_payout is not None else inputs.payout_ratio

    current, _ = compute_multiples(inputs)
    out: list[JustifiedMultiple] = []

    def add(key, label, formula, justified, actual):
        premium = safe_div(actual, justified) - 1 if (justified and actual) else None
        if premium is None:
            verdict = "Not measurable"
        elif premium < -0.10:
            verdict = "Undervalued vs fundamentals"
        elif premium > 0.10:
            verdict = "Overvalued vs fundamentals"
        else:
            verdict = "Fairly valued"
        out.append(JustifiedMultiple(key, label, formula, justified, actual, premium, verdict))

    if spread_e > MIN_SPREAD:
        add("forward_pe", "Justified forward P/E", "(1 − b) / (Ke − g)",
            payout / spread_e, current.pe)
        add("trailing_pe", "Justified trailing P/E", "(1 − b)(1 + g) / (Ke − g)",
            payout * (1 + g) / spread_e, current.pe)
        if inputs.roe is not None:
            add("pb", "Justified P/B", "(ROE − g) / (Ke − g)",
                (inputs.roe - g) / spread_e, current.pb)
    else:
        out.append(JustifiedMultiple(
            "forward_pe", "Justified forward P/E", "(1 − b) / (Ke − g)",
            None, current.pe, None,
            "Not measurable — growth too close to the cost of equity",
        ))

    if spread_f > MIN_SPREAD and inputs.reinvestment_rate is not None:
        justified_ev_ebitda = (
            (1 - inputs.tax_rate) * (1 - inputs.reinvestment_rate) / spread_f
        )
        add("ev_ebitda", "Justified EV/EBITDA", "(1 − t)(1 − RR) / (WACC − g)",
            justified_ev_ebitda, current.ev_ebitda)
    return out


def run_relative_valuation(inputs: RelativeInputs) -> RelativeValuationResult:
    """Full relative valuation: multiples, targets and justified multiples."""
    warnings: list[str] = []
    current, forward = compute_multiples(inputs)
    methods = compute_target_prices(inputs)
    justified = compute_justified_multiples(inputs)

    # Flag an incoherent growth/payout pair rather than silently overriding it.
    derived = sustainable_payout(inputs.terminal_growth, inputs.roe)
    if derived is not None and abs(derived - inputs.payout_ratio) > 0.15:
        warnings.append(
            f"Reported payout ({inputs.payout_ratio:.0%}) is inconsistent with "
            f"{inputs.terminal_growth:.1%} growth at {inputs.roe:.1%} ROE, which implies a "
            f"{derived:.0%} payout. Justified multiples use the implied payout."
        )
    if inputs.roe is not None and inputs.roe > 0 and inputs.terminal_growth >= inputs.roe:
        warnings.append(
            f"Terminal growth ({inputs.terminal_growth:.1%}) is at or above ROE "
            f"({inputs.roe:.1%}); such growth cannot be self-funded."
        )

    priced = [m for m in methods if m.target_price is not None and m.target_price > 0]
    if not priced:
        warnings.append("No valuation method produced a positive target price.")

    weight_total = sum(m.weight for m in priced)
    blended = (
        sum(m.target_price * m.weight for m in priced) / weight_total
        if weight_total > 0 else None
    )
    prices = [m.target_price for m in priced]

    upside = (
        safe_div(blended, inputs.current_price) - 1
        if blended is not None and inputs.current_price else None
    )

    if prices and max(prices) > 5 * min(prices):
        warnings.append(
            "Target prices span more than a 5x range across methods; the blend is "
            "unreliable and the individual methods should be reviewed."
        )

    return RelativeValuationResult(
        current=current,
        forward=forward,
        methods=methods,
        justified=justified,
        blended_target_price=blended,
        simple_average_target=sum(prices) / len(prices) if prices else None,
        median_target=median(prices) if prices else None,
        target_low=min(prices) if prices else None,
        target_high=max(prices) if prices else None,
        upside=upside,
        current_price=inputs.current_price,
        warnings=warnings,
    )
