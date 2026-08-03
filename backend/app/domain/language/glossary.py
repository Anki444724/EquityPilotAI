"""Financial terminology dictionary.

A general-purpose translator will render "current ratio" as a phrase about
present-day proportions and "bull case" as something involving cattle. Financial
English is a technical register, and the terms in it have settled equivalents in
Indian financial journalism that a translator has to be told about rather than
left to infer.

Two rules govern the tables below.

**Hindi keeps the Devanagari term where one is genuinely current.** राजस्व for
revenue and शुद्ध लाभ for net profit are ordinary Hindi business vocabulary. But
much modern financial Hindi transliterates rather than translates — EBITDA is
ईबीआईटीडीए, not a coined Sanskrit compound — and forcing a native equivalent
produces prose no Hindi speaker would recognise.

**Hinglish keeps the English term.** This is not laziness; it is how the
register works. An Indian analyst says "operating margin achhi hai", never
"parichalan labh maargin achhi hai". A Hinglish table that translated technical
vocabulary would defeat the point of Hinglish.

The glossary is *advisory* to the model, not a find-and-replace. Substituting
strings mechanically produces ungrammatical output because Hindi inflects for
case and gender: राजस्व as a subject and राजस्व के as a possessive differ, and a
blind replacement gets it wrong every time. The terms are handed to the model as
a reference table and it inflects them correctly in context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.language.types import Language


@dataclass(frozen=True, slots=True)
class GlossaryTerm:
    """One financial term and its renderings."""

    english: str
    hindi: str
    #: Hinglish rendering. Almost always the English term unchanged — see the
    #: module docstring.
    hinglish: str
    #: Common abbreviation, kept identical in every language. ROE is ROE.
    abbreviation: str | None = None

    def render(self, language: Language) -> str:
        if language is Language.HINDI:
            return self.hindi
        if language is Language.HINGLISH:
            return self.hinglish
        return self.english


def _t(english: str, hindi: str, hinglish: str | None = None,
       abbreviation: str | None = None) -> GlossaryTerm:
    return GlossaryTerm(
        english=english, hindi=hindi,
        hinglish=hinglish if hinglish is not None else english,
        abbreviation=abbreviation,
    )


#: The core dictionary. Every term the brief names is present, plus the
#: vocabulary the ten scoring modules actually emit — a glossary that covers
#: the examples but not the output is decorative.
TERMS: tuple[GlossaryTerm, ...] = (
    # --- the brief's list, verbatim ---
    _t("Revenue", "राजस्व"),
    _t("Net Profit", "शुद्ध लाभ"),
    _t("Operating Margin", "ऑपरेटिंग मार्जिन"),
    _t("Debt", "ऋण"),
    _t("Cash Flow", "कैश फ्लो"),
    _t("Free Cash Flow", "फ्री कैश फ्लो", abbreviation="FCF"),
    _t("ROE", "आरओई", abbreviation="ROE"),
    _t("ROCE", "आरओसीई", abbreviation="ROCE"),
    _t("EPS", "ईपीएस", abbreviation="EPS"),
    _t("PE Ratio", "पीई अनुपात", abbreviation="PE"),
    _t("Market Cap", "मार्केट कैप"),
    _t("Dividend", "लाभांश"),

    # --- income statement ---
    _t("Total Revenue", "कुल राजस्व"),
    _t("Gross Profit", "सकल लाभ"),
    _t("Gross Margin", "सकल मार्जिन"),
    _t("EBITDA", "ईबीआईटीडीए", abbreviation="EBITDA"),
    _t("EBIT", "ईबीआईटी", abbreviation="EBIT"),
    _t("Operating Profit", "परिचालन लाभ"),
    _t("Profit After Tax", "कर पश्चात लाभ", abbreviation="PAT"),
    _t("Profit Before Tax", "कर पूर्व लाभ", abbreviation="PBT"),
    _t("Net Margin", "शुद्ध मार्जिन"),
    _t("Earnings", "आय"),
    _t("Earnings Per Share", "प्रति शेयर आय", abbreviation="EPS"),
    _t("Tax", "कर"),
    _t("Depreciation", "मूल्यह्रास"),
    _t("Finance Costs", "वित्त लागत"),
    _t("Interest", "ब्याज"),

    # --- balance sheet ---
    _t("Balance Sheet", "बैलेंस शीट"),
    _t("Assets", "परिसंपत्तियाँ"),
    _t("Liabilities", "देनदारियाँ"),
    _t("Equity", "इक्विटी"),
    _t("Shareholders' Equity", "शेयरधारक इक्विटी"),
    _t("Reserves", "आरक्षित निधि"),
    _t("Borrowings", "उधारी"),
    _t("Net Debt", "शुद्ध ऋण"),
    _t("Cash", "नकद"),
    _t("Cash Position", "नकदी स्थिति"),
    _t("Working Capital", "कार्यशील पूंजी"),
    _t("Inventory", "इन्वेंटरी"),
    _t("Receivables", "प्राप्य राशि"),
    _t("Payables", "देय राशि"),
    _t("Capital Employed", "नियोजित पूंजी"),

    # --- cash flow ---
    _t("Operating Cash Flow", "परिचालन कैश फ्लो", abbreviation="CFO"),
    _t("Capital Expenditure", "पूंजीगत व्यय", abbreviation="Capex"),
    _t("Capex", "कैपेक्स", abbreviation="Capex"),

    # --- ratios and valuation ---
    _t("Ratio", "अनुपात"),
    _t("Price to Book", "पीबी अनुपात", abbreviation="PB"),
    _t("EV/EBITDA", "ईवी/ईबीआईटीडीए", abbreviation="EV/EBITDA"),
    _t("Enterprise Value", "एंटरप्राइज़ वैल्यू", abbreviation="EV"),
    _t("Valuation", "मूल्यांकन"),
    _t("Intrinsic Value", "आंतरिक मूल्य"),
    _t("Margin of Safety", "सुरक्षा मार्जिन"),
    _t("Discounted Cash Flow", "डिस्काउंटेड कैश फ्लो", abbreviation="DCF"),
    _t("Fair Value", "उचित मूल्य"),
    _t("Upside", "अपसाइड"),
    _t("Premium", "प्रीमियम"),
    _t("Discount", "छूट"),
    _t("Book Value", "बही मूल्य"),

    # --- growth and returns ---
    _t("Growth", "वृद्धि"),
    _t("Revenue Growth", "राजस्व वृद्धि"),
    _t("CAGR", "सीएजीआर", abbreviation="CAGR"),
    _t("Return on Equity", "इक्विटी पर प्रतिफल", abbreviation="ROE"),
    _t("Return on Capital Employed", "नियोजित पूंजी पर प्रतिफल",
       abbreviation="ROCE"),
    _t("Yield", "प्रतिफल"),
    _t("Payout Ratio", "भुगतान अनुपात"),

    # --- risk and quality ---
    _t("Risk", "जोखिम"),
    _t("Volatility", "अस्थिरता"),
    _t("Leverage", "उत्तोलन"),
    _t("Interest Coverage", "ब्याज कवरेज"),
    _t("Liquidity", "तरलता"),
    _t("Solvency", "शोधन क्षमता"),
    _t("Governance", "अभिशासन"),
    _t("Corporate Governance", "कॉर्पोरेट गवर्नेंस"),
    _t("Promoter", "प्रवर्तक"),
    _t("Promoter Holding", "प्रवर्तक हिस्सेदारी"),
    _t("Pledge", "गिरवी"),
    _t("Customer Concentration", "ग्राहक संकेंद्रण"),
    _t("Regulatory Risk", "नियामक जोखिम"),

    # --- business quality ---
    _t("Moat", "प्रतिस्पर्धात्मक खाई"),
    _t("Competitive Advantage", "प्रतिस्पर्धात्मक लाभ"),
    _t("Pricing Power", "मूल्य निर्धारण शक्ति"),
    _t("Brand", "ब्रांड"),
    _t("Scalability", "मापनीयता"),
    _t("Market Share", "बाज़ार हिस्सेदारी"),
    _t("Capital Allocation", "पूंजी आवंटन"),
    _t("Capital Efficiency", "पूंजी दक्षता"),
    _t("Business Quality", "व्यवसाय गुणवत्ता"),

    # --- market and instruments ---
    _t("Share", "शेयर"),
    _t("Shares", "शेयर"),
    _t("Stock", "स्टॉक"),
    _t("Shareholder", "शेयरधारक"),
    _t("Investor", "निवेशक"),
    _t("Investment", "निवेश"),
    _t("Portfolio", "पोर्टफोलियो"),
    _t("Sector", "क्षेत्र"),
    _t("Industry", "उद्योग"),
    _t("Peer", "समकक्ष"),
    _t("Benchmark", "बेंचमार्क"),
    _t("Index", "सूचकांक"),
    _t("Large Cap", "लार्ज कैप"),
    _t("Mid Cap", "मिड कैप"),
    _t("Small Cap", "स्मॉल कैप"),

    # --- recommendation vocabulary ---
    _t("Buy", "खरीदें"),
    _t("Sell", "बेचें"),
    _t("Hold", "होल्ड"),
    _t("Strong Buy", "मज़बूत खरीद"),
    _t("Reduce", "कम करें"),
    _t("Avoid", "बचें"),
    _t("Recommendation", "अनुशंसा"),
    _t("Rating", "रेटिंग"),
    _t("Score", "स्कोर"),
    _t("Outlook", "दृष्टिकोण"),
    _t("Guidance", "मार्गदर्शन"),
    _t("Management", "प्रबंधन"),
    _t("Annual Report", "वार्षिक रिपोर्ट"),
    _t("Quarterly Results", "तिमाही परिणाम"),
    _t("Conference Call", "कॉन्फ्रेंस कॉल"),
    _t("Investor Presentation", "निवेशक प्रस्तुति"),

    # --- units, where Indian usage differs ---
    _t("Crore", "करोड़"),
    _t("Lakh", "लाख"),
    _t("Basis Points", "आधार अंक", abbreviation="bps"),
)

#: Lookup by lowercased English term.
BY_ENGLISH: dict[str, GlossaryTerm] = {t.english.lower(): t for t in TERMS}

# A duplicate English key would mean one rendering silently shadowing another.
assert len(BY_ENGLISH) == len(TERMS), "duplicate English term in the glossary"


def lookup(english: str) -> GlossaryTerm | None:
    return BY_ENGLISH.get((english or "").strip().lower())


def render_for_prompt(language: Language, *, text: str | None = None,
                      limit: int = 60) -> str:
    """Render the glossary as a reference table for the writing model.

    When ``text`` is supplied only the terms that actually occur in it are
    included. That matters for cost and for accuracy: handing a model 120
    irrelevant term pairs wastes context and invites it to shoehorn them in.

    Returns an empty string for English and for any language with no distinct
    renderings, so the caller can append it unconditionally.
    """
    if language is Language.ENGLISH:
        return ""

    if language is Language.HINGLISH:
        # Hinglish keeps English vocabulary, so a term table would be a list
        # of identities. The instruction that matters is stated in the
        # LanguageSpec, not here.
        return ""

    candidates = TERMS
    if text:
        lowered = text.lower()
        candidates = tuple(t for t in TERMS if t.english.lower() in lowered)
        if not candidates:
            return ""

    rows = []
    for term in candidates[:limit]:
        rendered = term.render(language)
        if rendered.lower() == term.english.lower():
            continue
        rows.append(f"{term.english} → {rendered}")

    if not rows:
        return ""

    return (
        "FINANCIAL GLOSSARY — use these exact renderings for technical terms, "
        "inflecting them naturally for grammatical case:\n"
        + "\n".join(rows)
    )


def coverage() -> dict[str, Any]:
    """Glossary statistics, for the validation report and the API."""
    return {
        "terms": len(TERMS),
        "hindi_translated": sum(
            1 for t in TERMS if t.hindi.lower() != t.english.lower()
        ),
        "abbreviations_preserved": sum(1 for t in TERMS if t.abbreviation),
        "hinglish_keeps_english": sum(
            1 for t in TERMS if t.hinglish.lower() == t.english.lower()
        ),
    }
