"""Synthetic but realistic filings for the Module 7 test suite.

Written to mirror the register and phrasing of an actual Indian annual report
closely enough that the cue-phrase extractors are genuinely exercised. The
figures are BHARATCP's reference-model numbers so extraction can be checked
against values the platform already knows.
"""
from __future__ import annotations

ANNUAL_REPORT_TEXT = """Bharat Consumer Products Limited
Annual Report 2024-25

Chairman's Letter

Dear Shareholders, the year under review has been one of disciplined execution.
Revenue from operations grew to Rs 33,543 crore and the Board is pleased with
the margin trajectory. We remain confident that the demand environment will
support continued growth into the coming year.

Business Overview

Bharat Consumer Products Limited is a leading manufacturer of packaged foods and
household products in India. The Company is engaged in the business of
manufacturing, marketing and distribution of branded consumer products across
India and select export markets. Our brand portfolio includes Suraj, Amrit Gold
and Nirmal Care. Business segments are Packaged Foods, Home Care and Personal
Care.

The Company operates 14 manufacturing facilities. Installed capacity stands at
980000 tonnes with capacity utilisation of 78%. We compete with Hindustan
Unilever Limited and Nestle India Limited in our core categories.

Bharat Nutrition Private Limited is a wholly-owned subsidiary of the Company.
Suraj Foods International Pte Ltd is a wholly-owned subsidiary engaged in export
distribution. Nirmal Personal Care Limited is a material subsidiary of the
Company. The Company has 7 subsidiaries as at 31 March 2025.

Exports contributed 11% of consolidated revenue during the year, with shipments
to Bangladesh, Sri Lanka, UAE and Singapore.

Management Discussion and Analysis

The financial year was one of volume-led growth. Performance during the year
reflected pricing discipline and a favourable input cost cycle. Operational
highlights include commissioning of the Nashik line and a 210 basis point
improvement in gross margin.

Our strategic priorities are premiumisation, distribution depth and cost
leadership. Looking ahead, we expect revenue growth of 12% and EBITDA margin
guidance of 17% for the coming year. Capex guidance of Rs 1,400 crore has been
approved for capacity expansion.

We see significant headroom in rural penetration, where our distribution reaches
only 42% of addressable outlets. Opportunity also exists in the health and
wellness category, which is growing at twice the rate of the broader market.

Government scheme support under the PLI framework provides an industry tailwind
for food processing capacity. We launched a new product in the functional
beverages category during the third quarter.

The Company has announced a capital expenditure programme of Rs 3,200 crore over
three years. Commissioning is expected in phases through FY27. Capex spent to
date is Rs 1,165 crore. Expected asset turn on the new capacity is 2.4x.

Risk Factors

Volatility in agricultural commodity prices remains the principal exposure for
the Company and can compress gross margin materially within a single quarter.
Dependence on a concentrated distributor network in the northern region creates
execution risk if any large distributor were to exit.
Competition from regional unbranded players continues to intensify in the value
segment and may constrain pricing in the mass portfolio.
Foreign exchange risk arises on imported palm oil and packaging resin.
Contingent liabilities in respect of disputed indirect tax matters amount to
Rs 214 crore as at the balance sheet date.
Related party transactions during the year were conducted at arm's length and
are disclosed in note 38 to the financial statements.

Corporate Governance

The Board of Directors comprises 10 directors, of whom 6 are Independent
Directors. Mr Arvind Deshmukh is the Chairman and Independent Director of the
Company. Ms Kavita Raman is the Managing Director. Mr Suresh Iyer is the Chief
Financial Officer of the Company. The roles of Chairman and Managing Director
are separate, consistent with the Company's governance policy.

The statutory auditors are M/s Bhattacharya & Associates, Chartered Accountants,
appointed for a term of five years. Promoters hold 51.2% of the paid-up equity
capital and no promoter shares are pledged. Shares pledged stand at 0%.

Managerial remuneration for the year was Rs 42 crore.

Environmental Social and Governance

Scope 1 and 2 emissions were 412000 tCO2e for the year. Renewable energy
accounted for 34% of total electricity consumption. Specific water consumption
declined 9% year on year. CSR expenditure for the year was Rs 71 crore.
The Company's BRSR Core rating was reaffirmed during the year.

Independent Auditor's Report

We have audited the accompanying financial statements of Bharat Consumer
Products Limited. In our opinion the financial statements give a true and fair
view. Emphasis of matter is drawn to note 41 regarding the ongoing tax
assessment for assessment year 2021-22, in respect of which no provision has
been recognised.

Financial Statements

Statement of Profit and Loss
"""

ANNUAL_REPORT_TABLE = [
    ["Particulars (Rs in crore)", "FY 2023-24", "FY 2024-25"],
    ["Revenue from operations", "28,914", "33,543"],
    ["EBITDA", "4,702", "5,490.70"],
    ["Profit after tax", "2,918", "3,450.90"],
    ["Basic earnings per share", "11.67", "13.80"],
    ["Total borrowings", "2,410", "2,180"],
    ["Cash and cash equivalents", "4,760", "5,341.30"],
    ["Total equity", "13,120", "15,606.90"],
    ["Net cash from operating activities", "2,980", "3,450.90"],
    ["Purchase of property plant and equipment", "1,040", "1,165"],
    ["Total employees", "18,400", "19,220"],
    ["Research and development expenditure", "168", "196"],
    ["Advertising and sales promotion", "2,180", "2,540"],
]

CONCALL_TEXT = """Bharat Consumer Products Limited
Q4 FY25 Earnings Conference Call Transcript
15 May 2025

Moderator: Good evening and welcome to the Q4 FY25 earnings call of Bharat
Consumer Products Limited.

Management Guidance

Kavita Raman, Managing Director: Thank you. Revenue for the quarter was Rs 8,940
crore. For the coming year we expect revenue growth of 12% to 14%. EBITDA margin
guidance of 17% remains unchanged. Capex guidance of Rs 1,400 crore is on track.

Question and Answer Session

Analyst: Could you comment on raw material inflation?

Suresh Iyer, Chief Financial Officer: Palm oil has been the principal pressure
point. Volatility in edible oil prices has been the single largest swing factor
in our gross margin this year.

Analyst: What is the order book position in the institutional business?

Kavita Raman: Order book stands at Rs 2,850 crore. Execution period is typically
9 months. Book to bill is 1.4x for the institutional segment.

Analyst: Any update on the debt position?

Suresh Iyer: Total borrowings are Rs 2,180 crore. Our blended cost of debt is
7.8% and average maturity is 4.2 years. We are rated CRISIL AA+ for our long
term facilities. Covenants require net debt to EBITDA below 2.5 times, and we
are comfortably within that.
"""

CREDIT_RATING_TEXT = """CRISIL Ratings Limited
Credit Rating Report — Bharat Consumer Products Limited
Rating Action: Reaffirmed

The Company has been rated CRISIL AA+/Stable for its long-term bank facilities.

Business Overview

Bharat Consumer Products Limited is a leading player in the Indian packaged
foods industry. The Company competes with Hindustan Unilever Limited and
Britannia Industries Limited.

Key customers include Reliance Retail Limited and Avenue Supermarts Limited.
Key suppliers are Adani Wilmar Limited and Ruchi Soya Industries Limited.

Financial Risk Profile

Total borrowings stood at Rs 2,180 crore as at 31 March 2025. Blended cost of
debt is 7.8%. Covenants require the net debt to EBITDA ratio to remain below 2.5
times through the tenure of the facility.

Risk Factors

Exposure to agricultural commodity price volatility remains the key rating
sensitivity for the Company over the medium term.
"""


def annual_report_pdf() -> bytes:
    return _build_pdf(ANNUAL_REPORT_TEXT, tables=[ANNUAL_REPORT_TABLE])


def concall_pdf() -> bytes:
    return _build_pdf(CONCALL_TEXT)


def credit_rating_pdf() -> bytes:
    return _build_pdf(CREDIT_RATING_TEXT)


def _build_pdf(text: str, tables: list[list[list[str]]] | None = None) -> bytes:
    """Render text (and optional tables) into a real, multi-page PDF.

    Headings are drawn in a larger bold face so the section detector has
    genuine typography to work with rather than a synthetic hint.
    """
    import fitz

    doc = fitz.open()
    known_headings = {
        "Chairman's Letter", "Business Overview", "Management Discussion and Analysis",
        "Risk Factors", "Corporate Governance", "Environmental Social and Governance",
        "Notes to Accounts", "Shareholding Pattern",
        "Independent Auditor's Report", "Financial Statements",
        "Management Guidance", "Question and Answer Session", "Financial Risk Profile",
        "Statement of Profit and Loss",
    }

    page = doc.new_page()
    y = 60.0
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        is_heading = block in known_headings
        size = 16 if is_heading else 10
        font = "hebo" if is_heading else "helv"
        leading = size * 1.4

        # Wrap explicitly and draw line by line. Earlier revisions handed a
        # whole paragraph to insert_textbox with an estimated height; when the
        # estimate was short the text overflowed into the region reserved for
        # the following block, so a 16pt heading was consumed by the paragraph
        # before it and vanished from the document. Two sections were lost
        # that way, and the section detector was blamed before the fixture was.
        lines: list[str] = []
        for logical in block.split("\n"):
            words = logical.split()
            if not words:
                continue
            current = words[0]
            for word in words[1:]:
                candidate = f"{current} {word}"
                if fitz.get_text_length(candidate, fontname=font, fontsize=size) > 470:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            lines.append(current)

        for line in lines:
            if y + leading > 780:
                page = doc.new_page()
                y = 60.0
            page.insert_text(
                fitz.Point(50, y + size), line, fontsize=size, fontname=font
            )
            y += leading
        y += 12 if is_heading else 8

    for table in tables or []:
        page = doc.new_page()
        y = 60.0
        page.insert_textbox(
            fitz.Rect(50, y, 545, y + 24), "Statement of Profit and Loss",
            fontsize=16, fontname="hebo",
        )
        y += 40
        # The label column must be wide enough for the longest label. An
        # earlier version used 115pt for all three columns, which silently
        # clipped four long labels and produced a table with 8 labels against
        # 12 figures. The extractor correctly refused to pair them.
        col_x = [50.0, 300.0, 420.0]
        col_w = [240.0, 110.0, 110.0]
        for row_index, row in enumerate(table):
            font = "hebo" if row_index == 0 else "helv"
            for col_index, cell in enumerate(row):
                page.insert_textbox(
                    fitz.Rect(col_x[col_index], y, col_x[col_index] + col_w[col_index], y + 16),
                    cell, fontsize=9, fontname=font,
                )
            # Ruling line under the header so pdfplumber's lattice mode engages.
            if row_index == 0:
                page.draw_line(fitz.Point(50, y + 16), fitz.Point(535, y + 16))
            y += 20
        page.draw_rect(fitz.Rect(48, 92, 537, y - 4))
        for x in (295.0, 415.0):
            page.draw_line(fitz.Point(x, 92), fitz.Point(x, y - 4))

    payload = doc.tobytes()
    doc.close()
    return payload


def scanned_pdf(text: str = "SCANNED FILING\nRevenue Rs 1,234 crore") -> bytes:
    """A PDF with no text layer at all — forces the OCR decision path."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    render = fitz.open()
    render_page = render.new_page()
    render_page.insert_textbox(
        fitz.Rect(40, 40, 500, 300), text, fontsize=22, fontname="hebo"
    )
    pixmap = render_page.get_pixmap(matrix=fitz.Matrix(2, 2))
    page.insert_image(page.rect, pixmap=pixmap)
    render.close()
    payload = doc.tobytes()
    doc.close()
    return payload
