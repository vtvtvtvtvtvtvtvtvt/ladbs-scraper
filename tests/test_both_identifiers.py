"""An AIN and an address do not reach the same documents.

LADBS files documents against address records. A property can hold several
that differ only by direction — "234 MUSEUM DR" and "234 W MUSEUM DR". The
assessor (AIN) search reaches one of them; the address search reaches both.
A caller that supplies both identifiers must get the union.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import main
import scraper as scraper_mod
from scraper import LADBSScraper
from mock_ladbs import MockLADBS, IDIS

scraper_mod.SETTLE_SECONDS = 0.05

AIN = "5467-018-015"
ADDRESS = "234 Museum Drive, Los Angeles, CA, USA"


def call(payload):
    with MockLADBS() as mock:
        old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
        scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
        scraper_mod.MAIN_URL = mock.base
        try:
            return TestClient(main.app).post("/scrape", json=payload).json()
        finally:
            scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old


@pytest.fixture(scope="module")
def both():
    return call({"ain": AIN, "address": ADDRESS, "include_details": False})


class TestEachIdentifierAlone:
    def test_ain_alone_reaches_one_address_row(self):
        result = call({"ain": AIN, "include_details": False})
        assert result["total_records"] == 6

    def test_address_alone_reaches_both_rows(self):
        result = call({"address": ADDRESS, "include_details": False})
        assert result["total_records"] == 31


class TestBothTogether:
    def test_supplying_both_is_not_the_same_as_the_ain_alone(self, both):
        # The bug: an address alongside an AIN was silently discarded, so a
        # caller sending both got only the assessor parcel's documents.
        assert both["total_records"] == 31

    def test_both_searches_actually_ran(self, both):
        kinds = [s["type"] for s in both["diagnostics"]["searches"]]
        assert kinds == ["ain", "address"]

    def test_each_search_reports_its_contribution(self, both):
        searches = {s["type"]: s for s in both["diagnostics"]["searches"]}
        assert searches["ain"]["records"] == 6
        assert searches["address"]["records"] == 31

    def test_address_rows_are_named_per_search(self, both):
        searches = {s["type"]: s for s in both["diagnostics"]["searches"]}
        assert len(searches["ain"]["address_rows"]) <= 1
        assert len(searches["address"]["address_rows"]) == 2

    def test_overlapping_records_are_deduped(self, both):
        ids = [r["record_id"] for r in both["records"]]
        assert len(ids) == len(set(ids)) == 31

    def test_both_identifiers_are_echoed(self, both):
        assert both["ain"] == AIN
        assert both["address"] == ADDRESS


class TestStillWorksWithOneIdentifier:
    def test_ain_in_the_address_field_does_not_double_search(self):
        result = call({"address": "5467018015", "include_details": False})
        assert [s["type"] for s in result["diagnostics"]["searches"]] == ["ain"]

    def test_same_value_in_both_fields_searches_once(self):
        result = call({"ain": AIN, "address": "5467-018-015",
                       "include_details": False})
        assert [s["type"] for s in result["diagnostics"]["searches"]] == ["ain"]

    def test_neither_identifier_is_a_400(self):
        with MockLADBS():
            assert TestClient(main.app).post("/scrape", json={}).status_code == 400

    def test_an_unparseable_address_does_not_lose_the_ain_results(self):
        result = call({"ain": AIN, "address": "Museum Drive",
                       "include_details": False})
        assert result["total_records"] == 6
        assert any("could not parse address" in w
                   for w in result["diagnostics"]["warnings"])


class TestPerRowBreakdownSurvivesBothLegs:
    """Each leg's per-row detail must reach the caller, not be overwritten."""

    def test_each_leg_carries_its_own_steps(self, both):
        for search in both["diagnostics"]["searches"]:
            assert "steps" in search, f"{search['type']} leg lost its breakdown"

    def test_the_address_leg_shows_what_each_row_gave(self, both):
        address = next(s for s in both["diagnostics"]["searches"]
                       if s["type"] == "address")
        rows = [st for st in address["steps"] if st["step"] == "row"]
        assert rows, "no per-row breakdown for the address leg"
        assert all("records" in r and "row" in r for r in rows)

    def test_row_counts_are_attributable(self, both):
        address = next(s for s in both["diagnostics"]["searches"]
                       if s["type"] == "address")
        named = {st["row"] for st in address["steps"] if st["step"] == "row"}
        assert any("W MUSEUM" in n for n in named)


class TestBrowserGetEndpoint:
    """GET /scrape exists so a live run can be inspected from a browser."""

    def _get(self, params):
        with MockLADBS() as mock:
            old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
            scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
            scraper_mod.MAIN_URL = mock.base
            try:
                return TestClient(main.app).get("/scrape", params=params)
            finally:
                scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old

    def test_summary_carries_the_diagnostic_fields(self):
        body = self._get({"address": ADDRESS, "include_details": "false"}).json()
        assert body["status"] == "ok"
        assert body["total_records"] == 31
        assert body["address_rows_offered"] == ["234 MUSEUM DR", "234 W MUSEUM DR"]
        assert body["searches"][0]["per_row"], "per-row breakdown missing"
        assert "service_version" in body

    def test_full_format_returns_the_whole_response(self):
        body = self._get({"ain": AIN, "include_details": "false",
                          "format": "full"}).json()
        assert "records" in body and "diagnostics" in body

    def test_no_identifier_is_still_a_400(self):
        assert self._get({}).status_code == 400
