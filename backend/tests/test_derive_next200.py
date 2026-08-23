"""The next-200 derivation logic: exclusion, ranking, report shape.

Pure tests — no network, no database. The network wiring lives in
``deploy/derive_next_200.py`` and reuses sources the platform already runs
in production (the BSE master the Nifty 500 importer joins, screener.in the
financials backfill ingests from, FMP the market router reads from).
"""
from __future__ import annotations

import pytest

from app.services.universe.next200 import (
    BseScrip, UniverseEntry, calibrate_bse_mktcap, classify, normalise_name,
    parse_bse_master, rank_candidates, rows_to_dicts, to_csv, to_json,
    universe_sets,
)


def _scrip(scrip_code="500325", ticker="RELIANCE", name="Reliance Industries Ltd",
           isin="INE002A01018", sector="Oil Gas & Consumable Fuels",
           mktcap=None) -> BseScrip:
    return BseScrip(scrip_code=scrip_code, ticker=ticker, name=name,
                    isin=isin, sector=sector, mktcap=mktcap)


class TestParseBseMaster:
    def test_keeps_only_active_equity(self):
        rows = [
            {"SCRIP_CD": "1", "COMP_NAME": "Equity Active", "SYMBOL": "EQA",
             "SEGMENT": "Equity", "SCRIP_STATUS": "Active",
             "ISIN_NUMBER": "INE111A01001"},
            {"SCRIP_CD": "2", "COMP_NAME": "Debt Scrip", "SYMBOL": "DBT",
             "SEGMENT": "Debt", "SCRIP_STATUS": "Active",
             "ISIN_NUMBER": "INE222A01002"},
            {"SCRIP_CD": "3", "COMP_NAME": "Delisted Equity", "SYMBOL": "DLX",
             "SEGMENT": "Equity", "SCRIP_STATUS": "Delisted",
             "ISIN_NUMBER": "INE333A01003"},
        ]
        parsed = parse_bse_master(rows)
        assert [s.ticker for s in parsed] == ["EQA"]
        assert parsed[0].isin == "INE111A01001"

    def test_rows_without_symbol_are_kept_with_empty_ticker(self):
        parsed = parse_bse_master([{
            "SCRIP_CD": "99", "COMP_NAME": "BSE Only Co",
            "SEGMENT": "Equity", "SCRIP_STATUS": "Active",
            "ISIN_NUMBER": "INE777B01007",
        }])
        assert len(parsed) == 1
        assert parsed[0].ticker == ""
        assert parsed[0].name == "BSE Only Co"

    def test_rows_missing_code_or_name_are_dropped(self):
        assert parse_bse_master([
            {"COMP_NAME": "No Code", "SEGMENT": "Equity",
             "SCRIP_STATUS": "Active"},
            {"SCRIP_CD": "5", "SEGMENT": "Equity", "SCRIP_STATUS": "Active"},
        ]) == []


class TestParseActualBsePayload:
    """The field names the live BSE API actually returns.

    Production run returned 0 scrips because the parser looked for
    ``COMP_NAME``/``COMPANY_NAME`` while the current payload generation
    sends ``Scrip_Name`` — every row failed the name check and was
    discarded. These tests pin the real shape.
    """

    ACTUAL_ROW = {
        "SCRIP_CD": "500325",
        "Scrip_Name": "Reliance Industries Ltd",
        "Status": "Active",
        "Segment": "Equity",
        "ISIN_NUMBER": "INE002A01018",
        "INDUSTRY": "Petroleum Products",
        "scrip_id": "RELIANCE",
        "Mktcap": 177817559000000.0,
    }

    def test_actual_fields_parse(self):
        [scrip] = parse_bse_master([self.ACTUAL_ROW])
        assert scrip.scrip_code == "500325"
        assert scrip.name == "Reliance Industries Ltd"
        assert scrip.isin == "INE002A01018"
        assert scrip.ticker == "RELIANCE"          # from scrip_id
        assert scrip.industry == "Petroleum Products"
        assert scrip.mktcap == 177817559000000.0

    def test_mktcap_accepts_comma_grouped_strings(self):
        row = dict(self.ACTUAL_ROW, Mktcap="1,77,817,559,000,000.00")
        assert parse_bse_master([row])[0].mktcap == 177817559000000.0

    def test_mktcap_missing_is_none(self):
        row = {k: v for k, v in self.ACTUAL_ROW.items() if k != "Mktcap"}
        assert parse_bse_master([row])[0].mktcap is None

    def test_symbol_wins_over_scrip_id(self):
        row = dict(self.ACTUAL_ROW, SYMBOL="REL")
        assert parse_bse_master([row])[0].ticker == "REL"

    def test_debt_segment_filtered_with_actual_names(self):
        row = dict(self.ACTUAL_ROW, Segment="Debt", Status="Active")
        assert parse_bse_master([row]) == []

    def test_delisted_status_filtered_with_actual_names(self):
        row = dict(self.ACTUAL_ROW, Status="Delisted")
        assert parse_bse_master([row]) == []

    def test_old_all_caps_payload_still_parses(self):
        """Backward compatibility with the pre-migration field names."""
        legacy = {
            "SCRIP_CD": "1234",
            "COMP_NAME": "Legacy Co Ltd",
            "SYMBOL": "LEGACY",
            "SEGMENT": "Equity",
            "SCRIP_STATUS": "Active",
            "ISIN_NUMBER": "INE123A01012",
            "BSE_SECTOR": "Chemicals",
            "INDUSTRY_NAME": "Speciality Chemicals",
        }
        [scrip] = parse_bse_master([legacy])
        assert scrip.name == "Legacy Co Ltd"
        assert scrip.ticker == "LEGACY"
        assert scrip.sector == "Chemicals"
        assert scrip.industry == "Speciality Chemicals"
        assert scrip.mktcap is None


class TestCalibrateBseMktcap:
    """The Mktcap unit is undocumented — the calibration must be right."""

    def test_rupee_scale_detected(self):
        scrips = [
            _scrip(isin="INE002A01018", mktcap=1.78e14),  # ~₹17.8 lakh cr
        ]
        assert calibrate_bse_mktcap(scrips) == ("rupee", 1e-7)

    def test_crore_scale_detected(self):
        scrips = [
            _scrip(isin="INE002A01018", mktcap=1.78e7),  # 17.8 lakh crore
        ]
        assert calibrate_bse_mktcap(scrips) == ("crore", 1.0)

    def test_missing_reference_falls_back_to_market_leader(self):
        # No RELIANCE row, but the biggest scrip is far beyond the unit
        # boundary, so rupees are still the only consistent unit.
        scrips = [
            _scrip(ticker="BIGCO", isin="INE999A01999", mktcap=2.5e14),
            _scrip(ticker="SMALL", isin="INE000A01000", mktcap=3e11),
        ]
        assert calibrate_bse_mktcap(scrips) == ("rupee", 1e-7)

    def test_no_figures_at_all(self):
        scrips = [_scrip(), _scrip(ticker="B", isin=None)]
        assert calibrate_bse_mktcap(scrips) == (None, 0.0)

    def test_ambiguous_reference_is_refused_not_guessed(self):
        # 1e9 < x <= 1e12: could be a small company in rupees or a big one
        # in crore — the unit must not be assumed.
        scrips = [
            _scrip(isin="INE002A01018", mktcap=5e11),
        ]
        assert calibrate_bse_mktcap(scrips) == (None, 0.0)


class TestCliBseMcapTier:
    """The CLI tier that turns the master's own Mktcap into the mcap map."""

    @staticmethod
    def _cli():
        import importlib.util
        import os

        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "deploy",
            "derive_next_200.py",
        )
        spec = importlib.util.spec_from_file_location("derive_next_200", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_keyed_by_isin_ticker_and_name(self):
        cli = self._cli()
        scrips = [
            _scrip(ticker="NEWCO", name="New Co Ltd",
                   isin="INE555D01555", mktcap=8.8e14),
            _scrip(ticker="RELIANCE", name="Reliance Industries Ltd",
                   isin="INE002A01018", mktcap=1.78e14),
        ]
        mcap_map, unit = cli.bse_master_market_caps(scrips)
        assert unit == "rupee"
        assert mcap_map["INE555D01555"] == (88_000_000.0, "bse_master")
        assert mcap_map["NEWCO"] == (88_000_000.0, "bse_master")
        assert mcap_map[normalise_name("New Co Ltd")] == (
            88_000_000.0, "bse_master")
        # RELIANCE's figure is there too (the CLI applies the universe
        # exclusion itself; the tier is universe-agnostic by design).
        assert mcap_map["INE002A01018"][1] == "bse_master"

    def test_unresolvable_unit_yields_empty_map(self):
        cli = self._cli()
        scrips = [_scrip(ticker="MIDCO", isin="INE111A01111", mktcap=5e11)]
        mcap_map, unit = cli.bse_master_market_caps(scrips)
        assert unit is None
        assert mcap_map == {}


class TestExclusion:
    UNIVERSE = [
        UniverseEntry(ticker="RELIANCE", isin="INE002A01018"),
        UniverseEntry(ticker="TCS", isin="INE467B01029"),
        UniverseEntry(ticker="AAPL", isin="US0378331005"),
        # Legacy row without ISIN (the 135 pre-ISIN rows the import report
        # describes) — must still be excluded by ticker.
        UniverseEntry(ticker="BHARATCP", isin=None),
    ]

    def setup_method(self):
        self.isins, self.tickers = universe_sets(self.UNIVERSE)

    def test_isin_match_excludes_regardless_of_ticker(self):
        """A renamed ticker keeps its ISIN — the only identity-proof match."""
        scrip = _scrip(ticker="RENAMED", isin="INE002A01018")
        assert classify(scrip, self.isins, self.tickers) == "excluded_isin_match"

    def test_ticker_match_excludes_when_isin_absent(self):
        scrip = _scrip(ticker="bharatcp", isin=None)
        assert classify(scrip, self.isins, self.tickers) == "excluded_ticker_match"

    def test_non_indian_isin_is_out_of_scope(self):
        """A foreign scrip the universe does not hold is out of scope.

        (AAPL *is* in this universe — a Phase 3 US listing — so it is
        excluded by ISIN match instead; a different US ISIN tests the
        scope rule itself.)
        """
        scrip = _scrip(ticker="MSFT", isin="US5949181045")
        assert classify(scrip, self.isins, self.tickers) == "excluded_non_indian_isin"

    def test_missing_isin_is_surfaced_not_dropped(self):
        scrip = _scrip(ticker="NOISIN", isin=None)
        assert classify(scrip, self.isins, self.tickers) == "excluded_no_isin"

    def test_new_indian_company_is_a_candidate(self):
        scrip = _scrip(ticker="NEWCO", name="New Co Ltd", isin="INE999X01999")
        assert classify(scrip, self.isins, self.tickers) == "candidate"


class TestRanking:
    def _scrips(self):
        return [
            _scrip("1", "RELIANCE", "Reliance Industries Ltd", "INE002A01018"),
            _scrip("2", "NEWCO1", "New Co One Ltd", "INE100A01100"),
            _scrip("3", "NEWCO2", "New Co Two Ltd", "INE200A01200"),
            _scrip("4", "NEWCO3", "New Co Three Ltd", "INE300A01300"),
            _scrip("5", "NOCAP", "No Cap Ltd", "INE400A01400"),
            _scrip("6", "AAPL", "Apple Inc", "US0378331005"),
        ]

    MCP = {
        "INE100A01100": (50_000.0, "screener.in"),
        "NEWCO2": (75_000.0, "fmp_screener"),
        "NEWCO3": (60_000.0, "screener.in"),
    }

    def test_ranks_descending_and_assigns_501_first(self):
        rows, counts = rank_candidates(
            self._scrips(), {"INE002A01018"}, {"RELIANCE", "AAPL"},
            self.MCP, limit=200,
        )
        # RELIANCE excluded (ISIN), AAPL out of scope, NOCAP unrankable.
        assert [r.ticker for r in rows] == ["NEWCO2", "NEWCO3", "NEWCO1"]
        assert [r.proposed_rank for r in rows] == [501, 502, 503]
        assert [r.market_cap_inr_crore for r in rows] == [75_000.0, 60_000.0,
                                                          50_000.0]

    def test_limit_truncates(self):
        rows, _ = rank_candidates(
            self._scrips(), {"INE002A01018"}, {"RELIANCE", "AAPL"},
            self.MCP, limit=2,
        )
        assert len(rows) == 2
        assert rows[0].proposed_rank == 501
        assert rows[1].proposed_rank == 502

    def test_ties_break_by_name_for_stable_output(self):
        scrips = [
            _scrip("1", "ZCO", "Zed Co", "INE111C01111"),
            _scrip("2", "ACO", "Alpha Co", "INE222C01222"),
        ]
        mcap = {"INE111C01111": (1_000.0, "x"), "INE222C01222": (1_000.0, "x")}
        rows, _ = rank_candidates(scrips, set(), set(), mcap)
        assert [r.ticker for r in rows] == ["ACO", "ZCO"]

    def test_counts_break_down_every_non_candidate(self):
        # Only RELIANCE is in the universe here, so the AAPL row exercises
        # the non-Indian scope rule rather than an ISIN match.
        _, counts = rank_candidates(
            self._scrips(), {"INE002A01018"}, {"RELIANCE"},
            self.MCP,
        )
        assert counts["excluded_isin_match"] == 1
        assert counts["excluded_non_indian_isin"] == 1
        assert counts["no_market_cap_available"] == 1  # NOCAP
        assert counts["candidate"] == 4

    def test_name_key_is_a_fallback_lookup(self):
        """A master row without a ticker is still rankable by normalised name."""
        scrips = [_scrip("9", "", "BSE Only Co", "INE777B01007")]
        mcap = {normalise_name("BSE Only Co"): (9_000.0, "screener.in")}
        rows, _ = rank_candidates(scrips, set(), set(), mcap)
        assert len(rows) == 1
        assert rows[0].market_cap_inr_crore == 9_000.0


class TestReportShape:
    def test_dicts_carry_the_required_columns(self):
        rows = rank_candidates(
            [_scrip("1", "NEWCO", "New Co Ltd", "INE555D01555")],
            set(), set(),
            {"INE555D01555": (12_345.6, "screener.in")},
        )[0]
        d = rows_to_dicts(rows)[0]
        assert d == {
            "proposed_rank": 501,
            "ticker": "NEWCO",
            "company_name": "New Co Ltd",
            "isin": "INE555D01555",
            "exchange": "BSE/NSE",
            "market_cap_inr_crore": 12_345.6,
            "mcap_source": "screener.in",
            "current_universe_status": "not_in_current_universe",
            "bse_scrip_code": "1",
            "sector": "Oil Gas & Consumable Fuels",
        }

    def test_csv_round_trips(self):
        rows = rank_candidates(
            [_scrip("1", "NEWCO", "New Co Ltd", "INE555D01555")],
            set(), set(),
            {"INE555D01555": (12_345.6, "screener.in")},
        )[0]
        text = to_csv(rows)
        lines = text.strip().splitlines()
        assert lines[0].startswith("proposed_rank,ticker,company_name,isin")
        assert len(lines) == 2
        assert "501" in lines[1] and "NEWCO" in lines[1] and "INE555D01555" in lines[1]

    def test_json_carries_summary_and_candidates(self):
        rows = rank_candidates(
            [_scrip("1", "NEWCO", "New Co Ltd", "INE555D01555")],
            set(), set(),
            {"INE555D01555": (12_345.6, "screener.in")},
        )[0]
        import json as _json

        doc = _json.loads(to_json(
            rows, generated_at="2026-08-23T00:00:00+00:00",
            market_cap_as_of="2026-08-23", summary={"reported": 1},
        ))
        assert doc["summary"] == {"reported": 1}
        assert doc["candidates"][0]["proposed_rank"] == 501
