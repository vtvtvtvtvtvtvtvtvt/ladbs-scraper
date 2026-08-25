"""Most image ids are not in the results grid — they are on each record's page.

A real pull of 234 Museum Drive returned 51 rows showing an image icon and 4
carrying a document id. The other 47 rows show the icon but hold no id: it
lives on the record's own Report.aspx page, which the scraper was asking to
render with Image=Hidden and then parsing for bold labels only.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import scraper as scraper_mod
from scraper import LADBSScraper, detail_image_guids
from mock_ladbs import MockLADBS, IDIS

scraper_mod.SETTLE_SECONDS = 0.05

ICON_AIN = "9000-000-000"   # 8 records, icons in the grid, ids on the detail pages


def scrape(**kwargs):
    with MockLADBS() as mock:
        old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
        scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
        scraper_mod.MAIN_URL = mock.base
        try:
            return asyncio.run(
                LADBSScraper().scrape_by_ain(ICON_AIN, **kwargs))
        finally:
            scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old


@pytest.fixture(scope="module")
def with_details():
    return scrape(include_details=True)


@pytest.fixture(scope="module")
def without_details():
    return scrape(include_details=False)


class TestIdsComeFromTheDetailPage:
    def test_every_icon_row_ends_up_with_an_image(self, with_details):
        diag = with_details["diagnostics"]
        assert diag["rows_showing_image_icon"] == 8
        assert diag["records_with_image"] == 8

    def test_the_grid_supplied_none_of_them(self, with_details):
        assert with_details["diagnostics"]["image_ids_from_grid"] == 0
        assert with_details["diagnostics"]["image_ids_from_detail"] == 8

    def test_each_record_gets_a_usable_link(self, with_details):
        for rec in with_details["records"]:
            assert rec["has_digital_image"] is True
            assert "ImageMain.aspx?DocIds=" in rec["digital_image_url"]
            assert rec["image_source"] == "detail"

    def test_attachments_are_populated(self, with_details):
        assert len(with_details["attachments"]) == 8

    def test_ids_are_distinct_per_record(self, with_details):
        urls = {r["digital_image_url"] for r in with_details["records"]}
        assert len(urls) == 8


class TestSkippingDetailsCostsTheImages:
    def test_without_details_the_icons_yield_nothing(self, without_details):
        diag = without_details["diagnostics"]
        assert diag["rows_showing_image_icon"] == 8
        assert diag["records_with_image"] == 0

    def test_and_it_says_so(self, without_details):
        assert any("include_details=true" in w
                   for w in without_details["diagnostics"]["warnings"])

    def test_records_still_come_back(self, without_details):
        assert without_details["total_records"] == 8


class TestGuidExtractionFromDetailPages:
    def test_prefers_the_image_viewer_link(self):
        html = """<html><body>
            <a href="Other.aspx?x={11111111-1111-1111-1111-111111111111}">x</a>
            <a href="ImageMain.aspx?DocIds={22222222-2222-2222-2222-222222222222}">img</a>
        </body></html>"""
        assert detail_image_guids(html) == ["{22222222-2222-2222-2222-222222222222}"]

    def test_accepts_a_pdf_viewer_link(self):
        html = ('<a href="StPdfViewer.aspx?Id=%7B33333333-3333-3333-3333-333333333333%7D">'
                'pdf</a>')
        # URL-encoded braces are not ids; the raw fallback finds nothing here.
        assert detail_image_guids(html) == []

    def test_falls_back_to_any_id_in_an_attribute(self):
        html = '<a href="Whatever.aspx?q={44444444-4444-4444-4444-444444444444}">x</a>'
        assert detail_image_guids(html) == ["{44444444-4444-4444-4444-444444444444}"]

    def test_finds_ids_in_onclick_handlers(self):
        html = ("<a onclick=\"OpenImage('{55555555-5555-5555-5555-555555555555}')\">"
                "view</a>")
        assert detail_image_guids(html) == ["{55555555-5555-5555-5555-555555555555}"]

    def test_a_page_with_no_id_yields_nothing(self):
        assert detail_image_guids("<html><body>No image on file.</body></html>") == []
