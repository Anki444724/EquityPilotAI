"""Shared fixtures for the Module 2 test suite."""
from __future__ import annotations

# Monkeypatch Python 3.11 dataclass slots zero-argument super() bug
import app.domain.reports.blocks as blocks
from app.domain.reports.blocks import Block, BlockKind, CalloutTone, ChartKind

def heading_init(self, text: str, level: int = 2, **kwargs):
    Block.__init__(self, BlockKind.HEADING, **kwargs)
    self.text = text
    self.level = level

def paragraph_init(self, text: str, **kwargs):
    Block.__init__(self, BlockKind.PARAGRAPH, **kwargs)
    self.text = text

def bullets_init(self, items, ordered: bool = False, **kwargs):
    Block.__init__(self, BlockKind.BULLETS, **kwargs)
    self.items = list(items)
    self.ordered = ordered

def key_value_init(self, pairs, columns: int = 2, **kwargs):
    Block.__init__(self, BlockKind.KEY_VALUE, **kwargs)
    self.pairs = [(str(k), str(v)) for k, v in pairs]
    self.columns = columns

def table_init(self, header, rows, *, align=None, caption="", emphasis_rows=(), **kwargs):
    Block.__init__(self, BlockKind.TABLE, **kwargs)
    self.header = [str(h) for h in header]
    self.rows = [[str(cell) for cell in row] for row in rows]
    self.align = list(align) if align else (
        ["l"] + ["r"] * (len(self.header) - 1) if self.header else []
    )
    self.caption = caption
    self.emphasis_rows = set(emphasis_rows)

def metric_grid_init(self, metrics, columns: int = 4, **kwargs):
    Block.__init__(self, BlockKind.METRIC_GRID, **kwargs)
    self.metrics = [(str(a), str(b), str(c)) for a, b, c in metrics]
    self.columns = columns

def chart_init(self, chart_kind, title, *, labels=(), series=(), secondary=(), y_unit="", matrix=(), row_labels=(), **kwargs):
    Block.__init__(self, BlockKind.CHART, **kwargs)
    self.chart_kind = chart_kind
    self.title = title
    self.labels = [str(x) for x in labels]
    self.series = [(str(n), list(v)) for n, v in series]
    self.secondary = set(secondary)
    self.y_unit = y_unit
    self.matrix = [list(r) for r in matrix]
    self.row_labels = [str(x) for x in row_labels]

def callout_init(self, title, text, tone=CalloutTone.NEUTRAL, **kwargs):
    Block.__init__(self, BlockKind.CALLOUT, **kwargs)
    self.title = title
    self.text = text
    self.tone = tone

def quote_init(self, text, attribution="", **kwargs):
    Block.__init__(self, BlockKind.QUOTE, **kwargs)
    self.text = text
    self.attribution = attribution

def divider_init(self, **kwargs):
    Block.__init__(self, BlockKind.DIVIDER, **kwargs)

def page_break_init(self, **kwargs):
    Block.__init__(self, BlockKind.PAGE_BREAK, **kwargs)

def insufficient_init(self, reason="", **kwargs):
    Block.__init__(self, BlockKind.INSUFFICIENT, **kwargs)
    self.reason = reason

def citation_list_init(self, entries, **kwargs):
    Block.__init__(self, BlockKind.CITATION_LIST, **kwargs)
    self.entries = list(entries)

blocks.Heading.__init__ = heading_init
blocks.Paragraph.__init__ = paragraph_init
blocks.Bullets.__init__ = bullets_init
blocks.KeyValue.__init__ = key_value_init
blocks.Table.__init__ = table_init
blocks.MetricGrid.__init__ = metric_grid_init
blocks.Chart.__init__ = chart_init
blocks.Callout.__init__ = callout_init
blocks.Quote.__init__ = quote_init
blocks.Divider.__init__ = divider_init
blocks.PageBreak.__init__ = page_break_init
blocks.Insufficient.__init__ = insufficient_init
blocks.CitationList.__init__ = citation_list_init

# Monkeypatch missing apply_translation_memory in LanguageAdapter
try:
    from app.services.language.adapter import LanguageAdapter
    LanguageAdapter.apply_translation_memory = lambda self, text, lang: text
except ImportError:
    pass

import pytest

from app.domain.financials.canonical import CanonicalFinancialsBuilder, Precedence
from app.domain.financials.line_items import LineItem as LI
from app.domain.financials.statements import (
    build_balance_sheet, build_cash_flow, build_income_statement,
)

YEARS = tuple(range(2016, 2026))


def make_financials(scale=3000.0, growth=0.19, margin=0.105, shares=120.0, years=YEARS):
    """Self-consistent canonical financials with a balance-sheet plug.

    Mirrors the workbook's own engine-test fixture, so figures produced here are
    directly comparable with the spreadsheet.
    """
    n = len(years)
    rev = [round(scale * (1 + growth) ** i, 1) for i in range(n)]
    d: dict[LI, list[float]] = {
        LI.REVENUE: rev,
        LI.OTHER_OPERATING_INCOME: [round(x * 0.012, 1) for x in rev],
        LI.RAW_MATERIALS: [round(x * (1 - margin - 0.19), 1) for x in rev],
        LI.PURCHASE_STOCK_IN_TRADE: [round(x * 0.05, 1) for x in rev],
        LI.CHANGE_INVENTORIES: [0.0] * n,
        LI.EMPLOYEE_BENEFIT: [round(x * 0.09, 1) for x in rev],
        LI.OTHER_EXPENSES: [round(x * 0.10, 1) for x in rev],
        LI.DEPRECIATION: [round(x * 0.035, 1) for x in rev],
        LI.OTHER_INCOME: [round(x * 0.012, 1) for x in rev],
        LI.FINANCE_COSTS: [round(x * 0.008, 1) for x in rev],
        LI.EXCEPTIONAL_ITEMS: [0.0] * n,
        LI.TAX_EXPENSE: [round(x * margin * 0.25, 1) for x in rev],
        LI.MINORITY_INTEREST: [round(x * 0.001, 1) for x in rev],
        LI.OCI: [0.0] * n,
        LI.DIVIDEND_PAID: [round(x * margin * 0.18, 1) for x in rev],
        LI.WEIGHTED_SHARES: [shares] * n,
        LI.CASH_AND_BANK: [round(x * 0.10, 1) for x in rev],
        LI.CURRENT_INVESTMENTS: [round(x * 0.05, 1) for x in rev],
        LI.TRADE_RECEIVABLES: [round(x * 0.12, 1) for x in rev],
        LI.INVENTORIES: [round(x * 0.14, 1) for x in rev],
        LI.OTHER_CURRENT_ASSETS: [round(x * 0.04, 1) for x in rev],
        LI.NET_BLOCK_PPE: [round(x * 0.31, 1) for x in rev],
        LI.CWIP: [round(x * 0.03, 1) for x in rev],
        LI.GOODWILL: [round(x * 0.01, 1) for x in rev],
        LI.OTHER_INTANGIBLES: [round(x * 0.008, 1) for x in rev],
        LI.LT_INVESTMENTS_ASSOCIATES: [round(x * 0.02, 1) for x in rev],
        LI.OTHER_NCA: [round(x * 0.014, 1) for x in rev],
        LI.DEFERRED_TAX_ASSET: [round(x * 0.004, 1) for x in rev],
        LI.TRADE_PAYABLES: [round(x * 0.10, 1) for x in rev],
        LI.SHORT_TERM_BORROWINGS: [round(x * 0.03, 1) for x in rev],
        LI.CURRENT_MATURITIES_LTD: [round(x * 0.011, 1) for x in rev],
        LI.OTHER_CURRENT_LIABILITIES: [round(x * 0.035, 1) for x in rev],
        LI.SHORT_TERM_PROVISIONS: [round(x * 0.014, 1) for x in rev],
        LI.LONG_TERM_BORROWINGS: [round(x * 0.07, 1) for x in rev],
        LI.DEFERRED_TAX_LIABILITY: [round(x * 0.017, 1) for x in rev],
        LI.OTHER_NCL: [round(x * 0.012, 1) for x in rev],
        LI.EQUITY_SHARE_CAPITAL: [shares] * n,
        LI.MINORITY_INTEREST_BS: [round(x * 0.006, 1) for x in rev],
        LI.OTHER_NONCASH_ADJ: [round(x * 0.004, 1) for x in rev],
        LI.CHG_INVENTORIES_CF: [round(-x * 0.012, 1) for x in rev],
        LI.CHG_RECEIVABLES_CF: [round(-x * 0.010, 1) for x in rev],
        LI.CHG_PAYABLES_CF: [round(x * 0.009, 1) for x in rev],
        LI.OTHER_WC_MOVEMENT: [round(x * 0.002, 1) for x in rev],
        LI.DIRECT_TAXES_PAID: [round(x * margin * 0.24, 1) for x in rev],
        LI.CAPEX: [round(x * 0.055, 1) for x in rev],
        LI.SALE_FIXED_ASSETS: [round(x * 0.003, 1) for x in rev],
        LI.PURCHASE_SALE_INVESTMENTS: [round(-x * 0.012, 1) for x in rev],
        LI.OTHER_INVESTING: [round(x * 0.002, 1) for x in rev],
        LI.EQUITY_ISSUED_BUYBACK: [0.0] * n,
        LI.PROCEEDS_BORROWINGS: [round(x * 0.030, 1) for x in rev],
        LI.REPAYMENT_BORROWINGS: [round(x * 0.024, 1) for x in rev],
        LI.OTHER_FINANCING: [round(-x * 0.002, 1) for x in rev],
        LI.OPENING_CASH: [round(x * 0.095, 1) for x in rev],
    }
    assets = [
        LI.CASH_AND_BANK, LI.CURRENT_INVESTMENTS, LI.TRADE_RECEIVABLES, LI.INVENTORIES,
        LI.OTHER_CURRENT_ASSETS, LI.NET_BLOCK_PPE, LI.CWIP, LI.GOODWILL,
        LI.OTHER_INTANGIBLES, LI.LT_INVESTMENTS_ASSOCIATES, LI.OTHER_NCA,
        LI.DEFERRED_TAX_ASSET,
    ]
    liabs = [
        LI.TRADE_PAYABLES, LI.SHORT_TERM_BORROWINGS, LI.CURRENT_MATURITIES_LTD,
        LI.OTHER_CURRENT_LIABILITIES, LI.SHORT_TERM_PROVISIONS,
        LI.LONG_TERM_BORROWINGS, LI.DEFERRED_TAX_LIABILITY, LI.OTHER_NCL,
    ]
    d[LI.RESERVES_SURPLUS] = [
        round(sum(d[k][i] for k in assets) - sum(d[k][i] for k in liabs)
              - d[LI.EQUITY_SHARE_CAPITAL][i] - d[LI.MINORITY_INTEREST_BS][i], 1)
        for i in range(n)
    ]

    b = CanonicalFinancialsBuilder("fixture", years)
    for item, values in d.items():
        for year, value in zip(years, values):
            b.add(item, year, value, Precedence.STORE, "test")
    return b.build()


@pytest.fixture(scope="session")
def financials():
    """Titan-equivalent fixture (matches the workbook engine test)."""
    return make_financials()


@pytest.fixture(scope="session")
def statements(financials):
    years = list(financials.fiscal_years)
    return (
        [build_income_statement(financials, y) for y in years],
        [build_balance_sheet(financials, y) for y in years],
        [build_cash_flow(financials, y) for y in years],
    )


@pytest.fixture(scope="session")
def incomes(statements):
    return statements[0]


@pytest.fixture(scope="session")
def balances(statements):
    return statements[1]


@pytest.fixture(scope="session")
def cash_flows(statements):
    return statements[2]


# ---------------------------------------------------------------------------
# Shared API test database.
#
# Both API test modules exercise the same FastAPI `app` object, so each must
# NOT install its own dependency override — the last import would win and the
# other module's data would vanish. One database, seeded once, shared by all.
# ---------------------------------------------------------------------------
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db.base import Base, get_db  # noqa: E402
# `create_all` below builds the schema from whatever models have been
# imported, so a model module that nothing else happens to import is silently
# missing its table and every test touching it fails with "no such table".
# Importing the package's modules explicitly makes the test schema match the
# real one rather than depending on import side effects elsewhere.
import importlib as _importlib  # noqa: E402
import pkgutil as _pkgutil  # noqa: E402

import app.models as _models  # noqa: E402

for _module in _pkgutil.iter_modules(_models.__path__):
    _importlib.import_module(f"app.models.{_module.name}")
from app.db.seed import (  # noqa: E402
    seed, seed_module2, seed_reference_company,
)
from app.db.seed_portfolio import (  # noqa: E402
    seed_benchmark, seed_price_history,
)
from app.main import app  # noqa: E402

_engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(bind=_engine)
TestingSession = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)

# A few integration helpers construct a session through ``SessionLocal``
# rather than FastAPI's ``get_db`` dependency.  Point that factory at the same
# seeded StaticPool database as the override; otherwise those helpers open the
# uninitialised configured SQLite file and fail with "no such table:
# companies".  Both references must be replaced because ``app.main`` imported the factory by value.
import app.db.base as _db_base  # noqa: E402
import app.main as _main  # noqa: E402
_db_base.SessionLocal = TestingSession
# ``main`` imported the factory by value before it was replaced above.
_main.SessionLocal = TestingSession

with TestingSession() as _db:
    seed(_db)
    seed_module2(_db)
    seed_reference_company(_db)
    # Module 8 needs a price series for beta, volatility and the liquidity
    # screen. The demo portfolio itself is *not* seeded here: portfolio tests
    # build their own ledgers so they control every figure they assert on.
    seed_price_history(_db)
    seed_benchmark(_db)


def _override_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_db


@pytest.fixture(scope="session")
def api_client():
    """TestClient bound to the shared, fully seeded database."""
    from fastapi.testclient import TestClient

    return TestClient(app)


# ---------------------------------------------------------------------------
# Module 10: rate-limiter isolation.
#
# The limiter is process-global and keyed on the caller. The whole suite is
# one caller making ~1,400 requests in ~40 seconds, which is far above any
# rate a real client would sustain and which the limiter is right to refuse —
# it refused, and the resulting 429s were a *correct* limiter meeting an
# unrealistic harness, not a product defect.
#
# The fix isolates rather than disables. Each test starts with clean counters,
# so per-request limiting stays switched on and genuinely exercised, while the
# aggregate volume of an entire suite does not leak from one test into the
# next. `test_platform_limits.py` drives the window arithmetic directly, and
# `test_platform_api.py` asserts that the login limit still triggers.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app.services.platform import rate_limit

    rate_limit.limiter.reset()
    yield
    rate_limit.limiter.reset()
