"""The document-side address search: one free-text box, its own button.

The live form has a single Address$txtAddress field and btnSearchAddress —
none of the parcel side's separate number/street/direction inputs. It is the
flow that lists every matching address row, so an address the parcel search
cannot resolve is still reachable through it.
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

# Resolvable ONLY via the document-side search in the mock.
ADDRESS = "600 Docside Ave, Los Angeles, CA"


@pytest.fixture(scope="module")
def result():
    with MockLADBS() as mock:
        old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
        scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
        scraper_mod.MAIN_URL = mock.base
        try:
            return asyncio.run(LADBSScraper().scrape_all(
                address=ADDRESS, include_details=False))
        finally:
            scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old


class TestDocumentSideSearch:
    def test_reaches_addresses_the_parcel_search_cannot(self, result):
        # Parcel-side leg finds nothing for this street; the document-side
        # leg lists both rows and walks them: 2 + 3 records.
        assert result["total_records"] == 5

    def test_the_doc_leg_lists_both_address_rows(self, result):
        doc = next(s for s in result["diagnostics"]["searches"]
                   if s["type"] == "address_documents")
        assert doc["records"] == 5
        assert any("600 DOCSIDE AVE" == r for r in doc["address_rows"])
        assert any("600 W DOCSIDE AVE" == r for r in doc["address_rows"])

    def test_each_row_contributes(self, result):
        doc = next(s for s in result["diagnostics"]["searches"]
                   if s["type"] == "address_documents")
        counts = {st["row"]: st["records"] for st in doc["steps"]
                  if st["step"] == "row"}
        assert counts.get("600 DOCSIDE AVE") == 2
        assert counts.get("600 W DOCSIDE AVE") == 3

    def test_the_parcel_leg_found_nothing(self, result):
        base = next(s for s in result["diagnostics"]["searches"]
                    if s["type"] == "address")
        assert base["records"] == 0

    def test_records_are_distinct(self, result):
        ids = [r["record_id"] for r in result["records"]]
        assert len(ids) == len(set(ids)) == 5
