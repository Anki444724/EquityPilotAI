"""Deterministic financial answer engine.

Answers a small set of canonical financial questions directly from the
citations the existing :class:`ContextBuilder` already assembled. There is no
provider call, no RAG, no prompt, no new data source and no new calculation:
every figure in an answer is a number the platform has already computed, and
the ``[key]`` marker beside it is exactly the citation key the citation audit
resolves.

The consequences are structural, the same way grounding is structural in the
prompt path:

* a number the platform did not compute cannot appear — the answer is built
  from citations, so there is nothing else to quote;
* evidence that is missing is said to be missing, in so many words, instead
  of being filled in by a model that cannot tell the two apart;
* the answer text is canonical English and is verified downstream by the
  SAME citation audit, guardrail check, annotation and language pipeline a
  provider response passes through.

The engine is deliberately small. One method per intent, each of which reads
a fixed set of citation keys from the :class:`GroundedContext` and renders
sentences from what is there. It never re-derives a figure that
``AnalysisService``, ``RatioService``, ``ForecastService``,
``ValuationService`` or ``ContextBuilder`` already computed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.ai.types import Citation
from app.services.ai.context_builder import GroundedContext
from app.services.ai.financial_intent import FinancialIntent


@dataclass(frozen=True, slots=True)
class DeterministicAnswer:
    """A verified-by-construction answer to one canonical question."""

    intent: FinancialIntent
    #: Canonical English answer text, with ``[key]`` markers.
    content: str
    #: The citations actually cited by the answer, in order of first use.
    used_citations: list[Citation] = field(default_factory=list)
    #: Evidence keys the question expects but the context does not carry.
    #: Reported by the answer text itself; never filled in.
    missing: list[str] = field(default_factory=list)


def _render_value(citation: Citation) -> str:
    """A citation's value, formatted the way ``Citation.render()`` shows it.

    Percentages are stored as fractions platform-wide and shown in percentage
    points; every other figure gets two decimals and its unit. Keeping the
    formatting identical to the evidence block is what lets the citation
    audit match every number in the answer back to its source.
    """
    if citation.value is None:
        return "unavailable"
    if isinstance(citation.value, float):
        text = f"{citation.value * 100:,.2f}" if citation.unit == "%" else f"{citation.value:,.2f}"
    else:
        text = str(citation.value)
    if citation.unit == "x":
        return f"{text}x"
    unit = f" {citation.unit}" if citation.unit else ""
    return f"{text}{unit}"


def _fiscal_year(citation: Citation) -> str:
    return f"FY{str(citation.fiscal_year)[-2:]}" if citation.fiscal_year else ""


class FinancialAnswerEngine:
    """Answers canonical financial questions from existing citations only."""

    def answer(self, intent: FinancialIntent, context: GroundedContext) -> DeterministicAnswer:
        """Produce the deterministic answer for one resolved intent.

        Returns an answer for every supported intent. When the evidence is
        absent the answer says so explicitly — a question that was recognised
        as a canonical one deserves a direct, honest answer rather than a
        silent hand-back.
        """
        sentences, used, missing = getattr(self, f"_build_{intent.value}")(context)
        return DeterministicAnswer(
            intent=intent,
            content=" ".join(sentences),
            used_citations=used,
            missing=missing,
        )

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _find(citations: list[Citation], key: str) -> Citation | None:
        """The citation with `key`, or ``None`` when the platform has no such
        figure — which is the "unavailable" state, and never a blank to fill."""
        for citation in citations:
            if citation.key == key and citation.value is not None:
                return citation
        return None

    # --------------------------------------------------------------- intents
    def _build_pe(self, context: GroundedContext) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        pe = self._find(context.citations, "pe_ratio")
        price = self._find(context.citations, "price")
        eps = self._find(context.citations, "eps")

        if pe is None:
            return (
                [
                    f"The trailing P/E ratio is not available in the platform's "
                    f"current evidence for {name}. No P/E figure is estimated or "
                    "derived from other inputs — it is reported as unavailable.",
                ],
                [],
                ["pe_ratio"],
            )

        sentences = [
            f"The trailing price-to-earnings (P/E) ratio for {name} is "
            f"{_render_value(pe)} [pe_ratio].",
        ]
        used = [pe]
        if price is not None and eps is not None:
            sentences.append(
                f"It is the current market price {_render_value(price)} per share "
                f"[price] divided by earnings per share {_render_value(eps)} [eps] "
                "— how much the market pays for each unit of reported earnings."
            )
            used += [price, eps]
        else:
            sentences.append(
                "It compares the market price with earnings per share — how much "
                "the market pays for each unit of reported earnings."
            )
        sentences.append(
            "The P/E by itself is not a verdict: the platform does not call a "
            "stock cheap or expensive from that ratio alone."
        )
        return sentences, used, []

    def _build_pb(self, context: GroundedContext) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        pb = self._find(context.citations, "pb")

        if pb is None:
            # The ContextBuilder does not currently publish a canonical P/B
            # citation. The platform does not compute one here from price and
            # book value either — that would be a second, un-audited source of
            # the same figure — so the honest answer is "unavailable".
            return (
                [
                    f"The price-to-book (P/B) ratio is not available from the "
                    f"current canonical evidence for {name}.",
                    "The platform does not derive P/B from other figures, so it "
                    "is reported as unavailable rather than estimated.",
                ],
                [],
                ["pb"],
            )

        return (
            [
                f"The price-to-book (P/B) ratio for {name} is {_render_value(pb)} [pb].",
                "P/B compares the market price with the book value of equity per "
                "share — how much the market pays for each unit of net assets.",
            ],
            [pb],
            [],
        )

    def _build_debt(self, context: GroundedContext) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        gross = self._find(context.citations, "gross_debt")
        net = self._find(context.citations, "net_debt")
        multiple = self._find(context.citations, "net_debt_ebitda")

        sentences: list[str] = []
        used: list[Citation] = []
        missing: list[str] = []

        if gross is None:
            missing.append("gross_debt")
        if net is None:
            missing.append("net_debt")
        if multiple is None:
            missing.append("net_debt_ebitda")

        if gross is not None:
            year = f" for {_fiscal_year(gross)}" if gross.fiscal_year else ""
            sentences.append(
                f"Gross debt for {name} is {_render_value(gross)}{year} [gross_debt]."
            )
            used.append(gross)
        if net is not None:
            year = f" for {_fiscal_year(net)}" if net.fiscal_year else ""
            sentences.append(
                f"Net debt — gross debt less cash and current investments — is "
                f"{_render_value(net)}{year} [net_debt]."
            )
            used.append(net)
        if multiple is not None:
            sentences.append(
                f"Net debt is {multiple.value:,.2f} times EBITDA "
                f"[net_debt_ebitda], the platform's leverage multiple."
            )
            used.append(multiple)

        if not sentences:
            sentences = [
                f"Debt figures are not available in the platform's current "
                f"evidence for {name}. The debt position is reported as "
                "unavailable rather than estimated."
            ]
        elif missing:
            sentences.append(
                "Not available in this evidence set: "
                + ", ".join(
                    {"gross_debt": "gross debt", "net_debt": "net debt",
                     "net_debt_ebitda": "net debt / EBITDA"}[key]
                    for key in missing
                )
                + "."
            )
        return sentences, used, missing

    def _build_roe(self, context: GroundedContext) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        roe = self._find(context.citations, "roe_avg")

        if roe is None:
            return (
                [
                    f"Return on equity (ROE) is not available in the platform's "
                    f"current evidence for {name}.",
                ],
                [],
                ["roe_avg"],
            )

        year = f" for {_fiscal_year(roe)}" if roe.fiscal_year else ""
        sentences = [
            f"Return on equity (ROE) for {name} is {_render_value(roe)}{year} [roe_avg].",
        ]
        used = [roe]
        equity = self._find(context.citations, "equity")
        if equity is not None:
            equity_year = f" in {_fiscal_year(equity)}" if equity.fiscal_year else ""
            sentences.append(
                f"It measures the profit after tax earned per unit of shareholders' "
                f"equity ({_render_value(equity)}{equity_year} [equity]) [roe_avg]."
            )
            used.append(equity)
        sentences.append(
            "ROE is a profitability measure, not an investment recommendation."
        )
        return sentences, used, []

    def _build_roce(self, context: GroundedContext) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        roce = self._find(context.citations, "roce")

        if roce is None:
            return (
                [
                    f"Return on capital employed (ROCE) is not available in the "
                    f"platform's current evidence for {name}.",
                ],
                [],
                ["roce"],
            )

        year = f" for {_fiscal_year(roce)}" if roce.fiscal_year else ""
        return (
            [
                f"Return on capital employed (ROCE) for {name} is {_render_value(roce)}{year} [roce].",
                "ROCE measures the operating profit generated on the capital "
                "employed in the business — a gauge of capital productivity, "
                "not an investment recommendation.",
            ],
            [roce],
            [],
        )

    def _build_eps(self, context: GroundedContext) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        eps = self._find(context.citations, "eps")

        if eps is None:
            return (
                [
                    f"Earnings per share is not available in the platform's "
                    f"current evidence for {name}.",
                ],
                [],
                ["eps"],
            )

        year = f" for {_fiscal_year(eps)}" if eps.fiscal_year else ""
        return (
            [
                f"Earnings per share (basic) for {name} is {_render_value(eps)}{year} [eps].",
                "EPS is the platform's reported profit after tax expressed per "
                "share; it is not recomputed here, and it is the earnings figure "
                "the trailing P/E ratio divides into the market price.",
            ],
            [eps],
            [],
        )

    def _build_market_price(self, context: GroundedContext) -> tuple[list[str], list[Citation], list[str]]:
        price = self._find(context.citations, "price")
        market_cap = self._find(context.citations, "market_cap")

        if price is None:
            sentences = [
                f"The current market price is not available in the platform's "
                f"current evidence for {context.name}.",
            ]
            used: list[Citation] = []
            if market_cap is not None:
                sentences.append(
                    f"The recorded market capitalisation is {_render_value(market_cap)} "
                    f"[market_cap]."
                )
                used.append(market_cap)
            return sentences, used, ["price"]

        sentences = [
            f"The current market price of {context.name} ({context.ticker}) is "
            f"{_render_value(price)} [price].",
        ]
        used = [price]
        if market_cap is not None:
            sentences.append(
                f"The market capitalisation is {_render_value(market_cap)} [market_cap]."
            )
            used.append(market_cap)
        return sentences, used, []

    def _build_revenue_growth(self, context: GroundedContext) -> tuple[list[str], list[Citation], list[str]]:
        return self._build_growth(
            context, key="revenue_growth", metric="Revenue",
            level_key="revenue", level_name="total revenue",
        )

    def _build_profit_growth(self, context: GroundedContext) -> tuple[list[str], list[Citation], list[str]]:
        return self._build_growth(
            context, key="pat_growth", metric="Profit after tax (PAT)",
            level_key="pat", level_name="profit after tax",
        )

    def _build_growth(
        self, context: GroundedContext, *, key: str, metric: str,
        level_key: str, level_name: str,
    ) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        growth = self._find(context.citations, key)

        if growth is None:
            return (
                [
                    f"{metric} growth is not available in the platform's current "
                    f"evidence for {name}.",
                    "The platform computes year-on-year growth from reported "
                    "statements; it does not estimate it here.",
                ],
                [],
                [key],
            )

        year = f" for {_fiscal_year(growth)}" if growth.fiscal_year else ""
        sentences = [
            f"{metric} growth for {name} is {_render_value(growth)} year on year{year} [{key}].",
        ]
        used = [growth]
        level = self._find(context.citations, level_key)
        if level is not None:
            level_year = f" in {_fiscal_year(level)}" if level.fiscal_year else ""
            sentences.append(
                f"It is the platform's computed change in {level_name} "
                f"({_render_value(level)}{level_year} [{level_key}]) versus the "
                "prior fiscal year."
            )
            used.append(level)
        return sentences, used, []

    # -------------------------------------------------------------- valuation
    #: Valuation evidence, in the order the platform computes it. `data_quality`
    #: is an annotation on the whole bundle, not a missing item when absent.
    _VALUATION_EVIDENCE: tuple[tuple[str, str], ...] = (
        ("wacc", "the weighted average cost of capital (WACC)"),
        ("cost_of_equity", "the cost of equity"),
        ("dcf_value", "the DCF intrinsic value"),
        ("dcf_upside", "the DCF upside"),
        ("relative_target", "the blended relative-valuation target"),
        ("pe_ratio", "the trailing P/E"),
        ("ev_ebitda", "the EV/EBITDA multiple"),
        ("weighted_value", "the weighted intrinsic value"),
        ("valuation_upside", "the upside to intrinsic value"),
        ("valuation_recommendation", "the platform's valuation recommendation"),
    )

    def _build_valuation(self, context: GroundedContext) -> tuple[list[str], list[Citation], list[str]]:
        name, ticker = context.name, context.ticker
        present: dict[str, Citation] = {
            key: citation
            for key, _ in self._VALUATION_EVIDENCE
            for citation in [self._find(context.citations, key)]
            if citation is not None
        }
        quality = self._find(context.citations, "data_quality")

        if not present:
            return (
                [
                    f"Valuation outputs are not available in the platform's "
                    f"current evidence for {name}. The platform does not estimate "
                    "a value or an upside here — the valuation picture is "
                    "reported as unavailable rather than invented.",
                ],
                [],
                [key for key, _ in self._VALUATION_EVIDENCE],
            )

        sentences = [
            f"The platform's computed valuation picture for {name} ({ticker}) "
            "rests on the evidence below.",
        ]
        used: list[Citation] = []

        def cite(key: str) -> str:
            """The rendered value of a present citation, recording its use."""
            citation = present[key]
            if citation not in used:
                used.append(citation)
            return f"{_render_value(citation)} [{key}]"

        if "wacc" in present:
            sentence = f"The weighted average cost of capital (WACC) is {cite('wacc')}"
            if "cost_of_equity" in present:
                sentence += f", with a cost of equity of {_render_value(present['cost_of_equity'])} [cost_of_equity]"
                if present["cost_of_equity"] not in used:
                    used.append(present["cost_of_equity"])
            sentences.append(sentence + ".")
        if "dcf_value" in present:
            sentence = f"The DCF intrinsic value is {cite('dcf_value')} per share"
            if "dcf_upside" in present:
                sentence += f", i.e. {_render_value(present['dcf_upside'])} versus the current market price [dcf_upside]"
                if present["dcf_upside"] not in used:
                    used.append(present["dcf_upside"])
            sentences.append(sentence + ".")
        if "relative_target" in present:
            sentences.append(
                f"The blended relative-valuation target is {cite('relative_target')} per share."
            )
        multiples = [
            f"a trailing P/E of {_render_value(present['pe_ratio'])} [pe_ratio]"
            if "pe_ratio" in present else None,
            f"an EV/EBITDA of {_render_value(present['ev_ebitda'])} [ev_ebitda]"
            if "ev_ebitda" in present else None,
        ]
        if any(part is not None for part in multiples):
            for key in ("pe_ratio", "ev_ebitda"):
                if key in present and present[key] not in used:
                    used.append(present[key])
            sentences.append(
                "The current reference multiples are "
                + " and ".join(part for part in multiples if part is not None)
                + "."
            )
        if "weighted_value" in present:
            sentence = f"The weighted intrinsic value is {cite('weighted_value')} per share"
            if "valuation_upside" in present:
                sentence += f", {_render_value(present['valuation_upside'])} versus the current market price [valuation_upside]"
                if present["valuation_upside"] not in used:
                    used.append(present["valuation_upside"])
            sentences.append(sentence + ".")
        if "valuation_recommendation" in present:
            citation = present["valuation_recommendation"]
            if citation not in used:
                used.append(citation)
            sentences.append(
                f"The platform's valuation recommendation is '{citation.value}' "
                f"[valuation_recommendation]."
            )
        if quality is not None:
            if quality not in used:
                used.append(quality)
            sentences.append(
                f"The data-quality engine grades this valuation input as "
                f"'{quality.value}' [data_quality]."
            )

        missing = [
            label
            for key, label in self._VALUATION_EVIDENCE
            if key not in present
        ]
        if missing:
            sentences.append("Not available in this evidence set: " + ", ".join(missing) + ".")
        if not any(key in present for key in ("dcf_value", "relative_target", "weighted_value")):
            sentences.append(
                "The core valuation outputs are unavailable, so no value or "
                "upside is stated beyond the cited figures."
            )
        if "pe_ratio" in present:
            sentences.append(
                "The P/E is one input among these and is not treated, by itself, "
                "as a verdict on the stock's value."
            )
        return sentences, used, missing
