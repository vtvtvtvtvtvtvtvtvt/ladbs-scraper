"""The site's own script fast-forwards past the address-choice screen.

ParcelSearch runs CheckResult() on load, which pre-answers the "1st
intermediate screen" with the primary (W) address and submits — so a scraper
reading the page after load never sees the other address rows, while a person
does. A second pass with that function neutered holds the screen still and
walks every row on it.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import scraper as scraper_mod
from scraper import LADBSScraper
from mock_ladbs import MockLADBS, IDIS

# The auto-advance is a real navigation race; give it time to fire.
scraper_mod.SETTLE_SECONDS = 0.4

ADDRESS = "5000 Frozen Ave, Los Angeles, CA"


def scrape(**kwargs):
    with MockLADBS() as mock:
        old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
        scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
        scraper_mod.MAIN_URL = mock.base
        try:
            return asyncio.run(LADBSScraper().scrape_all(
                address=ADDRESS, include_details=False, **kwargs))
        finally:
            scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old


@pytest.fixture(scope="module")
def result():
    return scrape()


class TestFrozenScreenRescuesSkippedRows:
    def test_all_rows_documents_are_collected(self, result):
        # Auto-advance alone reaches only the W row's 2 records; holding the
        # screen still reaches the plain row's 2 as well.
        assert result["total_records"] == 4

    def test_the_grid_leg_reports_its_contribution(self, result):
        grid = next(s for s in result["diagnostics"]["searches"]
                    if s["type"] == "address_grid")
        assert grid["records"] == 4
        assert any("5000 FROZEN AVE" in r for r in grid["address_rows"])
        assert any("5000 W FROZEN AVE" in r for r in grid["address_rows"])

    def test_the_normal_leg_saw_only_the_auto_selected_row(self, result):
        base = next(s for s in result["diagnostics"]["searches"]
                    if s["type"] == "address")
        # The script submitted before the scraper could look: straight to the
        # W row's results, no choice offered.
        assert base["records"] == 2
        assert base["address_rows"] == []

    def test_without_expansion_the_skip_wins(self):
        plain = scrape(expand_directions=False)
        assert plain["total_records"] == 2

    def test_no_duplicates(self, result):
        ids = [r["record_id"] for r in result["records"]]
        assert len(ids) == len(set(ids)) == 4
