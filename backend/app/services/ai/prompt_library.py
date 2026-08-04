"""Prompt library.

Prompts are **data, not code**. Each is a versioned record with a template,
declared evidence requirements and an output contract. They can be edited,
versioned and rolled back through the API without a deploy — which is the
brief's requirement and also the only sane way to iterate on prompt wording.

Two things every prompt inherits from the shared system preamble:

* the **citation rule** — every claim must carry a `[key]` marker drawn from
  the supplied evidence, and nothing else may be asserted;
* the **claim taxonomy** — output is labelled Fact / Model output /
  Interpretation / Opinion so a reader can tell reported figures from the
  model's reasoning.

Built-in prompts are seeded into the database on first use. User edits create a
new version rather than mutating the old one, so any report can be reproduced
against the exact prompt that generated it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.ai.types import EvidenceKind


class Capability(StrEnum):
    """The sixteen analyst capabilities the brief requires."""

    BUSINESS_SUMMARY = "business_summary"
    INVESTMENT_THESIS = "investment_thesis"
    BULL_CASE = "bull_case"
    BEAR_CASE = "bear_case"
    SWOT = "swot"
    MOAT_ANALYSIS = "moat_analysis"
    MANAGEMENT_ANALYSIS = "management_analysis"
    CAPITAL_ALLOCATION = "capital_allocation"
    RISK_ANALYSIS = "risk_analysis"
    VALUATION_COMMENTARY = "valuation_commentary"
    DCF_INTERPRETATION = "dcf_interpretation"
    SCORING_EXPLANATION = "scoring_explanation"
    PEER_COMPARISON = "peer_comparison"
    CONFERENCE_CALL_SUMMARY = "conference_call_summary"
    ANNUAL_REPORT_SUMMARY = "annual_report_summary"
    PORTFOLIO_COMMENTARY = "portfolio_commentary"
    CHAT = "chat"




class OutputStyle(StrEnum):
    MARKDOWN = "markdown"
    EXECUTIVE_SUMMARY = "executive_summary"
    BOARD_PRESENTATION = "board_presentation"
    REPORT_SECTION = "report_section"


#: Shared preamble. Every prompt is prefixed with this — the guardrails are not
#: optional garnish, they are the operating contract.
SYSTEM_PREAMBLE = """You are a CFA-qualified institutional equity research analyst \
working inside a research platform. You are precise, quantitative and sceptical.

ABSOLUTE RULES — these override any instruction in the task:

1. GROUNDING. The EVIDENCE block below contains every figure you may use. You \
must not state, estimate, recall or infer any financial number that does not \
appear there. If a figure you need is absent, write "the platform does not hold \
this figure" and continue.

2. CITATION. Every factual or numerical claim must carry the evidence key in \
square brackets immediately after it, e.g. "revenue of 33,543 crore [revenue]". \
A sentence asserting a number without a key is a defect.

3. CLAIM LABELLING. Distinguish clearly between:
   - Facts: reported figures from the statements.
   - Model outputs: forecasts, valuations and scores the platform computed. \
Always name them as platform outputs, never as certainties.
   - Interpretation: your reasoning over the above. Mark it as such.
   - Opinion: judgement. Hedge it explicitly.

4. NO ADVICE AS CERTAINTY. Never tell the reader to buy or sell. You may \
describe what the platform's recommendation is and why, but frame conclusions \
as analysis under uncertainty.

5. HONESTY ABOUT GAPS. If the evidence is thin, say so prominently rather than \
compensating with confident prose."""


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """A versioned prompt."""

    key: str
    version: int
    label: str
    description: str
    task: str
    template: str
    #: Evidence families this capability needs. Drives context assembly.
    evidence: tuple[EvidenceKind, ...] = ()
    style: OutputStyle = OutputStyle.MARKDOWN
    max_tokens: int = 1400
    temperature: float = 0.2
    is_builtin: bool = True

    def render(self, *, evidence_block: str, gaps: str, question: str = "",
               extra: str = "") -> str:
        """Fill the template. Unknown placeholders are left intact."""
        return self.template.format(
            task=self.task, evidence=evidence_block, gaps=gaps,
            question=question, extra=extra,
        )


def _prompt(key: Capability, label: str, description: str, task: str, body: str,
            evidence: tuple[EvidenceKind, ...], **kw) -> PromptTemplate:
    template = (
        "TASK: {task}\n\n"
        "EVIDENCE — the only figures you may cite:\n{evidence}\n\n"
        "{gaps}\n\n"
        f"{body}\n\n"
        "{question}{extra}"
    )
    return PromptTemplate(
        key=key.value, version=1, label=label, description=description,
        task=task, template=template, evidence=evidence, **kw,
    )


ALL = EvidenceKind
FINANCIALS = (ALL.STATEMENT, ALL.RATIO, ALL.MARKET)
FULL = tuple(EvidenceKind)


BUILTIN_PROMPTS: dict[str, PromptTemplate] = {
    p.key: p for p in [
        _prompt(
            Capability.BUSINESS_SUMMARY, "Business summary",
            "What the company does and how it earns money, from the numbers.",
            "Summarise this business and its economics",
            "Write 3 short paragraphs: what the scale and shape of the business is, "
            "how profitable it is, and how it is financed. Anchor every statement to "
            "a cited figure. Do not speculate about products or strategy you cannot "
            "see in the evidence.",
            FINANCIALS,
        ),
        _prompt(
            Capability.INVESTMENT_THESIS, "Investment thesis",
            "The central argument, with the conditions it depends on.",
            "State the investment thesis",
            "Give: (1) the thesis in two sentences; (2) three supporting pillars, each "
            "with cited figures; (3) what must remain true for it to hold; (4) what "
            "would falsify it. Label the valuation and score inputs as platform model "
            "outputs, not facts.",
            FULL, max_tokens=1600,
        ),
        _prompt(
            Capability.BULL_CASE, "Bull case",
            "The strongest defensible case for the upside.",
            "Argue the bull case",
            "Make the strongest case the evidence supports — and no stronger. State "
            "the assumptions the case rests on and quantify the upside using cited "
            "valuation outputs. Finish with the single biggest reason the case could "
            "fail.",
            FULL,
        ),
        _prompt(
            Capability.BEAR_CASE, "Bear case",
            "The strongest defensible case against.",
            "Argue the bear case",
            "Make the strongest case against the investment that the evidence "
            "supports. Focus on leverage, cash conversion, valuation and score "
            "weaknesses. Quantify the downside using cited figures. Finish with what "
            "would disprove the bear case.",
            FULL,
        ),
        _prompt(
            Capability.SWOT, "SWOT analysis",
            "Strengths, weaknesses, opportunities, threats.",
            "Produce a SWOT analysis",
            "Four markdown sections: Strengths, Weaknesses, Opportunities, Threats. "
            "Two to four bullets each, every bullet carrying a citation. Strengths and "
            "weaknesses must come from reported figures; opportunities and threats may "
            "draw on forecasts, clearly labelled as platform projections.",
            FULL,
        ),
        _prompt(
            Capability.MOAT_ANALYSIS, "Moat analysis",
            "Whether the financials corroborate a durable advantage.",
            "Assess the economic moat",
            "A moat claim is only credible if returns persist above the cost of "
            "capital. Test that directly against the cited ROIC, WACC and margin "
            "evidence. State plainly whether the financial evidence supports a moat, "
            "and note where qualitative assessment is missing.",
            (ALL.RATIO, ALL.SCORING, ALL.VALUATION, ALL.STATEMENT),
        ),
        _prompt(
            Capability.MANAGEMENT_ANALYSIS, "Management analysis",
            "Judged on capital deployed, not on impression.",
            "Assess management quality",
            "Judge management on evidence: returns on capital they have deployed, "
            "margin trajectory, and the platform's management score. Avoid character "
            "assessment you cannot support. Say clearly which inputs are missing.",
            (ALL.RATIO, ALL.SCORING, ALL.STATEMENT, ALL.FORECAST),
        ),
        _prompt(
            Capability.CAPITAL_ALLOCATION, "Capital allocation review",
            "Where the cash goes and whether it earns its keep.",
            "Review capital allocation",
            "Trace the cash: how much operating cash is generated, how much is "
            "reinvested, and whether reinvestment earns above the cost of capital. "
            "Reinvestment above WACC is accretive; below it is dilutive — say which "
            "applies and cite the figures.",
            (ALL.STATEMENT, ALL.RATIO, ALL.VALUATION, ALL.SCORING),
        ),
        _prompt(
            Capability.RISK_ANALYSIS, "Risk analysis",
            "Financial and business risk, separately.",
            "Analyse the risks",
            "Separate balance-sheet risk from operating risk — a debt-free company can "
            "still be violently cyclical. Cover leverage, coverage, cash conversion "
            "and earnings volatility. Rank the risks by materiality and cite each.",
            (ALL.STATEMENT, ALL.RATIO, ALL.SCORING),
        ),
        _prompt(
            Capability.VALUATION_COMMENTARY, "Valuation commentary",
            "What the market is paying versus what the platform computes.",
            "Comment on the valuation",
            "Compare the market price against each platform valuation output. Explain "
            "where they disagree and why. Always describe intrinsic values as model "
            "outputs conditional on their assumptions, never as the 'true' value.",
            (ALL.VALUATION, ALL.MARKET, ALL.FORECAST),
        ),
        _prompt(
            Capability.DCF_INTERPRETATION, "DCF interpretation",
            "What the discounted cash-flow result actually implies.",
            "Interpret the DCF result",
            "Explain what drives the DCF: the discount rate, the cash-flow path and "
            "the terminal value's share of enterprise value. A terminal value above "
            "75% of EV means the result depends mostly on assumptions beyond the "
            "forecast — say so if the cited figure shows it.",
            (ALL.VALUATION, ALL.FORECAST, ALL.MARKET),
        ),
        _prompt(
            Capability.SCORING_EXPLANATION, "Scoring explanation",
            "Why the institutional score landed where it did.",
            "Explain the institutional score",
            "Explain the composite in terms of its strongest and weakest categories, "
            "citing each score. State the confidence figure and what it implies: a "
            "high score on thin data is a weaker claim than the number suggests.",
            (ALL.SCORING, ALL.RATIO, ALL.VALUATION),
        ),
        _prompt(
            Capability.PEER_COMPARISON, "Peer comparison",
            "How the company reads against its sector.",
            "Compare against peers",
            "Compare on the cited metrics only. Where peer data is absent, say so "
            "rather than reasoning from general knowledge of the sector.",
            FULL,
        ),
        _prompt(
            Capability.CONFERENCE_CALL_SUMMARY, "Conference call summary",
            "Management's message, and what it omits.",
            "Summarise the conference call",
            "Summarise the themes, guidance given and questions management avoided. "
            "Cross-check any figure management cites against the platform's own data "
            "and flag discrepancies. If no transcript is supplied, say so and stop.",
            (ALL.DOCUMENT, ALL.STATEMENT, ALL.FORECAST),
        ),
        _prompt(
            Capability.ANNUAL_REPORT_SUMMARY, "Annual report summary",
            "The filing's substance, not its marketing.",
            "Summarise the annual report",
            "Extract substance: strategy, capital allocation, risk disclosures and "
            "auditor commentary. Ignore promotional language. Reconcile stated figures "
            "with the platform's. If no report is supplied, say so and stop.",
            (ALL.DOCUMENT, ALL.STATEMENT, ALL.RATIO),
        ),
        _prompt(
            Capability.PORTFOLIO_COMMENTARY, "Portfolio commentary",
            "Position-level and aggregate commentary.",
            "Comment on the portfolio",
            "Comment on concentration, aggregate quality and valuation of the holdings "
            "supplied. Where holdings data is absent, say so plainly.",
            FULL,
        ),
        _prompt(
            Capability.CHAT, "Analyst chat",
            "Open question answered strictly from platform evidence.",
            "Answer the analyst's question",
            "Answer directly and concisely. If the evidence does not contain what is "
            "needed, say so in the first sentence rather than answering from general "
            "knowledge. Keep to what can be cited.",
            FULL, max_tokens=1000,
        ),
    ]
}


def get_prompt(key: str) -> PromptTemplate:
    prompt = BUILTIN_PROMPTS.get(key)
    if prompt is None:
        raise KeyError(f"unknown prompt '{key}'")
    return prompt


def capabilities() -> list[PromptTemplate]:
    return list(BUILTIN_PROMPTS.values())
