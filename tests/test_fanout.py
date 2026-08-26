"""The 24-parcel address case: one submit, not one search per parcel."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import scraper as scraper_mod
from scraper import LADBSScraper
from mock_ladbs import STATE, MockLADBS, IDIS

FAN_AIN = "7777-777-777"        # 24 parcels x 5 records = 120
FAN_ADDRESS = "6801 Fanout Blvd, Los Angeles, CA"


# The point of this file is the number of round trips, not the settle delays.
scraper_mod.SETTLE_SECONDS = 0.05


def scrape(value, by="ain", **kwargs):
    with MockLADBS() as mock:
        old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
        scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
        scraper_mod.MAIN_URL = mock.base
        try:
            s = LADBSScraper(**kwargs)
            started = time.monotonic()
            result = asyncio.run(
                s.scrape_by_ain(value, include_details=False) if by == "ain"
                else s.scrape(value, include_details=False))
            result["_wall_seconds"] = time.monotonic() - started
            result["_searches"] = STATE.searches
            return result
        finally:
            scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old


@pytest.fixture(scope="module")
def combined():
    return scrape(FAN_AIN, parcel_mode="all")


@pytest.fixture(scope="module")
def per_parcel():
    return scrape(FAN_AIN, parcel_mode="each")


class TestCombinedSelection:
    """parcel_mode="all": one submit — fast where the site honours it."""

    def test_returns_every_parcels_records(self, combined):
        assert combined["total_records"] == 120
        assert combined["status"] == "ok"

    def test_runs_one_search_not_one_per_parcel(self, combined):
        assert combined["_searches"] == 1
        steps = [s["step"] for s in combined["diagnostics"]["strategy"]]
        assert steps == ["combined"]
        assert combined["diagnostics"]["address_rows_checked"] == 24

    def test_pages_through_the_merged_result_set(self, combined):
        assert combined["diagnostics"]["result_pages"] >= 5   # 120 rows / 25

    def test_records_are_distinct(self, combined):
        ids = [r["record_id"] for r in combined["records"]]
        assert len(ids) == len(set(ids)) == 120


class TestAgainstPerParcelWalk:
    def test_per_parcel_runs_a_search_for_every_parcel(self, per_parcel):
        assert per_parcel["_searches"] == 24

    def test_both_modes_find_the_same_records(self, combined, per_parcel):
        assert ({r["record_id"] for r in combined["records"]}
                == {r["record_id"] for r in per_parcel["records"]})

    def test_combined_is_faster(self, combined, per_parcel):
        assert combined["_wall_seconds"] < per_parcel["_wall_seconds"]


class TestFanoutByAddress:
    def test_address_search_supports_combined_mode_too(self):
        result = scrape(FAN_ADDRESS, by="address", parcel_mode="all")
        assert result["total_records"] == 120
        assert result["_searches"] == 1


class TestBudgetIsNotHitAnymore:
    def test_fanout_completes_inside_a_modest_budget(self):
        result = scrape(FAN_AIN, budget_seconds=60)
        assert result["status"] == "ok"
        assert result["diagnostics"]["truncated"] is False
