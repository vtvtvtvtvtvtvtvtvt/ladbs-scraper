"""Records must not be silently dropped — by the pager or by image detection.

Both failures below were reported from live LADBS on 234 Museum Dr:
3 of 5 pages read while claiming truncated:false, and 4 of 51 rows carrying
an image link when the site showed icons on most of them.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from bs4 import BeautifulSoup

import scraper as scraper_mod
from scraper import (
    LADBSScraper, parse_results_html, row_image_guids, row_has_image_icon,
)
from mock_ladbs import MockLADBS, IDIS

scraper_mod.SETTLE_SECONDS = 0.05

WINDOW_AIN = "5467-018-015"   # 5 pages behind a 3-wide pager, 25 records


def scrape(value, **kwargs):
    with MockLADBS() as mock:
        old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
        scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
        scraper_mod.MAIN_URL = mock.base
        try:
            return asyncio.run(LADBSScraper(**kwargs).scrape_by_ain(
                value, include_details=False))
        finally:
            scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old


@pytest.fixture(scope="module")
def windowed():
    return scrape(WINDOW_AIN)


class TestWindowedPager:
    def test_reads_every_page_not_just_the_first_window(self, windowed):
        # The bug: the pager shows 3 numbers at a time, so it stopped at 3 of 5.
        assert windowed["diagnostics"]["result_pages"] == 5
        assert windowed["total_records"] == 25

    def test_knows_how_many_pages_there_were(self, windowed):
        assert windowed["diagnostics"]["pages_advertised"] == 5

    def test_completing_all_pages_is_not_truncated(self, windowed):
        assert windowed["diagnostics"]["truncated"] is False
        assert windowed["status"] == "ok"

    def test_records_are_distinct_across_pages(self, windowed):
        ids = [r["record_id"] for r in windowed["records"]]
        assert len(set(ids)) == 25

    def test_a_short_read_is_reported_as_truncated(self):
        # If the budget stops paging early, it must NOT claim a clean finish.
        result = scrape(WINDOW_AIN, budget_seconds=10)
        if result["diagnostics"]["result_pages"] < 5:
            assert result["diagnostics"]["truncated"] is True


class TestImageDetection:
    def test_hidden_rows_still_get_their_image(self, windowed):
        # 4 of every 5 mock rows carry a guid; only one row opens "Visible".
        with_image = [r for r in windowed["records"] if r["has_digital_image"]]
        assert len(with_image) == 20, "Hidden-pane rows lost their image link"

    def test_visible_flag_is_reported_separately(self, windowed):
        visible = [r for r in windowed["records"] if r["image_pane_visible"]]
        assert len(visible) == 1
        # ...and it must not be what decides has_digital_image.
        assert len(visible) != len([r for r in windowed["records"]
                                    if r["has_digital_image"]])

    def test_image_urls_are_built(self, windowed):
        for rec in windowed["records"]:
            if rec["has_digital_image"]:
                assert "ImageMain.aspx?DocIds=" in rec["digital_image_url"]
                assert rec["attachments"]

    def test_icon_coverage_is_counted(self, windowed):
        diag = windowed["diagnostics"]
        assert diag["records_with_image"] == 20
        assert diag["rows_showing_image_icon"] == 20

    def test_rows_without_a_document_id_stay_false(self, windowed):
        without = [r for r in windowed["records"] if not r["has_digital_image"]]
        assert len(without) == 5
        assert all(r["digital_image_url"] is None for r in without)


class TestGuidExtraction:
    """The unit behind the fix: find a guid wherever the row hides it."""

    def _row(self, html):
        return BeautifulSoup(f"<table><tr>{html}</tr></table>",
                             "html.parser").find("tr")

    def test_guid_from_the_document_link(self):
        row = self._row("<td><a href=\"javascript:OpenWindow('1','Hidden',"
                        "'{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}')\">P</a></td>")
        assert row_image_guids(row, "{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}")

    def test_guid_from_a_separate_icon_link(self):
        row = self._row("<td><a href=\"javascript:OpenImage("
                        "'{11111111-2222-3333-4444-555555555555}')\">"
                        "<img src='camera.gif'/></a></td>")
        assert row_image_guids(row, "") == ["{11111111-2222-3333-4444-555555555555}"]

    def test_multiple_guids_are_all_kept(self):
        row = self._row("<td onclick=\"View('{11111111-1111-1111-1111-111111111111},"
                        "{22222222-2222-2222-2222-222222222222}')\">x</td>")
        assert len(row_image_guids(row, "")) == 2

    def test_no_guid_means_no_image(self):
        row = self._row("<td><a href=\"javascript:OpenWindow('1','Hidden','')\">P</a></td>")
        assert row_image_guids(row, "") == []

    def test_icon_is_detected(self):
        assert row_has_image_icon(self._row("<td><img src='/i/camera.gif'/></td>"))
        assert row_has_image_icon(self._row("<td><img src='/x.gif' alt='Digital Image'/></td>"))
        assert not row_has_image_icon(self._row("<td><img src='/i/spacer.gif'/></td>"))


class TestDebugCapture:
    def test_debug_returns_real_markup(self):
        diag = scrape(WINDOW_AIN, debug=True)["diagnostics"]
        assert "grdIdisResult" not in diag["grid_html_sample"]  # rows only
        assert "OpenWindow(" in diag["grid_html_sample"]
        assert diag["pager_html"] and "pnlNavigate" in diag["pager_html"]

    def test_debug_is_off_by_default(self, windowed):
        assert "grid_html_sample" not in windowed["diagnostics"]
