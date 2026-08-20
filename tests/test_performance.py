"""Tests for the things that made real scrapes time out or fail silently."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import scraper as scraper_mod
from scraper import LADBSScraper
from mock_ladbs import MockLADBS, IDIS

BULK_AIN = "9999-999-999"      # 60 records
BLOCKED_AIN = "4030-030-030"   # upstream refuses


def scrape(kind, value, **kwargs):
    with MockLADBS() as mock:
        old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
        scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
        scraper_mod.MAIN_URL = mock.base
        try:
            s = LADBSScraper(budget_seconds=kwargs.pop("budget_seconds", 150))
            started = time.monotonic()
            if kind == "ain":
                result = asyncio.run(s.scrape_by_ain(value, **kwargs))
            else:
                result = asyncio.run(s.scrape(value, **kwargs))
            result["_wall_seconds"] = time.monotonic() - started
            return result
        finally:
            scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old


@pytest.fixture(scope="module")
def bulk_with_details():
    return scrape("ain", BULK_AIN)


@pytest.fixture(scope="module")
def bulk_without_details():
    return scrape("ain", BULK_AIN, include_details=False)


class TestHighHistoryParcel:
    def test_all_records_returned(self, bulk_with_details):
        assert bulk_with_details["total_records"] == 60

    def test_every_record_gets_its_detail(self, bulk_with_details):
        # Concurrency must not drop or cross-contaminate records.
        assert all("detail_error" not in r for r in bulk_with_details["records"])
        assert all(r.get("status") == "Finaled" for r in bulk_with_details["records"])
        assert bulk_with_details["diagnostics"]["detail_failures"] == 0

    def test_records_stay_distinct(self, bulk_with_details):
        ids = [r["record_id"] for r in bulk_with_details["records"]]
        assert len(ids) == len(set(ids))

    def test_status_is_ok_and_not_truncated(self, bulk_with_details):
        assert bulk_with_details["status"] == "ok"
        assert bulk_with_details["diagnostics"]["truncated"] is False


class TestSkippingDetails:
    def test_same_records_without_details(self, bulk_without_details):
        assert bulk_without_details["total_records"] == 60
        assert bulk_without_details["diagnostics"]["details_fetched"] is False

    def test_grid_fields_survive(self, bulk_without_details):
        rec = bulk_without_details["records"][0]
        for field in ("doc_type", "sub_type", "doc_date", "doc_number"):
            assert rec[field], f"{field} missing"

    def test_attachments_still_collected(self, bulk_without_details):
        assert len(bulk_without_details["attachments"]) == 20  # every third record

    def test_is_much_faster(self, bulk_with_details, bulk_without_details):
        assert bulk_without_details["_wall_seconds"] < bulk_with_details["_wall_seconds"]


class TestUpstreamBlock:
    """A refusal must not look like a permit-free property."""

    @pytest.fixture(scope="class")
    @classmethod
    def blocked(cls):
        return scrape("ain", BLOCKED_AIN)

    def test_reported_as_blocked_not_empty(self, blocked):
        assert blocked["status"] == "blocked"
        assert blocked["total_records"] == 0

    def test_summary_says_it_is_a_failure(self, blocked):
        assert "failure, not an empty parcel" in blocked["summary"]

    def test_genuine_empty_is_not_called_blocked(self):
        result = scrape("ain", "1111-222-333")
        assert result["status"] == "no_records"
        assert "failure" not in result["summary"]

    def test_successful_scrape_is_ok(self):
        assert scrape("ain", "5443-016-018")["status"] == "ok"
