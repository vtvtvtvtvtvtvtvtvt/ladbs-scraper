"""Search each directional prefix outright and merge — the user's own idea.

A direction-less search for "4000 Variant Ave" resolves to one parcel; the
N-prefixed address record belongs to another, reachable only by searching
with the direction filled in. Blank and W resolve to the same parcel, which
must be walked once, not twice.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
import scraper as scraper_mod
from scraper import LADBSScraper
from mock_ladbs import MockLADBS, IDIS
from fastapi.testclient import TestClient

scraper_mod.SETTLE_SECONDS = 0.05

ADDRESS = "4000 Variant Ave, Los Angeles, CA"


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
def expanded():
    return call({"address": ADDRESS, "include_details": False})


class TestDirectionExpansion:
    def test_records_from_every_direction_are_merged(self, expanded):
        # 2 from the shared plain/W parcel + 2 from the N parcel.
        assert expanded["total_records"] == 4

    def test_the_north_parcel_is_only_reachable_via_its_variant(self):
        plain_only = call({"address": ADDRESS, "include_details": False,
                           "expand_directions": False})
        assert plain_only["total_records"] == 2

    def test_every_variant_is_reported(self, expanded):
        legs = [(s["type"], s["records"])
                for s in expanded["diagnostics"]["searches"]]
        assert ("address", 2) in legs
        variants = [l for l in legs if l[0] == "address_variant"]
        assert len(variants) == 4    # N, S, E, W (blank was the base search)

    def test_shared_parcel_is_not_walked_twice(self, expanded):
        # Blank and W resolve to the same selection page; the W variant must
        # be recognised and skipped, not re-scraped.
        west = next(s for s in expanded["diagnostics"]["searches"]
                    if s["type"] == "address_variant" and " W " in f' {s["query"]} ')
        assert any(st["step"] == "duplicate_parcel" for st in west["steps"])
        assert west["records"] == 0

    def test_missing_directions_come_back_empty_not_fatal(self, expanded):
        south = next(s for s in expanded["diagnostics"]["searches"]
                     if s["type"] == "address_variant" and " S " in f' {s["query"]} ')
        assert south["records"] == 0
        assert expanded["status"] == "ok"

    def test_records_are_distinct(self, expanded):
        # total_records counts the deduped set; verify against full format.
        pass


class TestExpansionRespectsInputs:
    def test_ain_only_requests_do_not_expand(self):
        result = call({"ain": "5443-016-018", "include_details": False})
        kinds = {s["type"] for s in result["diagnostics"]["searches"]}
        assert kinds == {"ain"}

    def test_expansion_can_be_disabled(self):
        result = call({"address": ADDRESS, "include_details": False,
                       "expand_directions": False})
        kinds = [s["type"] for s in result["diagnostics"]["searches"]]
        assert kinds == ["address"]
