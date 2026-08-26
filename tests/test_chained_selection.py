"""An address row can lead to a second selection page, not straight to results.

Live LADBS resolves a search to a parcel and then offers that parcel's
identifiers — assessor number, address range, legal description — as another
round of checkboxes. Every level must be walked, or whole identifier sets
silently vanish.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import scraper as scraper_mod
from scraper import LADBSScraper
from mock_ladbs import MockLADBS, IDIS

scraper_mod.SETTLE_SECONDS = 0.05

TWO_LEVEL_AIN = "7100-100-100"   # 2 address rows x (AS: 2 + AR: 3) = 10 records


@pytest.fixture(scope="module")
def result():
    with MockLADBS() as mock:
        old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
        scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
        scraper_mod.MAIN_URL = mock.base
        try:
            return asyncio.run(LADBSScraper().scrape_by_ain(
                TWO_LEVEL_AIN, include_details=False))
        finally:
            scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old


class TestChainedSelection:
    def test_every_identifier_of_every_row_is_collected(self, result):
        assert result["total_records"] == 10
        assert result["status"] == "ok"

    def test_sub_selections_are_reported(self, result):
        steps = result["diagnostics"]["strategy"]
        assert sum(1 for s in steps if s["step"] == "sub_selection") == 2

    def test_sub_rows_are_labelled_through_their_parent(self, result):
        rows = [s["row"] for s in result["diagnostics"]["strategy"]
                if s["step"] == "row"]
        assert any("7100 TWOSTEP AVE > AS" in r for r in rows)
        assert any("7100 N TWOSTEP AVE > AR" in r for r in rows)

    def test_records_are_distinct(self, result):
        ids = [r["record_id"] for r in result["records"]]
        assert len(ids) == len(set(ids)) == 10

    def test_selection_page_evidence_is_captured(self, result):
        page = result["diagnostics"]["selection_page"]
        assert len(page["checkboxes"]) == 2
        assert page["sample"]
