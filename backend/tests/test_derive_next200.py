"""The next-200 derivation logic: exclusion, ranking, report shape.

Pure tests — no network, no database. The network wiring lives in
``deploy/derive_next_200.py`` and reuses sources the platform already runs
in production (the BSE master the Nifty 500 importer joins, screener.in the
financials backfill ingests from, FMP the market router reads from).
"""
from __future__ import annotations

import pytest

from app.services.universe.next200 import (
    BseScrip, CompanyCandidate, UniverseEntry, build_bse_identity_maps,
    build_bse_mktcap_map, calibrate_bse_mktcap, classify, classify_company,
    dedupe_candidates, normalise_name, parse_bse_master, rank_candidates,
    rank_companies, resolve_identity, rows_to_dicts, to_csv, to_json,
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


def _company(company_id="c1", ticker="NEWCO", name="New Co Ltd",
             isin="INE555D01555", exchange="NSE", listing_status="active",
             index_membership=None, currency="INR", sector=None,
             bse_code=None) -> CompanyCandidate:
    return CompanyCandidate(
        company_id=company_id, ticker=ticker, name=name, isin=isin,
        exchange=exchange, listing_status=listing_status,
        index_membership=index_membership, currency=currency,
        sector=sector, bse_code=bse_code,
    )


class TestClassifyCompany:
    """The database-driven candidate filter (NIFTY500 stays excluded)."""

    def test_nifty500_tag_excludes(self):
        c = _company(index_membership="NIFTY500")
        assert classify_company(c) == "excluded_nifty500_tag"

    def test_nifty500_tag_is_case_insensitive(self):
        c = _company(index_membership="  nifty500 ")
        assert classify_company(c) == "excluded_nifty500_tag"

    def test_live_list_catches_a_stale_tag(self):
        """A constituent added after the last import, tag never updated."""
        c = _company(index_membership=None)
        assert classify_company(
            c, live_nifty500_isins=frozenset({"INE555D01555"}),
        ) == "excluded_nifty500_live_list"

    def test_live_list_ticker_catches_no_isin_constituent(self):
        c = _company(isin=None)
        assert classify_company(
            c, live_nifty500_tickers=frozenset({"NEWCO"}),
        ) == "excluded_nifty500_live_list"

    def test_not_active_excluded(self):
        c = _company(listing_status="delisted")
        assert classify_company(c) == "excluded_not_active"

    def test_us_isin_excluded(self):
        c = _company(ticker="AAPL", isin="US0378331005", currency="USD")
        assert classify_company(c) == "excluded_non_indian"

    def test_no_isin_foreign_currency_excluded(self):
        c = _company(isin=None, currency="USD")
        assert classify_company(c) == "excluded_non_indian"

    def test_ine_isin_active_untagged_is_candidate(self):
        assert classify_company(_company()) == "candidate"

    def test_no_isin_inr_is_candidate(self):
        assert classify_company(_company(isin=None)) == "candidate"

    def test_unrelated_index_tag_is_not_nifty500(self):
        """Tagged for some other index: still a candidate pool member."""
        c = _company(index_membership="BSE500")
        assert classify_company(c) == "candidate"


class TestBuildBseMktcapMap:
    def test_rupee_values_converted_to_crore_with_codes(self):
        scrips = [
            _scrip(isin="INE002A01018", mktcap=1.78e14),  # RELIANCE
            _scrip(ticker="NEWCO", isin="INE555D01555",
                   scrip_code="543210", mktcap=8.8e12),
            _scrip(ticker="NOCAP", isin="INE777A01777", mktcap=None),
        ]
        mcap_map, unit = build_bse_mktcap_map(scrips)
        assert unit == "rupee"
        assert mcap_map["INE555D01555"] == (880_000.0, "543210")
        assert mcap_map["INE002A01018"] == (17_800_000.0, "500325")
        assert "INE777A01777" not in mcap_map  # no figure, no entry

    def test_unresolvable_unit_yields_empty_map(self):
        scrips = [_scrip(isin="INE002A01018", mktcap=5e11)]  # ambiguous
        assert build_bse_mktcap_map(scrips) == ({}, None)

    def test_crore_values_pass_through(self):
        scrips = [
            _scrip(isin="INE002A01018", mktcap=1.78e7),  # crore scale
            _scrip(ticker="NEWCO", isin="INE555D01555", mktcap=880_000.0),
        ]
        mcap_map, unit = build_bse_mktcap_map(scrips)
        assert unit == "crore"
        assert mcap_map["INE555D01555"][0] == 880_000.0


class TestRankCompanies:
    """The database-driven ranking path (CompanyCandidate rows)."""

    def _companies(self):
        return [
            _company("c1", "NEWCO", "New Co Ltd", "INE555D01555",
                     exchange="NSE", bse_code="543210"),
            _company("c2", "OTHERCO", "Other Co Ltd", "INE666E01666",
                     exchange="BSE", bse_code=None),
            _company("c3", "NOISIN", "No Isin Co Ltd", None, exchange="NSE"),
        ]

    def test_ranks_descending_from_501_with_db_fields(self):
        mcap = {
            "INE555D01555": (880_000.0, "bse_master"),
            "INE666E01666": (420_000.0, "bse_master"),
            "NOISIN": (90_000.0, "screener.in"),
        }
        rows, counts = rank_companies(self._companies(), mcap)
        assert [r.ticker for r in rows] == ["NEWCO", "OTHERCO", "NOISIN"]
        assert [r.proposed_rank for r in rows] == [501, 502, 503]
        assert rows[0].exchange == "NSE"            # DB value, not guessed
        assert rows[0].bse_scrip_code == "543210"   # DB bse_code wins
        assert rows[0].company_id == "c1"
        assert rows[0].current_universe_status == "not_in_nifty500"
        assert counts == {"ranked": 3, "no_market_cap_available": 0,
                          "duplicates_collapsed": 0}

    def test_master_scrip_code_enriches_rows_without_one(self):
        mcap = {"INE666E01666": (420_000.0, "bse_master")}
        rows, _ = rank_companies(
            self._companies(), mcap,
            bse_scrip_codes={"INE666E01666": "777888"},
        )
        assert [r.ticker for r in rows] == ["OTHERCO"]
        assert rows[0].bse_scrip_code == "777888"

    def test_missing_figure_is_counted_not_ranked(self):
        mcap = {"INE555D01555": (880_000.0, "bse_master")}
        rows, counts = rank_companies(self._companies(), mcap)
        assert len(rows) == 1
        assert counts["no_market_cap_available"] == 2

    def test_limit_truncates_at_rank(self):
        mcap = {
            "INE555D01555": (880_000.0, "bse_master"),
            "INE666E01666": (420_000.0, "bse_master"),
        }
        rows, counts = rank_companies(self._companies(), mcap, limit=1)
        assert rows[0].proposed_rank == 501
        assert counts == {"ranked": 1, "no_market_cap_available": 1,
                          "duplicates_collapsed": 0}


class TestDeduplicateSameSecurity:
    """The production bug: an NSE row and a BSE row for the same ISIN must
    count as ONE company, or one security occupies two of the 200 slots and
    the proposed ranks 501-700 carry the same ISIN twice."""

    def test_nse_and_bse_rows_same_isin_collapse_to_one(self):
        """Two DB rows, one security (same ISIN, different exchange)."""
        nse = _company("c-nse", "SOMECO", "Some Co Ltd", "INE123A01001",
                       exchange="NSE", bse_code=None)
        bse = _company("c-bse", "SOMECO", "Some Co Ltd", "INE123A01001",
                       exchange="BSE", bse_code="500123")
        mcap = {"INE123A01001": (500_000.0, "bse_master")}
        rows, counts = rank_companies([nse, bse], mcap)
        # One company, one rank — not two.
        assert len(rows) == 1
        assert counts["duplicates_collapsed"] == 1
        assert counts["ranked"] == 1
        # The representative keeps the correct identity: ISIN, ticker, the
        # unified exchange, and the BSE scrip code that lived on the BSE row.
        assert rows[0].isin == "INE123A01001"
        assert rows[0].ticker == "SOMECO"
        assert rows[0].exchange == "NSE/BSE"
        assert rows[0].bse_scrip_code == "500123"
        assert rows[0].proposed_rank == 501

    def test_isin_is_the_primary_identity_not_ticker(self):
        """Same ISIN but different tickers across venues is still one
        security — ISIN wins over ticker."""
        a = _company("c1", "SOMECO", "Some Co Ltd", "INE123A01001",
                     exchange="NSE")
        b = _company("c2", "SOMECO.BSE", "Some Co Ltd", "INE123A01001",
                     exchange="BSE")
        mcap = {"INE123A01001": (500_000.0, "bse_master")}
        rows, counts = rank_companies([a, b], mcap)
        assert len(rows) == 1
        assert counts["duplicates_collapsed"] == 1

    def test_no_isin_falls_back_to_ticker(self):
        """Without an ISIN, two rows sharing a ticker collapse."""
        a = _company("c1", "TICKERCO", "Ticker Co Ltd", None, exchange="NSE")
        b = _company("c2", "TICKERCO", "Ticker Co Ltd", None, exchange="BSE")
        mcap = {"TICKERCO": (200_000.0, "screener.in")}
        rows, counts = rank_companies([a, b], mcap)
        assert len(rows) == 1
        assert counts["duplicates_collapsed"] == 1
        assert rows[0].isin is None
        assert rows[0].ticker == "TICKERCO"

    def test_distinct_isins_are_not_merged(self):
        """Two genuinely different securities must stay separate."""
        a = _company("c1", "ALPHA", "Alpha Ltd", "INE111A01111", exchange="NSE")
        b = _company("c2", "BETA", "Beta Ltd", "INE222B02222", exchange="BSE")
        mcap = {
            "INE111A01111": (300_000.0, "bse_master"),
            "INE222B02222": (100_000.0, "bse_master"),
        }
        rows, counts = rank_companies([a, b], mcap)
        assert len(rows) == 2
        assert counts["duplicates_collapsed"] == 0
        assert {r.isin for r in rows} == {"INE111A01111", "INE222B02222"}

    def test_200_rows_are_200_unique_identities(self):
        """The hard guarantee: 300 candidate rows containing 100 duplicated
        (NSE+BSE) pairs + 100 single rows = 200 unique securities. The output
        must be exactly 200 rows with 200 unique ISINs, ranks 501-700."""
        cands = []
        mcap = {}
        # 100 securities, each present twice (NSE + BSE), ISINs A0001..A0100
        for i in range(1, 101):
            isin = f"INE{i:08d}X"
            mcap[isin] = (10_000.0 * i, "bse_master")  # descending distinct
            cands.append(_company(f"nse-{i}", f"CO{i:03d}",
                                  f"Co {i} Ltd", isin, exchange="NSE",
                                  bse_code=f"{500000 + i}"))
            cands.append(_company(f"bse-{i}", f"CO{i:03d}",
                                  f"Co {i} Ltd", isin, exchange="BSE"))
        # 100 single-row securities, ISINs B0001..B0100
        for i in range(1, 101):
            isin = f"INEB{i:07d}Y"
            mcap[isin] = (5_000.0 * i, "bse_master")
            cands.append(_company(f"single-{i}", f"DS{i:03d}",
                                  f"DS {i} Ltd", isin, exchange="NSE"))
        assert len(cands) == 300  # 200 unique securities in 300 rows

        rows, counts = rank_companies(cands, mcap, limit=200)
        # Exactly 200 rows.
        assert len(rows) == 200
        # 200 unique ISINs — no security appears twice.
        assert len({r.isin for r in rows}) == 200
        # Ranks are exactly 501..700, contiguous, no gaps.
        assert [r.proposed_rank for r in rows] == list(range(501, 701))
        # The 100 duplicated securities collapsed away.
        assert counts["duplicates_collapsed"] == 100
        # Market cap is still strictly descending.
        caps = [r.market_cap_inr_crore for r in rows]
        assert caps == sorted(caps, reverse=True)


class TestBseIdentityResolution:
    """The production root cause: the NSE row has a NULL ISIN and only the
    BSE row carries the real one. In production the BSE row's *ticker column*
    holds the numeric scrip code (544780), not a symbol, and the NSE row's
    ticker holds the symbol (VAML) — so ISIN->ticker->name dedupe left the
    pair separate (duplicates_collapsed=0). The fix reconciles the pair via
    (1) the BSE master's explicit code/symbol -> ISIN maps and/or (2) an exact
    legal-suffix-normalised company-name match."""

    # (nse_ticker, bse_code, isin, nse_name, bse_name) — confirmed AWS
    # dual-listed examples. The NSE name and BSE name differ by legal suffix
    # ("... Limited" vs "... Ltd") and sometimes a leading "The", exactly as
    # the two exchanges record them.
    PRODUCTION = [
        ("VAML", "544780", "INE1CDF01017",
         "Vedanta Aluminium Metal Limited", "Vedanta Aluminium Metal Ltd"),
        ("SBIFUNDS", "544829", "INE640G01020",
         "SBI Funds Management Limited", "SBI Funds Management Ltd"),
        ("MANIPALHOS", "544847", "INE459N01021",
         "Manipal Health Enterprises Limited", "Manipal Health Enterprises Ltd"),
        ("INDOMIM", "544837", "INE084101034",
         "INDO-MIM Limited", "INDO-MIM Ltd"),
        ("CUPID", "530843", "INE509F01029",
         "Cupid Limited", "Cupid Ltd"),
        ("VIYASH", "512529", "INE807F01027",
         "Viyash Scientific Limited", "Viyash Scientific Ltd"),
        ("EDELWEISS", "532922", "INE532F01054",
         "Edelweiss Financial Services Limited", "Edelweiss Financial Services Ltd"),
        ("SOUTHBANK", "532218", "INE683A01023",
         "The South Indian Bank Limited", "South Indian Bank Ltd"),
        ("METROPOLIS", "542650", "INE112L01020",
         "Metropolis Healthcare Limited", "Metropolis Healthcare Ltd"),
    ]

    def _master(self, symbol_is_nse_symbol: bool):
        """BSE master rows. ``symbol_is_nse_symbol`` controls what the master's
        SYMBOL/scrip_id field holds: the NSE symbol (VAML) or the BSE code
        (544780). The production master did NOT connect the NSE row via this
        field (0 collapsed), so the main dedupe test uses the code form to
        prove name reconciliation is the deciding mechanism."""
        scrips = [BseScrip("500325", "RELIANCE", "Reliance Industries Ltd",
                           "INE002A01018", None, None, 1.78e14)]
        for i, (nse_ticker, code, isin, _nse_name, bse_name) in \
                enumerate(self.PRODUCTION):
            symbol = nse_ticker if symbol_is_nse_symbol else code
            scrips.append(BseScrip(code, symbol, bse_name, isin, None, None,
                                   float(1e12 + 1e9 * i)))
        return scrips

    def _db_rows(self):
        """The two ``companies`` rows per security, exactly the AWS shape: the
        NSE row has ticker=symbol and a NULL ISIN; the BSE row has
        ticker=scrip-code and the real ISIN."""
        rows = []
        for nse_ticker, code, isin, nse_name, bse_name in self.PRODUCTION:
            rows.append(CompanyCandidate(
                company_id=f"nse-{nse_ticker}", ticker=nse_ticker,
                name=nse_name, isin=None, exchange="NSE",
                listing_status="active", currency="INR", bse_code=None))
            rows.append(CompanyCandidate(
                company_id=f"bse-{code}", ticker=code, name=bse_name,
                isin=isin, exchange="BSE", listing_status="active",
                currency="INR", bse_code=code))
        return rows

    # -- explicit trusted mapping (BSE master code/symbol -> ISIN) ---------

    def test_identity_maps_built_from_master(self):
        sym2isin, code2isin = build_bse_identity_maps(self._master(True))
        assert sym2isin["VAML"] == "INE1CDF01017"
        assert sym2isin["METROPOLIS"] == "INE112L01020"
        assert code2isin["544780"] == "INE1CDF01017"
        assert code2isin["542650"] == "INE112L01020"

    def test_null_isin_nse_row_resolves_via_symbol_map(self):
        """When the master's symbol field IS the NSE symbol, the NULL-ISIN NSE
        row resolves through the explicit symbol -> ISIN map (Stage 1)."""
        sym2isin, code2isin = build_bse_identity_maps(self._master(True))
        nse = self._db_rows()[0]  # VAML NSE row, isin=None
        assert nse.isin is None
        assert resolve_identity(nse, sym2isin, code2isin) == "INE1CDF01017"

    def test_row_with_scrip_code_resolves_via_code_map(self):
        sym2isin, code2isin = build_bse_identity_maps(self._master(False))
        row = CompanyCandidate(company_id="x", ticker="544780",
                               name="Vedanta Aluminium Metal Ltd", isin=None,
                               exchange="BSE", currency="INR", bse_code="544780")
        assert resolve_identity(row, sym2isin, code2isin) == "INE1CDF01017"

    def test_physical_isin_wins_over_master(self):
        sym2isin, code2isin = build_bse_identity_maps(self._master(False))
        bse = self._db_rows()[1]  # VAML BSE row, isin=INE1CDF01017
        assert resolve_identity(bse, sym2isin, code2isin) == "INE1CDF01017"

    # -- name reconciliation (the production deciding mechanism) -----------

    def test_master_symbol_code_does_not_connect_nse_row(self):
        """With the master's symbol field holding the BSE code, the NSE row
        (ticker=VAML, no bse_code) does NOT resolve via Stage-1 maps — this is
        the production case where name reconciliation must decide."""
        sym2isin, code2isin = build_bse_identity_maps(self._master(False))
        nse = self._db_rows()[0]  # ticker=VAML, isin=None, bse_code=None
        assert resolve_identity(nse, sym2isin, code2isin) is None

    def test_name_reconciliation_collapses_nse_null_isin_plus_bse_isin(self):
        """VAML: NSE (NULL ISIN, name '... Limited') + BSE (ISIN, name
        '... Ltd') collapse to ONE candidate via exact legal-normalised name,
        even though the master's symbol field does not connect them."""
        sym2isin, code2isin = build_bse_identity_maps(self._master(False))
        nse = self._db_rows()[0]
        bse = self._db_rows()[1]
        [rep] = dedupe_candidates([nse, bse], sym2isin, code2isin)
        assert rep.isin == "INE1CDF01017"
        assert rep.ticker == "VAML"          # the NSE symbol, not the code
        assert rep.exchange == "NSE/BSE"
        assert rep.bse_code == "544780"      # recovered from the BSE row

    def test_every_production_example_occupies_exactly_one_rank(self):
        """VAML/544780, SBIFUNDS/544829, MANIPALHOS/544847, INDOMIM/544837,
        CUPID/530843, VIYASH/512529, EDELWEISS/532922, SOUTHBANK/532218 and
        METROPOLIS/542650 each appear EXACTLY ONCE — no NSE/BSE pair appears
        twice (the AWS verification requirement). Uses the production master
        (symbol field = code) so name reconciliation is the deciding path."""
        sym2isin, code2isin = build_bse_identity_maps(self._master(False))
        bse_map, _unit = build_bse_mktcap_map(self._master(False))
        mcap = {isin: (amt, "bse_master") for isin, (amt, _c) in bse_map.items()}
        bse_codes = {isin: code for isin, (_amt, code) in bse_map.items()}

        rows, counts = rank_companies(
            self._db_rows(), mcap, limit=200,
            bse_scrip_codes=bse_codes, symbol_to_isin=sym2isin,
            scrip_code_to_isin=code2isin)

        # 18 rows (9 NSE + 9 BSE) collapse to 9 unique securities.
        assert len(rows) == 9
        assert counts["duplicates_collapsed"] == 9
        assert counts["ranked"] == 9

        # Each production security occupies exactly one rank, by ISIN and by
        # the NSE ticker (the pair's two rows must not both appear).
        for nse_ticker, _code, isin, _n, _b in self.PRODUCTION:
            assert len([r for r in rows if r.isin == isin]) == 1, isin
            assert [r.ticker for r in rows].count(nse_ticker) == 1, nse_ticker

        # Ranks contiguous from 501; market cap descending.
        assert [r.proposed_rank for r in rows] == list(range(501, 501 + 9))
        caps = [r.market_cap_inr_crore for r in rows]
        assert caps == sorted(caps, reverse=True)
        # Every representative carries the unified exchange, the NSE symbol as
        # ticker, the BSE scrip code, and the security-level ISIN.
        for r in rows:
            assert r.exchange == "NSE/BSE"
            assert r.bse_scrip_code
            assert r.isin and r.isin.startswith("INE")
            assert not r.ticker.isdigit()  # ticker is a symbol, not a code

    def test_name_reconciliation_handles_the_article_and_suffix_variants(self):
        """'The South Indian Bank Limited' (NSE) == 'South Indian Bank Ltd'
        (BSE): leading 'The' + 'Limited'/'Ltd' all normalise away."""
        from app.services.universe.next200 import normalize_legal_name
        nse = self._db_rows()[14]   # SOUTHBANK NSE row: "The South Indian Bank Limited"
        bse = self._db_rows()[15]   # SOUTHBANK BSE row: "South Indian Bank Ltd"
        assert nse.name == "The South Indian Bank Limited"
        assert bse.name == "South Indian Bank Ltd"
        assert normalize_legal_name(nse.name) == normalize_legal_name(bse.name)
        assert normalize_legal_name(nse.name) == "SOUTHINDIANBANK"

    # -- safety: no false merges ------------------------------------------

    def test_different_securities_not_merged_on_similar_names(self):
        """Requirement: similar (but not legal-suffix-identical) names with
        distinct ISINs must NOT merge. 'Vedanta' != 'Vedanta Aluminium
        Metal' after normalisation, so they stay separate."""
        a = CompanyCandidate(company_id="a", ticker="VEDAL",
                             name="Vedanta Limited", isin="INEAAA01001",
                             exchange="NSE", currency="INR")
        b = CompanyCandidate(company_id="b", ticker="VEDAN",
                             name="Vedanta Aluminium Metal Limited",
                             isin="INEBBB01002", exchange="BSE", currency="INR",
                             bse_code="999999")
        rows, counts = rank_companies(
            [a, b],
            {"INEAAA01001": (100.0, "bse_master"),
             "INEBBB01002": (90.0, "bse_master")},
        )
        assert len(rows) == 2
        assert counts["duplicates_collapsed"] == 0
        assert {r.isin for r in rows} == {"INEAAA01001", "INEBBB01002"}

    def test_single_nse_null_isin_row_stays_null_isin(self):
        """A lone NSE NULL-ISIN row with no ISIN-bearing same-name counterpart
        must remain a physical NULL-ISIN row (no ISIN invented)."""
        lone = CompanyCandidate(company_id="lone", ticker="LONECO",
                                name="Lone Company Limited", isin=None,
                                exchange="NSE", currency="INR", bse_code=None)
        [rep] = dedupe_candidates([lone])
        assert rep.isin is None
        assert rep.ticker == "LONECO"

    def test_ambiguous_name_not_merged(self):
        """If a name maps to two distinct ISINs (a data collision), a
        NULL-ISIN row with that name must NOT be merged on the ambiguous
        name."""
        iso = CompanyCandidate(company_id="x", ticker="X1", name="Dual Name Co",
                               isin=None, exchange="NSE", currency="INR")
        a = CompanyCandidate(company_id="a", ticker="A1", name="Dual Name Co",
                             isin="INEAAA01001", exchange="BSE", currency="INR")
        b = CompanyCandidate(company_id="b", ticker="B1", name="Dual Name Co",
                             isin="INEBBB01002", exchange="BSE", currency="INR")
        # The ISIN-bearing rows keep their own ISINs; the NULL-ISIN row's name
        # maps to two ISINs, so it must not be reconciled to either.
        rows = dedupe_candidates([iso, a, b])
        isins = sorted(r.isin for r in rows if r.isin)
        assert isins == ["INEAAA01001", "INEBBB01002"]
        null_rows = [r for r in rows if r.isin is None]
        assert len(null_rows) == 1  # the lone NULL-ISIN row, unreconciled

    def test_null_isin_row_still_ranked_via_resolved_bse_mcap(self):
        """A NULL-ISIN NSE row still finds its BSE market cap through the
        security-level (name-reconciled) ISIN — matching stays ISIN-first."""
        sym2isin, code2isin = build_bse_identity_maps(self._master(False))
        bse_map, _unit = build_bse_mktcap_map(self._master(False))
        mcap = {isin: (amt, "bse_master") for isin, (amt, _c) in bse_map.items()}
        # Both VAML rows (NSE NULL-ISIN + BSE ISIN) so the name reconciliation
        # can resolve the NSE row to the BSE ISIN and match its market cap.
        nse, bse = self._db_rows()[0], self._db_rows()[1]
        rows, counts = rank_companies(
            [nse, bse], mcap, symbol_to_isin=sym2isin,
            scrip_code_to_isin=code2isin)
        assert counts["ranked"] == 1
        assert rows[0].market_cap_inr_crore == bse_map["INE1CDF01017"][0]
        assert rows[0].mcap_source == "bse_master"
        # The merged representative carries the security-level ISIN.
        assert rows[0].isin == "INE1CDF01017"


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
            "company_id": None,
            "ticker": "NEWCO",
            "company_name": "New Co Ltd",
            "isin": "INE555D01555",
            "exchange": "BSE",
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
        assert lines[0].startswith("proposed_rank,company_id,ticker,company_name,isin")
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
