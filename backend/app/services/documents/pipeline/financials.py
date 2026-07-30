"""Financial and structured-field extraction — the AI-2 store, populated.

This is where a document stops being text and becomes data the rest of the
platform can use. The target is the workbook's 73-field extraction store, and
coverage is reported against that denominator so "we extracted a lot" is
replaced by "41 of 73, and here are the 32 we did not find".

Two extraction paths, deliberately kept apart:

* **Tables** — the reliable one. A financial statement table has a label column
  and period columns; matching the label and reading the period gives a value
  whose unit is declared by the table itself.
* **Prose** — the fallback. Regex cues over sentences, used for fields that
  never appear in a table (a covenant, a strategy priority, an auditor's name).

Every fact records its page, its section, the verbatim span it came from, and a
confidence. A fact without a page is not stored, because it could not then be
cited, and an uncitable number is exactly what Module 6 was built to refuse.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.documents.fields import (
    FIELD_COUNT, FIELDS_BY_KEY, FieldCategory, FieldSpec,
)
from app.domain.documents.types import (
    DetectedSection, ExtractedFact, ExtractedTable, ParsedDocument, SectionKind,
    Unit, normalise_whitespace,
)
from app.services.documents.extractors.tables import detect_unit, parse_number
from app.services.documents.pipeline.sections import section_for_order, section_for_page

# ---------------------------------------------------------------------------
# Period detection
# ---------------------------------------------------------------------------
#: Ordered most-specific first. Ranges must be tried before bare years, or
#: "FY 2024-25" matches the leading "2024" and resolves to FY24 — a silent
#: off-by-one that assigns every figure to the wrong year. The range patterns
#: therefore require the trailing half, and only then does a bare year apply.
_FY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # FY 2024-25 · FY2024/25 · 2024-2025 · 2024-25  → closing half
    (re.compile(r"\b(?:FY|F\.Y\.?)?\s*20(\d{2})\s*[-–—/]\s*(\d{2})(?!\d)", re.I), "close2"),
    (re.compile(r"\b(?:FY|F\.Y\.?)?\s*20(\d{2})\s*[-–—/]\s*20(\d{2})\b", re.I), "close2"),
    # FY24-25 → closing half
    (re.compile(r"\bFY\s*(\d{2})\s*[-–—/]\s*(\d{2})(?!\d)", re.I), "close2"),
    # year ended 31 March 2025 · as at 31 March 2025
    (re.compile(r"\b(?:year|period)\s+ended\D{0,20}(20\d{2})\b", re.I), "year4"),
    (re.compile(r"\bas\s+at\D{0,20}(20\d{2})\b", re.I), "year4"),
    # FY2025 · FY 2025
    (re.compile(r"\bFY\s*(20\d{2})\b", re.I), "year4"),
    # FY25 · FY 25
    (re.compile(r"\bFY\s*(\d{2})(?!\d)", re.I), "year2"),
    # A bare four-digit year, last resort.
    (re.compile(r"\b(20\d{2})\b"), "year4"),
)
_QUARTER = re.compile(r"\bQ([1-4])\s*(?:FY|F\.Y\.?)?\s*'?(\d{2,4})\b", re.I)


def detect_period(text: str) -> str | None:
    """Normalise a column header into a period label such as ``FY25`` or ``Q3FY25``.

    Indian filings write the same year six ways — FY25, FY 2024-25, 2024-25,
    year ended 31 March 2025. They must all reduce to one key or the same fact
    is stored several times under different periods, and a year-on-year
    comparison silently compares a figure with itself.

    A range always resolves to its **closing** year, which is the Indian fiscal
    convention and the one the rest of the platform already uses.
    """
    if not text:
        return None
    cleaned = normalise_whitespace(text)

    quarter = _QUARTER.search(cleaned)
    if quarter:
        year = quarter.group(2)
        return f"Q{quarter.group(1)}FY{year[-2:]}"

    for pattern, mode in _FY_PATTERNS:
        match = pattern.search(cleaned)
        if not match:
            continue
        if mode == "close2":
            return f"FY{match.group(2)[-2:]}"
        return f"FY{match.group(1)[-2:]}"
    return None


def fiscal_year_of(period: str | None) -> int | None:
    """``FY25`` → 2025, ``Q4FY25`` → 2025. ``None`` when unparseable.

    A quarter resolves to its fiscal year rather than to nothing: a Q4 FY25
    transcript is evidence about FY2025 and should be filed under it. Returning
    ``None`` left every conference call unattached to a year, which made them
    invisible to any year-filtered query.
    """
    if not period:
        return None
    match = re.fullmatch(r"(?:Q[1-4])?FY(\d{2})", period)
    if not match:
        return None
    return 2000 + int(match.group(1))


def quarter_of(period: str | None) -> int | None:
    """``Q3FY25`` → 3. ``None`` for an annual period."""
    if not period:
        return None
    match = re.fullmatch(r"Q([1-4])FY\d{2}", period)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Label matching
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LabelRule:
    """Row-label synonyms for one extraction field."""

    field_key: str
    #: Synonyms, matched against a normalised label. Longest wins.
    synonyms: tuple[str, ...]
    #: Labels that must not appear — the guard against near-miss rows.
    exclude: tuple[str, ...] = ()


#: Table-row synonyms. Only fields that genuinely appear in tables are listed;
#: the rest are prose-only and handled below.
LABEL_RULES: tuple[LabelRule, ...] = (
    LabelRule("revenue",
              ("revenue from operations", "total revenue", "net revenue",
               "revenue from contracts with customers", "net sales",
               "total income from operations", "turnover", "revenue"),
              exclude=("other", "segment", "deferred", "per share")),
    LabelRule("ebitda",
              ("ebitda", "operating profit before depreciation",
               "earnings before interest tax depreciation"),
              exclude=("margin", "%", "per share")),
    LabelRule("pat",
              ("profit after tax", "net profit for the year", "profit for the year",
               "net profit", "pat", "profit attributable to owners"),
              exclude=("margin", "before", "per share", "%")),
    LabelRule("eps",
              ("basic earnings per share", "earnings per share", "eps",
               "basic eps"),
              exclude=("diluted",)),
    LabelRule("gross_debt",
              ("total borrowings", "gross debt", "total debt",
               "borrowings total"),
              exclude=("net", "cost of", "short", "long")),
    LabelRule("cash_and_equivalents",
              ("cash and cash equivalents", "cash and bank balances",
               "cash & cash equivalents", "cash and bank"),
              exclude=("flow", "used", "generated")),
    LabelRule("net_worth",
              ("total equity", "net worth", "shareholders funds",
               "total shareholders equity", "shareholders equity"),
              exclude=("share capital only",)),
    LabelRule("operating_cash_flow",
              ("net cash from operating activities",
               "cash generated from operations", "operating cash flow",
               "net cash generated from operating activities"),
              exclude=("investing", "financing")),
    LabelRule("capex",
              ("purchase of property plant and equipment", "capital expenditure",
               "capex", "additions to fixed assets",
               "purchase of fixed assets"),
              exclude=("sale of", "proceeds")),
    LabelRule("free_cash_flow", ("free cash flow", "fcf"), exclude=("yield",)),
    LabelRule("total_borrowings",
              ("total borrowings", "borrowings"),
              exclude=("net", "cost", "current maturities")),
    LabelRule("blended_cost_of_debt",
              ("cost of debt", "average cost of borrowing",
               "weighted average cost of debt", "blended cost of debt")),
    LabelRule("average_maturity",
              ("average maturity", "weighted average maturity",
               "average tenure of debt")),
    LabelRule("order_book_backlog",
              ("order book", "order backlog", "outstanding order book",
               "unexecuted order book"),
              exclude=("inflow", "intake")),
    LabelRule("order_inflow_during_year",
              ("order inflow", "order intake", "new orders", "order booking")),
    LabelRule("book_to_bill_ratio", ("book to bill", "book-to-bill")),
    LabelRule("installed_capacity",
              ("installed capacity", "total capacity", "rated capacity"),
              exclude=("utilisation", "utilization")),
    LabelRule("capacity_utilisation",
              ("capacity utilisation", "capacity utilization",
               "utilisation rate", "utilization rate")),
    LabelRule("number_of_plants",
              ("number of plants", "manufacturing facilities",
               "number of manufacturing units", "number of factories")),
    LabelRule("planned_capacity_addition",
              ("planned capacity addition", "capacity addition",
               "capacity expansion")),
    LabelRule("top_5_customer_concentration",
              ("top 5 customers", "top five customers",
               "customer concentration", "top 5 client contribution",
               "top 10 customers")),
    LabelRule("number_of_subsidiaries",
              ("number of subsidiaries", "total subsidiaries")),
    LabelRule("employee_headcount",
              ("total employees", "employee headcount", "number of employees",
               "headcount", "total workforce"),
              exclude=("cost", "benefit", "expense")),
    LabelRule("randd_spend",
              ("research and development expenditure", "r&d spend",
               "research and development expense", "r&d expenditure")),
    LabelRule("advertising_brand_spend",
              ("advertising and sales promotion", "advertisement expenses",
               "brand spend", "marketing spend", "advertising expenses")),
    LabelRule("revenue_per_employee", ("revenue per employee",)),
    LabelRule("board_size", ("board size", "number of directors",
                             "total directors", "strength of the board")),
    LabelRule("independent_director_share",
              ("independent directors", "independent director",
               "% independent directors")),
    LabelRule("promoter_pledge",
              ("shares pledged", "promoter pledge", "pledged shares",
               "encumbered shares")),
    LabelRule("csr_spend", ("csr expenditure", "csr spend",
                            "corporate social responsibility expenditure")),
    LabelRule("scope_1_plus_2_emissions",
              ("scope 1 and scope 2", "scope 1 + scope 2", "total emissions",
               "ghg emissions", "scope 1 and 2 emissions")),
    LabelRule("renewable_energy_share",
              ("renewable energy", "% renewable", "renewable share",
               "share of renewable energy")),
    LabelRule("water_waste_intensity",
              ("water intensity", "waste intensity", "specific water consumption")),
    LabelRule("market_share", ("market share",)),
    LabelRule("kmp_remuneration",
              ("managerial remuneration", "kmp remuneration",
               "remuneration to key managerial personnel")),
    LabelRule("subsidiary_revenue_contribution",
              ("subsidiary revenue", "revenue from subsidiaries")),
    LabelRule("expected_asset_turn", ("asset turn", "asset turnover"),
              exclude=("fixed asset turnover ratio history",)),
)

_LABEL_INDEX: dict[str, list[LabelRule]] = {}
for _rule in LABEL_RULES:
    _LABEL_INDEX.setdefault(_rule.field_key, []).append(_rule)

_PUNCT = re.compile(r"[^\w%&\s]")


def _normalise_label(text: str) -> str:
    """Lower, strip punctuation and note markers so labels compare cleanly."""
    cleaned = normalise_whitespace(_PUNCT.sub(" ", text.lower()))
    # Drop a trailing note reference: "Revenue from operations 21" → "...operations".
    cleaned = re.sub(r"\s+\d{1,3}$", "", cleaned)
    return normalise_whitespace(cleaned)


def match_label(label: str) -> tuple[FieldSpec, float] | None:
    """Match a table row label to a field, with a match confidence.

    Exact synonym match scores highest; a containment match scores lower and in
    proportion to how much of the label the synonym accounts for, so a synonym
    buried in a long unrelated label does not win.
    """
    normalised = _normalise_label(label)
    if not normalised or len(normalised) < 3:
        return None

    best: tuple[FieldSpec, float] | None = None
    for rule in LABEL_RULES:
        spec = FIELDS_BY_KEY.get(rule.field_key)
        if spec is None:
            continue
        if any(term in normalised for term in rule.exclude):
            continue
        for synonym in sorted(rule.synonyms, key=len, reverse=True):
            if normalised == synonym:
                score = 0.95
            elif normalised.startswith(synonym) or normalised.endswith(synonym):
                score = 0.85 * (len(synonym) / len(normalised)) ** 0.35
            elif synonym in normalised:
                score = 0.75 * (len(synonym) / len(normalised)) ** 0.5
            else:
                continue
            score = round(min(0.95, score), 4)
            if best is None or score > best[1]:
                best = (spec, score)
            break
    if best is None or best[1] < 0.45:
        return None
    return best


# ---------------------------------------------------------------------------
# Prose rules for fields that never appear in a table
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProseRule:
    field_key: str
    pattern: re.Pattern[str]
    #: Sections in which the rule is trusted; empty means anywhere.
    sections: tuple[SectionKind, ...] = ()
    confidence: float = 0.6
    #: Numeric group, when the rule captures a value rather than a phrase.
    value_group: int | None = None
    unit_override: Unit | None = None


_NUM = r"([\d,]+(?:\.\d+)?)"

PROSE_RULES: tuple[ProseRule, ...] = (
    ProseRule("revenue_growth_guidance",
              re.compile(rf"(?:revenue|topline|top-line|sales)\s+growth\s+"
                         rf"(?:guidance\s+)?(?:of|at|around|to\s+be)?\s*"
                         rf"(?:about\s+)?{_NUM}\s*%", re.I),
              confidence=0.72, value_group=1, unit_override=Unit.PERCENT),
    ProseRule("ebitda_margin_guidance",
              re.compile(rf"ebitda\s+margins?\s+(?:guidance\s+)?"
                         rf"(?:of|at|around|to\s+be|in\s+the\s+range\s+of)?\s*"
                         rf"(?:about\s+)?{_NUM}\s*%", re.I),
              confidence=0.72, value_group=1, unit_override=Unit.PERCENT),
    ProseRule("capex_guidance",
              re.compile(rf"capex\s+(?:guidance\s+)?(?:of|at|around|will\s+be)?\s*"
                         rf"(?:₹|rs\.?|inr)?\s*{_NUM}\s*(?:cr|crore)", re.I),
              confidence=0.7, value_group=1, unit_override=Unit.INR_CRORE),
    ProseRule("announced_capex_programme",
              re.compile(rf"(?:announced|approved|committed)\s+(?:a\s+)?capital\s+"
                         rf"expenditure\s+(?:programme|program|plan)?\s*(?:of)?\s*"
                         rf"(?:₹|rs\.?|inr)?\s*{_NUM}\s*(?:cr|crore)", re.I),
              confidence=0.7, value_group=1, unit_override=Unit.INR_CRORE),
    ProseRule("credit_rating",
              re.compile(r"\b(?:rated|rating\s+of|assigned)\s+"
                         r"((?:CRISIL|ICRA|CARE|India\s+Ratings|IND|BWR|Brickwork)?\s*"
                         r"(?:AAA|AA\+|AA-|AA|A\+|A-|A1\+|A1|BBB\+|BBB-|BBB|BB\+|BB|B\+|B|C|D)"
                         r"(?:\s*/\s*\w+)?)", re.I),
              confidence=0.78),
    ProseRule("covenant_terms",
              re.compile(r"(covenants?\s+(?:require|stipulate|include|of)[^.]{15,220}\.)",
                         re.I),
              confidence=0.6),
    ProseRule("auditor_qualification",
              re.compile(r"((?:qualified\s+opinion|adverse\s+opinion|disclaimer\s+of\s+opinion|"
                         r"emphasis\s+of\s+matter|except\s+for\s+the\s+(?:possible\s+)?effects?)"
                         r"[^.]{0,220}\.)", re.I),
              sections=(SectionKind.AUDITOR_REPORT, SectionKind.NOTES_TO_ACCOUNTS,
                        SectionKind.UNKNOWN),
              confidence=0.75),
    ProseRule("related_party_transactions",
              re.compile(r"(related\s+part(?:y|ies)\s+transactions?[^.]{15,220}\.)", re.I),
              confidence=0.62),
    ProseRule("litigation_contingent_liability",
              re.compile(r"((?:contingent\s+liabilit(?:y|ies)|pending\s+litigation|"
                         r"claims?\s+against\s+the\s+company)[^.]{15,220}\.)", re.I),
              confidence=0.65),
    ProseRule("chairman_ceo_separation",
              re.compile(r"((?:the\s+)?(?:roles?|positions?)\s+of\s+(?:the\s+)?chairman\s+"
                         r"and\s+(?:the\s+)?(?:managing\s+director|chief\s+executive|ceo)"
                         r"[^.]{0,180}\.)", re.I),
              confidence=0.7),
    ProseRule("auditor_name_and_tenure",
              re.compile(r"((?:M/s\.?\s*)?[A-Z][\w&.'-]*(?:\s+[A-Z&][\w&.'-]*){0,4}\s*"
                         r"(?:&\s*Co\.?|&\s*Associates|LLP|Chartered\s+Accountants)"
                         r"[^.]{0,120}(?:statutory\s+auditors?|auditors?)[^.]{0,80}\.)", re.I),
              confidence=0.6),
    ProseRule("stated_competitive_advantage",
              re.compile(r"((?:our\s+(?:competitive\s+advantage|key\s+strengths?|moat)|"
                         r"we\s+believe\s+our\s+(?:strength|advantage)|"
                         r"competitive\s+strengths?\s+include)[^.]{15,240}\.)", re.I),
              confidence=0.62),
    ProseRule("brand_ip_assets",
              re.compile(r"((?:trademarks?|patents?|registered\s+brands?|"
                         r"intellectual\s+property)[^.]{15,220}\.)", re.I),
              confidence=0.58),
    ProseRule("switching_cost_evidence",
              re.compile(r"((?:switching\s+costs?|long-?term\s+contracts?|"
                         r"customer\s+retention\s+rate|multi-?year\s+agreements?)"
                         r"[^.]{15,220}\.)", re.I),
              confidence=0.58),
    ProseRule("business_description",
              re.compile(r"((?:the\s+Company\s+is\s+(?:a|an|one\s+of)|"
                         r"we\s+are\s+(?:a|an|one\s+of)|is\s+engaged\s+in\s+the\s+business\s+of)"
                         r"[^.]{25,300}\.)", re.I),
              confidence=0.68),
    ProseRule("strategy_priorities",
              re.compile(r"((?:our\s+strategic\s+priorit|strategy\s+(?:rests|is\s+built)|"
                         r"strategic\s+pillars?|we\s+will\s+focus\s+on)[^.]{20,260}\.)", re.I),
              confidence=0.6),
    ProseRule("management_outlook_commentary",
              re.compile(r"((?:looking\s+ahead|going\s+forward|the\s+outlook\s+for|"
                         r"we\s+remain\s+(?:confident|optimistic|cautious))[^.]{20,280}\.)", re.I),
              confidence=0.6),
    ProseRule("industry_tailwind",
              re.compile(r"((?:industry\s+(?:tailwind|growth)|favourable\s+(?:demand|policy)|"
                         r"government\s+(?:scheme|incentive|pli)|structural\s+demand)"
                         r"[^.]{15,240}\.)", re.I),
              confidence=0.58),
    ProseRule("new_product_market_entry",
              re.compile(r"((?:launched|entered|commissioned|introduced)\s+"
                         r"(?:a\s+)?(?:new\s+)?(?:product|market|geography|plant|facility|category)"
                         r"[^.]{10,220}\.)", re.I),
              confidence=0.6),
    ProseRule("commissioning_timeline",
              re.compile(r"((?:expected\s+to\s+be\s+commissioned|commissioning\s+"
                         r"(?:is\s+)?(?:expected|scheduled)|will\s+be\s+operational)"
                         r"[^.]{10,200}\.)", re.I),
              confidence=0.62),
    ProseRule("execution_period",
              re.compile(rf"(?:execution\s+period|order\s+book\s+(?:to\s+be\s+)?executed"
                         rf"(?:\s+over)?)\s*(?:of|is|over)?\s*{_NUM}\s*(?:months|month)", re.I),
              confidence=0.62, value_group=1, unit_override=Unit.MONTHS),
    ProseRule("customer_contract_tenure",
              re.compile(rf"(?:contracts?\s+(?:are\s+)?(?:typically\s+)?(?:of|for|run\s+for))"
                         rf"\s*{_NUM}\s*(?:year|years)", re.I),
              confidence=0.6, value_group=1, unit_override=Unit.YEARS),
    ProseRule("loss_making_subsidiaries",
              re.compile(rf"{_NUM}\s+subsidiar(?:y|ies)\s+(?:reported|incurred|made)\s+"
                         rf"(?:a\s+)?loss", re.I),
              confidence=0.65, value_group=1, unit_override=Unit.COUNT),
    ProseRule("esg_brsr_rating",
              re.compile(r"((?:esg\s+(?:rating|score)|brsr\s+(?:core\s+)?(?:rating|score)|"
                         r"sustainalytics|msci\s+esg)[^.]{0,120}\.)", re.I),
              confidence=0.62),
    ProseRule("key_customer_names",
              re.compile(r"((?:key|major|principal|marquee)\s+(?:customers?|clients?)\s+"
                         r"(?:are|include|comprise)[^.]{10,220}\.)", re.I),
              confidence=0.62),
    ProseRule("segment_revenue_split",
              re.compile(r"((?:segment\s+revenue|revenue\s+(?:split|mix|contribution)\s+"
                         r"(?:by|across)\s+segments?)[^.]{10,240}\.)", re.I),
              confidence=0.6),
    ProseRule("geographic_revenue_split",
              re.compile(r"((?:(?:export|domestic|international|overseas)\s+revenue|"
                         r"revenue\s+(?:split|mix)\s+by\s+geograph)[^.]{10,240}\.)", re.I),
              confidence=0.6),
    ProseRule("product_brand_portfolio",
              re.compile(r"((?:our\s+(?:brand|product)\s+portfolio|brands?\s+such\s+as|"
                         r"portfolio\s+of\s+brands?)[^.]{10,240}\.)", re.I),
              confidence=0.6),
    ProseRule("operational_highlights",
              re.compile(r"((?:operational\s+highlights?|during\s+the\s+year\s+under\s+review|"
                         r"key\s+operational\s+achievements?)[^.]{20,260}\.)", re.I),
              confidence=0.58),
    ProseRule("management_discussion_summary",
              re.compile(r"((?:the\s+(?:year|financial\s+year)\s+(?:was|has\s+been)|"
                         r"performance\s+during\s+the\s+year)[^.]{25,300}\.)", re.I),
              sections=(SectionKind.MANAGEMENT_DISCUSSION,),
              confidence=0.6),

    # --- numeric fields commonly stated in prose ---------------------
    # These all have table rules above, but ESG and governance figures are
    # overwhelmingly written as sentences rather than tabulated. Without a
    # prose path they were simply invisible: ESG coverage sat at 1 of 5 on a
    # document that stated four of the five in plain English.
    ProseRule("scope_1_plus_2_emissions",
              re.compile(rf"scope\s*1\s*(?:and|\+|&)\s*(?:scope\s*)?2\s+emissions?\s+"
                         rf"(?:were|was|of|at|stood\s+at|totalled)?\s*{_NUM}\s*"
                         rf"(?:tco2e?|tonnes)", re.I),
              confidence=0.74, value_group=1, unit_override=Unit.TONNES_CO2),
    ProseRule("renewable_energy_share",
              re.compile(rf"renewable\s+(?:energy|power)\s+(?:accounted\s+for|was|"
                         rf"comprised|represented|share\s+(?:of|was))?\s*{_NUM}\s*%",
                         re.I),
              confidence=0.72, value_group=1, unit_override=Unit.PERCENT),
    ProseRule("csr_spend",
              re.compile(rf"csr\s+(?:expenditure|spend|spending)\s+"
                         rf"(?:for\s+the\s+year\s+)?(?:was|of|at|stood\s+at|totalled)?"
                         rf"\s*(?:₹|rs\.?|inr)?\s*{_NUM}\s*(?:cr|crore)", re.I),
              confidence=0.74, value_group=1, unit_override=Unit.INR_CRORE),
    ProseRule("water_waste_intensity",
              re.compile(r"((?:specific\s+water\s+consumption|water\s+intensity|"
                         r"waste\s+intensity)[^.]{5,160}\.)", re.I),
              confidence=0.6),
    ProseRule("board_size",
              re.compile(rf"(?:board\s+of\s+directors\s+comprises|board\s+comprises|"
                         rf"board\s+consists\s+of|there\s+are)\s+(?:of\s+)?{_NUM}\s+"
                         rf"(?:directors|members)", re.I),
              confidence=0.76, value_group=1, unit_override=Unit.COUNT),
    ProseRule("independent_director_share",
              re.compile(rf"(?:of\s+whom\s+|comprising\s+|including\s+){_NUM}\s+are\s+"
                         rf"independent\s+directors?", re.I),
              confidence=0.74, value_group=1, unit_override=Unit.COUNT),
    ProseRule("promoter_pledge",
              re.compile(rf"(?:shares?\s+pledged|promoter\s+pledge|pledged\s+shares?)"
                         rf"[^.]{{0,40}}?(?:stand\s+at|is|are|was|of|:)\s*{_NUM}\s*%",
                         re.I),
              confidence=0.74, value_group=1, unit_override=Unit.PERCENT),
    ProseRule("kmp_remuneration",
              re.compile(rf"(?:managerial\s+remuneration|kmp\s+remuneration|"
                         rf"remuneration\s+to\s+key\s+managerial\s+personnel)"
                         rf"[^.]{{0,40}}?(?:was|of|at|totalled)\s*(?:₹|rs\.?|inr)?\s*"
                         rf"{_NUM}\s*(?:cr|crore)", re.I),
              confidence=0.72, value_group=1, unit_override=Unit.INR_CRORE),
    ProseRule("number_of_subsidiaries",
              re.compile(rf"(?:has|had|there\s+(?:are|were))\s+{_NUM}\s+subsidiar(?:y|ies)",
                         re.I),
              confidence=0.72, value_group=1, unit_override=Unit.COUNT),
    ProseRule("number_of_plants",
              re.compile(rf"(?:operates|has|owns)\s+{_NUM}\s+"
                         rf"(?:manufacturing\s+)?(?:facilities|plants|units|factories)",
                         re.I),
              confidence=0.72, value_group=1, unit_override=Unit.COUNT),
    ProseRule("installed_capacity",
              re.compile(rf"installed\s+capacity\s+(?:stands?\s+at|is|was|of)?\s*"
                         rf"{_NUM}\s*(?:tonnes|tpa|mt|mw|units|kl)", re.I),
              confidence=0.72, value_group=1, unit_override=Unit.UNITS),
    ProseRule("capacity_utilisation",
              re.compile(rf"capacity\s+utili[sz]ation\s+(?:of|at|was|is|stood\s+at)?\s*"
                         rf"{_NUM}\s*%", re.I),
              confidence=0.74, value_group=1, unit_override=Unit.PERCENT),
    ProseRule("top_5_customer_concentration",
              re.compile(rf"top[- ](?:5|five|10|ten)\s+(?:customers?|clients?)\s+"
                         rf"(?:accounted\s+for|contributed|represent(?:ed)?|"
                         rf"comprised|:)?\s*{_NUM}\s*%", re.I),
              confidence=0.74, value_group=1, unit_override=Unit.PERCENT),
    ProseRule("market_share",
              re.compile(rf"market\s+share\s+(?:of|is|was|stood\s+at|at)\s*{_NUM}\s*%",
                         re.I),
              confidence=0.7, value_group=1, unit_override=Unit.PERCENT),
    ProseRule("order_book_backlog",
              re.compile(rf"order\s+book\s+(?:stands?\s+at|is|was|of|position\s+"
                         rf"(?:is|was))\s*(?:₹|rs\.?|inr)?\s*{_NUM}\s*(?:cr|crore)",
                         re.I),
              confidence=0.74, value_group=1, unit_override=Unit.INR_CRORE),
    ProseRule("book_to_bill_ratio",
              re.compile(rf"book[- ]to[- ]bill\s+(?:ratio\s+)?(?:is|was|of|at|stands?\s+at)?"
                         rf"\s*{_NUM}\s*x?", re.I),
              confidence=0.7, value_group=1, unit_override=Unit.TIMES),
    ProseRule("blended_cost_of_debt",
              re.compile(rf"(?:blended\s+)?cost\s+of\s+(?:debt|borrowing)\s+"
                         rf"(?:is|was|of|at|stands?\s+at)?\s*{_NUM}\s*%", re.I),
              confidence=0.74, value_group=1, unit_override=Unit.PERCENT),
    ProseRule("average_maturity",
              re.compile(rf"average\s+maturity\s+(?:is|was|of|at|stands?\s+at)?\s*"
                         rf"{_NUM}\s*(?:years?|yrs?)", re.I),
              confidence=0.72, value_group=1, unit_override=Unit.YEARS),
    ProseRule("total_borrowings",
              re.compile(rf"total\s+borrowings\s+(?:are|is|were|was|of|stood\s+at|at)?"
                         rf"\s*(?:₹|rs\.?|inr)?\s*{_NUM}\s*(?:cr|crore)", re.I),
              confidence=0.74, value_group=1, unit_override=Unit.INR_CRORE),
    ProseRule("employee_headcount",
              re.compile(rf"(?:total\s+(?:employees|workforce|headcount)|"
                         rf"employee\s+headcount|headcount)\s+"
                         rf"(?:of|is|was|stood\s+at|at)?\s*{_NUM}", re.I),
              confidence=0.68, value_group=1, unit_override=Unit.COUNT),
    ProseRule("capex_spent_to_date",
              re.compile(rf"capex\s+spent\s+to\s+date\s+(?:is|was|of|at)?\s*"
                         rf"(?:₹|rs\.?|inr)?\s*{_NUM}\s*(?:cr|crore)", re.I),
              confidence=0.72, value_group=1, unit_override=Unit.INR_CRORE),
    ProseRule("expected_asset_turn",
              re.compile(rf"(?:expected\s+)?asset\s+turn\s+(?:on\s+[^.]{{0,40}}?)?"
                         rf"(?:is|of|at|will\s+be)\s*{_NUM}\s*x", re.I),
              confidence=0.7, value_group=1, unit_override=Unit.TIMES),
    ProseRule("subsidiary_revenue_contribution",
              re.compile(rf"subsidiar(?:y|ies)\s+(?:contributed|accounted\s+for)\s*"
                         rf"{_NUM}\s*%", re.I),
              confidence=0.7, value_group=1, unit_override=Unit.PERCENT),
    ProseRule("planned_capacity_addition",
              re.compile(rf"(?:planned\s+)?capacity\s+(?:addition|expansion)\s+"
                         rf"(?:of|is|will\s+be)?\s*{_NUM}\s*(?:tonnes|tpa|mt|mw|units)",
                         re.I),
              confidence=0.7, value_group=1, unit_override=Unit.UNITS),
    ProseRule("geographic_revenue_split",
              re.compile(r"((?:exports?|domestic\s+sales?|international\s+revenue)\s+"
                         r"(?:contributed|accounted\s+for|were|was)[^.]{5,180}\.)", re.I),
              confidence=0.64),
)

#: Sentences that read as a principal risk, ranked and assigned to risk 1/2/3.
_RISK_SENTENCE = re.compile(
    r"((?:volatility|fluctuation|disruption|shortage|dependence|concentration|"
    r"exposure|competition|regulatory\s+change|cyber|climate|slowdown|adverse\s+"
    r"(?:movement|impact)|foreign\s+exchange\s+risk|credit\s+risk|liquidity\s+risk)"
    r"[^.]{20,240}\.)",
    re.I,
)
_RISK_FIELDS = ("principal_risk_1", "principal_risk_2", "principal_risk_3")

#: Opportunity sentences, assigned to opportunity 1/2.
_OPPORTUNITY_SENTENCE = re.compile(
    r"((?:opportunit(?:y|ies)|we\s+see\s+(?:significant\s+)?(?:scope|headroom|potential)|"
    r"addressable\s+market|expansion\s+(?:into|opportunity)|penetration\s+"
    r"(?:remains|is)\s+low)[^.]{20,240}\.)",
    re.I,
)
_OPPORTUNITY_FIELDS = ("growth_opportunity_1", "growth_opportunity_2")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")


# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ExtractionResult:
    """Facts plus the coverage arithmetic the workbook's Section 3 reports."""

    facts: list[ExtractedFact]
    #: field key → best confidence, for the coverage panel.
    covered: dict[str, float]

    @property
    def coverage(self) -> float:
        return round(len(self.covered) / FIELD_COUNT, 4) if FIELD_COUNT else 0.0

    @property
    def average_confidence(self) -> float:
        if not self.covered:
            return 0.0
        return round(sum(self.covered.values()) / len(self.covered), 4)

    def missing(self) -> list[str]:
        """Fields the platform looked for and did not find — reported, not hidden."""
        return sorted(set(FIELDS_BY_KEY) - set(self.covered))

    def by_category(self) -> dict[FieldCategory, dict[str, float]]:
        """Per-category coverage, matching the workbook's Section 3 grid."""
        out: dict[FieldCategory, dict[str, float]] = {}
        for category in FieldCategory:
            specs = [s for s in FIELDS_BY_KEY.values() if s.category is category]
            found = [s.key for s in specs if s.key in self.covered]
            confidences = [self.covered[k] for k in found]
            out[category] = {
                "defined": float(len(specs)),
                "extracted": float(len(found)),
                "coverage": round(len(found) / len(specs), 4) if specs else 0.0,
                "avg_confidence": round(sum(confidences) / len(confidences), 4)
                if confidences else 0.0,
            }
        return out


class FinancialExtractor:
    """Populates the 73-field store from tables and prose."""

    #: Facts below this are discarded rather than shown as findings.
    MIN_CONFIDENCE = 0.4
    #: Prose scanning is capped so a 400-page report stays inside latency budget.
    MAX_PROSE_CHARS = 1_200_000

    def extract(
        self,
        document: ParsedDocument,
        sections: list[DetectedSection] | None = None,
    ) -> ExtractionResult:
        sections = sections or []
        facts: list[ExtractedFact] = []
        for page in document.pages:
            section = section_for_page(sections, page.number)
            kind = section.kind if section else SectionKind.UNKNOWN
            for table in page.tables:
                facts.extend(self._from_table(table, page.number, kind))
        facts.extend(self._from_prose(document, sections))

        best = self._deduplicate(facts)
        covered: dict[str, float] = {}
        for fact in best:
            covered[fact.field_key] = max(covered.get(fact.field_key, 0.0), fact.confidence)
        return ExtractionResult(facts=best, covered=covered)

    # ---------------------------------------------------------- tables
    def _from_table(
        self, table: ExtractedTable, page: int, section: SectionKind
    ) -> list[ExtractedFact]:
        grid = table.to_grid()
        if not grid:
            return []

        periods = self._column_periods(table)
        out: list[ExtractedFact] = []
        for row in grid:
            if not row or not row[0].strip():
                continue
            matched = match_label(row[0])
            if matched is None:
                continue
            spec, label_score = matched
            for column in range(1, len(row)):
                parsed = parse_number(row[column])
                if parsed is None:
                    continue
                value, inline_unit = parsed
                unit = self._resolve_unit(spec, inline_unit, table)
                period = periods.get(column)
                confidence = self._table_confidence(
                    label_score, table.confidence, unit, period
                )
                if confidence < self.MIN_CONFIDENCE:
                    continue
                out.append(
                    ExtractedFact(
                        category=spec.category.value,
                        field_key=spec.key,
                        label=spec.label,
                        value=value,
                        unit=unit,
                        period=period,
                        page=page,
                        section=section,
                        confidence=confidence,
                        evidence=self._row_evidence(table, row, column),
                    )
                )
        return out

    @staticmethod
    def _column_periods(table: ExtractedTable) -> dict[int, str]:
        """Map column index → period, from the header row."""
        periods: dict[int, str] = {}
        for index, cell in enumerate(table.header):
            period = detect_period(cell)
            if period:
                periods[index] = period
        return periods

    #: Fields whose unit is a property of the field itself, not of the table
    #: it sits in. A headcount printed inside a "₹ in crore" statement is still
    #: a headcount; inheriting the table's unit would let `to_crore()` convert
    #: 19,220 employees into ₹19,220 crore. Non-monetary by definition.
    _UNIT_IMMUNE: frozenset[Unit] = frozenset({
        Unit.COUNT, Unit.YEARS, Unit.MONTHS, Unit.TIMES, Unit.BOOLEAN,
        Unit.TEXT, Unit.SCORE, Unit.INDEX, Unit.UNITS, Unit.TONNES_CO2,
    })

    @classmethod
    def _resolve_unit(cls, spec: FieldSpec, inline: Unit, table: ExtractedTable) -> Unit:
        """Precedence: the cell's own suffix, then the table, then the field spec.

        The cell wins because a "%" printed beside a number overrides a table
        declared in ₹ crore — margin columns sit inside financial tables all the
        time. The field spec is last: it is what the number *should* be, which
        is a weaker claim than what the document says it is.

        The exception is a field whose declared unit is inherently non-monetary.
        There, the spec outranks the table, because no table caption can make a
        count of employees into an amount of money.
        """
        if inline is not Unit.UNKNOWN:
            return inline
        if spec.unit in cls._UNIT_IMMUNE:
            return spec.unit
        if table.unit is not Unit.UNKNOWN:
            return table.unit
        return spec.unit

    @staticmethod
    def _table_confidence(
        label_score: float, table_score: float, unit: Unit, period: str | None
    ) -> float:
        """Combine label, table and metadata confidence.

        The label match dominates because misreading the row is the failure that
        produces a wrong number under a right name. An unknown unit and a
        missing period each cost, because both make the fact harder to use
        safely downstream.
        """
        score = 0.6 * label_score + 0.25 * table_score
        score += 0.1 if unit is not Unit.UNKNOWN else 0.0
        score += 0.05 if period else 0.0
        return round(min(0.95, score), 4)

    @staticmethod
    def _row_evidence(table: ExtractedTable, row: list[str], column: int) -> str:
        header = table.header[column] if column < len(table.header) else ""
        return normalise_whitespace(
            f"{row[0]} | {header} | {row[column]}"
        )[:300]

    # ----------------------------------------------------------- prose
    def _from_prose(
        self, document: ParsedDocument, sections: list[DetectedSection]
    ) -> list[ExtractedFact]:
        out: list[ExtractedFact] = []
        budget = self.MAX_PROSE_CHARS
        order = 0
        for page in document.pages:
            text = page.text
            if not text.strip() or budget <= 0:
                order += len(page.blocks)
                continue
            budget -= len(text)
            # Attribute the page to the section holding most of its blocks;
            # prose rules scan whole pages, so a single label is unavoidable
            # here, but it is chosen by weight of content rather than by which
            # section happens to start first.
            section = self._dominant_section(sections, page, order)
            order += len(page.blocks)
            kind = section.kind if section else SectionKind.UNKNOWN

            for rule in PROSE_RULES:
                if rule.sections and kind not in rule.sections:
                    continue
                match = rule.pattern.search(text)
                if match is None:
                    continue
                fact = self._prose_fact(rule, match, page.number, kind)
                if fact is not None:
                    out.append(fact)

            out.extend(self._ranked(
                _RISK_SENTENCE, _RISK_FIELDS, text, page.number, kind, 0.6,
                boost_section=SectionKind.RISK_FACTORS,
            ))
            out.extend(self._ranked(
                _OPPORTUNITY_SENTENCE, _OPPORTUNITY_FIELDS, text, page.number, kind, 0.58,
                boost_section=SectionKind.MANAGEMENT_DISCUSSION,
            ))
        return out

    @staticmethod
    def _dominant_section(
        sections: list[DetectedSection], page, start_order: int
    ) -> DetectedSection | None:
        """The section covering the largest share of a page's blocks."""
        if not page.blocks:
            return section_for_page(sections, page.number)
        tally: dict[int, int] = {}
        found: dict[int, DetectedSection] = {}
        for offset in range(len(page.blocks)):
            section = section_for_order(sections, start_order + offset, page.number)
            if section is None:
                continue
            key = id(section)
            tally[key] = tally.get(key, 0) + 1
            found[key] = section
        if not tally:
            return None
        return found[max(tally, key=lambda k: tally[k])]

    def _prose_fact(
        self, rule: ProseRule, match: re.Match, page: int, section: SectionKind
    ) -> ExtractedFact | None:
        spec = FIELDS_BY_KEY.get(rule.field_key)
        if spec is None:
            return None
        evidence = normalise_whitespace(match.group(0))[:400]

        value: float | None = None
        unit = rule.unit_override or (Unit.TEXT if not rule.value_group else spec.unit)
        text: str | None = None
        if rule.value_group:
            parsed = parse_number(match.group(rule.value_group))
            if parsed is None:
                return None
            value = parsed[0]
        else:
            captured = match.group(1) if match.re.groups else match.group(0)
            text = normalise_whitespace(captured)[:600]
            unit = Unit.TEXT

        return ExtractedFact(
            category=spec.category.value,
            field_key=spec.key,
            label=spec.label,
            value=value,
            text=text,
            unit=unit,
            page=page,
            section=section,
            # Prose is inherently weaker evidence than a table, and is scored so.
            confidence=round(rule.confidence, 4),
            evidence=evidence,
        )

    def _ranked(
        self,
        pattern: re.Pattern[str],
        field_keys: tuple[str, ...],
        text: str,
        page: int,
        section: SectionKind,
        base: float,
        *,
        boost_section: SectionKind,
    ) -> list[ExtractedFact]:
        """Assign the strongest N sentences to ordered slots (risk 1, 2, 3…).

        Ordering is by sentence length as a crude proxy for specificity: a
        one-clause boilerplate risk carries less information than a qualified
        two-clause one. Crude, and labelled crude.
        """
        matches = [normalise_whitespace(m.group(1)) for m in pattern.finditer(text)]
        if not matches:
            return []
        # In the section that is *about* risks, a risk sentence is far more
        # likely to be a genuine principal risk than the same sentence elsewhere.
        confidence = base + (0.12 if section is boost_section else 0.0)
        unique: list[str] = []
        for sentence in matches:
            if sentence not in unique:
                unique.append(sentence)
        unique.sort(key=len, reverse=True)

        out: list[ExtractedFact] = []
        for index, key in enumerate(field_keys):
            if index >= len(unique):
                break
            spec = FIELDS_BY_KEY.get(key)
            if spec is None:
                continue
            out.append(
                ExtractedFact(
                    category=spec.category.value,
                    field_key=spec.key,
                    label=spec.label,
                    text=unique[index][:600],
                    unit=Unit.TEXT,
                    page=page,
                    section=section,
                    confidence=round(min(0.9, confidence - 0.03 * index), 4),
                    evidence=unique[index][:400],
                )
            )
        return out

    # ----------------------------------------------------------- merge
    @staticmethod
    def _deduplicate(facts: list[ExtractedFact]) -> list[ExtractedFact]:
        """One fact per (field, period). Highest confidence wins, then latest page.

        Keeping every occurrence would let a five-year summary table and the
        primary statement disagree silently. Keeping the most confident makes
        the disagreement resolvable and the winner inspectable.
        """
        best: dict[tuple[str, str | None], ExtractedFact] = {}
        for fact in facts:
            key = (fact.field_key, fact.period)
            current = best.get(key)
            if current is None or fact.confidence > current.confidence:
                best[key] = fact
        out = list(best.values())
        out.sort(key=lambda f: (f.category, f.field_key, f.period or "", -f.confidence))
        return out
