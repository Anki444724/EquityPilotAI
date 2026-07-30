"""Primary financial data source — screener.in.

Chosen over Yahoo as the primary for three reasons that matter to this
platform specifically:

1. **Twelve years of history**, against Yahoo's four. The workbook assumes ten
   (`00 Setup` §A), Modules 2 and 5 compute 10-year CAGRs and trend scores,
   and a four-year window makes most of that meaningless.
2. **Reported natively in ₹ crore**, which is the workbook's unit throughout.
   No conversion, so no place for a factor-of-10⁷ error to hide.
3. **Indian consolidated statements**, presented the way an Indian analyst
   expects — the same aggregation the workbook's own `0A Data Import` sheet is
   built to receive.

The cost is granularity: screener aggregates expenses into one line, where the
canonical schema wants raw materials, employee benefit and other expenses
separately. Yahoo supplies that breakdown, so the two are merged in
`ingest.py` with screener winning on any overlap — and the disagreement
between them is recorded, because two independent sources differing on a
number is exactly the signal a validation sprint exists to surface.

This is a **secondary source, not a filing.** Every fact is stamped
`source="screener.in"` and carries `Precedence.IMPORT`, so provenance travels
with the number.
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser

_BASE = "https://www.screener.in/company"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}

MIN_INTERVAL = 1.1
_last_request_at = 0.0


class ScreenerError(Exception):
    pass


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def _fetch(url: str, *, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        _throttle()
        try:
            # nosec B310 — the scheme is fixed https and the host is a
            # module constant (screener.in); `url` is assembled here from a
            # ticker, never taken from a request. No file:// or custom
            # scheme can reach this call.
            request = urllib.request.Request(url, headers=_HEADERS)  # noqa: S310
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                raise ScreenerError(f"not listed: {url}") from exc
            if exc.code == 429:
                global MIN_INTERVAL
                MIN_INTERVAL = min(MIN_INTERVAL * 1.5, 8.0)
                time.sleep(6.0 * (attempt + 1))
            else:
                time.sleep(2.0 ** (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(2.0 ** (attempt + 1))
    raise ScreenerError(f"{type(last).__name__}: {last}")


class _TableParser(HTMLParser):
    """Minimal table extractor.

    Written by hand rather than pulled from BeautifulSoup because the shape is
    trivial and the dependency is already used elsewhere for documents; one
    fewer parsing behaviour to reason about when a row goes missing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(c for c in self._row):
                self.rows.append(self._row)
            self._row = None


def _number(text: str) -> float | None:
    """Parse a screener cell.

    Handles thousands separators, percentages, and the various dashes used for
    "not reported". Returns None for anything unparseable — an absent fact must
    stay absent rather than silently becoming zero, because the canonical
    builder treats those differently and a fabricated zero propagates into
    every ratio built on it.
    """
    if not text:
        return None
    cleaned = text.strip().replace(",", "").replace("%", "").replace("\u2212", "-")
    if cleaned in ("", "-", "--", "—", "–", "NA", "N/A"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fiscal_year(header: str) -> int | None:
    """`Mar 2025` → 2025. Anything else (`TTM`, blank) is not a fiscal year."""
    match = re.search(r"(Mar|Jun|Sep|Dec)\s+(\d{4})", header)
    if not match:
        return None
    month, year = match.group(1), int(match.group(2))
    # A December year-end belongs to the fiscal year it falls within.
    return year if month in ("Mar", "Jun") else year + 1


@dataclass(slots=True)
class ScreenerFinancials:
    """One company's statements as screener reports them, in ₹ crore."""

    ticker: str
    fiscal_years: list[int] = field(default_factory=list)
    #: section → row label → {fiscal_year: value}
    profit_loss: dict[str, dict[int, float]] = field(default_factory=dict)
    balance_sheet: dict[str, dict[int, float]] = field(default_factory=dict)
    cash_flow: dict[str, dict[int, float]] = field(default_factory=dict)
    ratios: dict[str, dict[int, float]] = field(default_factory=dict)
    #: Headline figures from the company banner — used for cross-checking.
    summary: dict[str, float] = field(default_factory=dict)
    price: float | None = None
    market_cap: float | None = None
    warnings: list[str] = field(default_factory=list)

    def row(self, section: str, label: str, year: int) -> float | None:
        table = getattr(self, section, {})
        for key, series in table.items():
            if key.lower().rstrip(" +").strip() == label.lower():
                return series.get(year)
        return None

    @property
    def latest_year(self) -> int | None:
        return max(self.fiscal_years) if self.fiscal_years else None


def _parse_section(html: str, section_id: str) -> tuple[dict[str, dict[int, float]], list[int]]:
    match = re.search(
        rf'<section id="{section_id}".*?(?=<section id=|\Z)', html, re.S,
    )
    if not match:
        return {}, []

    parser = _TableParser()
    parser.feed(match.group(0))
    if not parser.rows:
        return {}, []

    header = parser.rows[0]
    years: list[int | None] = [_fiscal_year(cell) for cell in header]
    valid = [(index, year) for index, year in enumerate(years) if year is not None]
    if not valid:
        return {}, []

    table: dict[str, dict[int, float]] = {}
    for row in parser.rows[1:]:
        label = row[0].strip()
        # `Compounded Sales Growth` and friends are footers, not data rows.
        if not label or label.endswith(":") or "Compounded" in label:
            continue
        series: dict[int, float] = {}
        for index, year in valid:
            if index < len(row):
                value = _number(row[index])
                if value is not None:
                    series[year] = value
        if series:
            table[label] = series

    return table, sorted({year for _, year in valid})


#: NSE symbol → screener.in slug, where they differ.
#:
#: Screener keys on its own historical slug, which lags corporate actions: a
#: merger, demerger or rename leaves the NSE symbol pointing at nothing. Every
#: entry here was found by probing, not guessed, and each is a real corporate
#: event rather than a typo:
#:
#:   LTIM        LTI merged with Mindtree (2022); screener kept MINDTREE
#:   TATAMOTORS  demerged into CV and PV entities (2025); PV arm is TMPV
#:   ZOMATO      renamed Eternal Ltd (2025)
#:
#: Without this map those three simply vanish from the universe, which is a
#: silent coverage hole rather than a visible failure.
SLUG_ALIASES: dict[str, str] = {
    "LTIM": "MINDTREE",
    "TATAMOTORS": "TMPV",
    "ZOMATO": "ETERNAL",
}


def fetch_screener(ticker: str, *, consolidated: bool = True) -> ScreenerFinancials:
    """Fetch and parse one company.

    Consolidated by default: `32 Documentation` §C names mixing standalone and
    consolidated as *"the single most common modelling error in Indian equity
    research"*. Companies with no consolidated statements fall back to
    standalone, and the fallback is recorded as a warning rather than passing
    silently.
    """
    slug = SLUG_ALIASES.get(ticker, ticker)

    def _load(url: str):
        html = _fetch(url)
        tables, all_years = {}, set()
        for section_id, attribute in (
            ("profit-loss", "profit_loss"),
            ("balance-sheet", "balance_sheet"),
            ("cash-flow", "cash_flow"),
            ("ratios", "ratios"),
        ):
            table, section_years = _parse_section(html, section_id)
            tables[attribute] = table
            all_years.update(section_years)
        return tables, all_years, html

    out = ScreenerFinancials(ticker=ticker)
    if slug != ticker:
        out.warnings.append(f"resolved via screener slug '{slug}' (corporate action)")

    tables = years = html = None
    if consolidated:
        try:
            tables, years, html = _load(f"{_BASE}/{slug}/consolidated/")
        except ScreenerError:
            tables = None

    # A company with no consolidated accounts — a standalone insurer, a
    # single-entity manufacturer — serves the consolidated page with HTTP 200
    # and an **empty table**, not a 404. The first version only caught the
    # 404, so PAGEIND, SBILIFE, BDL and ICICIGI were all recorded as "no
    # profit & loss table" when in fact they simply file standalone.
    # Emptiness, not the status code, is the signal to fall back.
    if tables is None or not tables.get("profit_loss"):
        tables, years, html = _load(f"{_BASE}/{slug}/")
        if consolidated:
            out.warnings.append(
                "standalone statements — company files no consolidated accounts"
            )

    out.profit_loss = tables["profit_loss"]
    out.balance_sheet = tables["balance_sheet"]
    out.cash_flow = tables["cash_flow"]
    out.ratios = tables["ratios"]

    if not out.profit_loss:
        raise ScreenerError(f"no profit & loss table for {ticker}")

    out.fiscal_years = sorted(years)

    # Banner figures: market cap, price, book value, ROE and so on. These are
    # screener's own computed values and are used purely as an independent
    # check on ours — never as an input.
    for label, value in re.findall(
        r'<span class="name">\s*([^<]+?)\s*</span>\s*<span class="nowrap value">(.*?)</span>',
        html, re.S,
    ):
        number = _number(re.sub(r"<[^>]+>|₹|,|Cr\.?|%", "", value))
        if number is not None:
            out.summary[label.strip().rstrip(":")] = number

    out.price = out.summary.get("Current Price")
    out.market_cap = out.summary.get("Market Cap")
    return out
