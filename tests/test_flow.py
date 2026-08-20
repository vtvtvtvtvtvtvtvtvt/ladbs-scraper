"""End-to-end tests of the scraper's browser flow against a local mock LADBS.

These need Chromium (playwright install chromium) but no internet access.
Run: python -m pytest tests/test_flow.py -q
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import scraper as scraper_mod
from scraper import LADBSScraper
from mock_ladbs import STATE, MockLADBS, IDIS


def run_scrape(kind, value):
    """Point the scraper at the mock site and run one scrape."""
    with MockLADBS() as mock:
        old_base, old_main = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
        scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
        scraper_mod.MAIN_URL = mock.base
        try:
            s = LADBSScraper()
            if kind == "ain":
                return asyncio.run(s.scrape_by_ain(value))
            return asyncio.run(s.scrape(value))
        finally:
            scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old_base, old_main


@pytest.fixture(scope="module")
def ain_result():
    return run_scrape("ain", "5443-016-018")


@pytest.fixture(scope="module")
def address_result():
    return run_scrape("address", "2100 Cypress Ave, Los Angeles, CA 90065")


class TestAinFlow:
    def test_finds_all_unique_records(self, ain_result):
        # parcel 1: records 100,101 (page 1) + 102 (page 2); parcel 2: 103 + dup 100
        assert ain_result["total_records"] == 4
        ids = sorted(r["record_id"] for r in ain_result["records"])
        assert ids == ["100", "101", "102", "103"]

    def test_walks_both_parcel_checkboxes(self, ain_result):
        assert ain_result["diagnostics"]["checkboxes_found"] == 2

    def test_follows_pagination(self, ain_result):
        # Record 102 only exists on result page 2.
        assert any(r["record_id"] == "102" for r in ain_result["records"])

    def test_never_replays_a_stale_viewstate(self, ain_result):
        # The old httpx-replay flow tripped this counter; the browser flow must not.
        assert STATE.stale_rejections == 0

    def test_detail_pages_are_fetched_in_the_same_session(self, ain_result):
        for rec in ain_result["records"]:
            assert "detail_error" not in rec, rec.get("detail_error")
            assert rec.get("status") == "Finaled"
            assert rec.get("applicant") == "ACME Builders"

    def test_attachments_collected(self, ain_result):
        # Records 100 and 102 have digital images.
        assert len(ain_result["attachments"]) == 2

    def test_summary_and_echo(self, ain_result):
        assert ain_result["ain"] == "5443-016-018"
        assert "Found 4 record(s)" in ain_result["summary"]
        assert "Building Permit: 2" in ain_result["summary"]


class TestAddressFlow:
    def test_address_search_returns_records(self, address_result):
        # This is the path that previously posted the selection form back to
        # ParcelSearch.aspx with the wrong button and always returned nothing.
        assert address_result["total_records"] == 4

    def test_echoes_address_and_parses_it(self, address_result):
        assert address_result["address"] == "2100 Cypress Ave, Los Angeles, CA 90065"
        assert address_result["diagnostics"]["parsed_address"] == {
            "number": "2100", "street": "CYPRESS", "direction": "",
        }


class TestNoMatch:
    def test_unknown_ain_returns_empty_with_diagnostics(self):
        result = run_scrape("ain", "1111-222-333")
        assert result["total_records"] == 0
        assert result["records"] == []
        assert "No records found" in result["summary"]
        assert result["diagnostics"]["warnings"], "a miss must explain itself"

    def test_unknown_address_returns_empty(self):
        result = run_scrape("address", "9999 Nowhere St, Los Angeles")
        assert result["total_records"] == 0
        assert result["diagnostics"]["checkboxes_found"] == 0


class TestCrmInputs:
    """The exact values found in the CRM's property_research table."""

    def test_bare_ain_in_the_address_field_still_searches(self):
        # Row 14 sent "5467018015" as the address; the AIN search must kick in.
        result = run_scrape("address", "5443016018")
        assert result["total_records"] == 4
        assert result["address"] == "5443016018"
        assert result["ain"] == "5443016018"
        assert "AIN" in result["diagnostics"]["routed"]

    def test_out_of_area_address_explains_itself(self):
        result = run_scrape("address", "1975 Lincoln Ave, Pasadena, CA, USA")
        assert result["total_records"] == 0
        assert result["diagnostics"]["outside_jurisdiction"] == "Pasadena"
        assert "outside the City of Los Angeles" in result["summary"]


class TestTimeBudget:
    """A scrape must always answer — a caller that times out gets a 502."""

    def test_tiny_budget_still_returns_a_response(self):
        with MockLADBS() as mock:
            old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
            scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
            scraper_mod.MAIN_URL = mock.base
            try:
                s = LADBSScraper(budget_seconds=0.001)
                result = asyncio.run(s.scrape_by_ain("5443-016-018"))
            finally:
                scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old

        assert result["diagnostics"]["truncated"] is True
        assert isinstance(result["total_records"], int)
        assert "elapsed_seconds" in result["diagnostics"]

    def test_normal_budget_is_not_truncated(self, ain_result):
        assert ain_result["diagnostics"]["truncated"] is False
        assert ain_result["diagnostics"]["elapsed_seconds"] > 0

    def test_budget_reads_from_env(self, monkeypatch):
        import importlib
        monkeypatch.setenv("SCRAPE_TIMEOUT_SECONDS", "42")
        importlib.reload(scraper_mod)
        try:
            assert scraper_mod.LADBSScraper().budget_seconds == 42.0
        finally:
            monkeypatch.delenv("SCRAPE_TIMEOUT_SECONDS")
            importlib.reload(scraper_mod)
