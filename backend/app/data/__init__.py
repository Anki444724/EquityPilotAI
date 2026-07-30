"""Real market data ingestion.

Replaces the synthetic generator in `db/seed.py` with reported financials for
real NSE-listed companies.

    python -m app.data          # ingest, derive, and report coverage

Two sources, one canonical fact store:

* **screener.in** — primary. Twelve years of consolidated Indian statements,
  reported natively in ₹ crore, presented the way the workbook's schema
  expects.
* **Yahoo Finance** — secondary. Supplies the expense and balance-sheet
  granularity screener aggregates away, plus quotes and price history. Rate
  limited aggressively from a single IP, so it is strictly optional: a run
  without it produces a complete, validated dataset with working-capital
  items derived from screener's own reported days ratios instead.

Neither is a filing. Every fact carries `source` and `Precedence.STORE`, so
provenance travels with the number and the UI can say where it came from.
"""
from __future__ import annotations

from app.data.derive_wc import derive_from_ratios, derive_universe
from app.data.enrich import enrich_company, enrich_universe
from app.data.ingest import ingest_company, ingest_universe
from app.data.nse_universe import NSE_UNIVERSE, is_financial
from app.data.validate import Validator

__all__ = [
    "NSE_UNIVERSE", "Validator", "derive_from_ratios", "derive_universe",
    "enrich_company", "enrich_universe", "ingest_company", "ingest_universe",
    "is_financial",
]
