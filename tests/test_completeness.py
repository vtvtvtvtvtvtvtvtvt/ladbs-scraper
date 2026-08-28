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

PAGER_AIN = "5468-018-015"    # one address, 5 pages behind a 3-wide pager
# The address search returns both rows; the assessor search reaches only one.
MUSEUM_ADDRESS = "234 Museum Drive, Los Angeles, CA, USA"


def scrape_address(value, **kwargs):
    with MockLADBS() as mock:
        old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
        scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
        scraper_mod.MAIN_URL = mock.base
        try:
            return asyncio.run(LADBSScraper(**kwargs).scrape(
                value, include_details=False))
        finally:
            scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old


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
    return scrape(PAGER_AIN)


@pytest.fixture(scope="module")
def museum():
    return scrape_address(MUSEUM_ADDRESS)


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
        result = scrape(PAGER_AIN, budget_seconds=10)
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
        diag = scrape(PAGER_AIN, debug=True)["diagnostics"]
        assert "grdIdisResult" not in diag["grid_html_sample"]  # rows only
        assert "OpenWindow(" in diag["grid_html_sample"]
        assert diag["pager_html"] and "pnlNavigate" in diag["pager_html"]

    def test_debug_is_off_by_default(self, windowed):
        assert "grid_html_sample" not in windowed["diagnostics"]


class TestEveryMatchingAddressRow:
    """A search can match several address rows that differ only by direction.

    "234 Museum Dr" returns both "234 MUSEUM DR" and "234 W MUSEUM DR".
    Taking one of them silently halves the results.
    """

    def test_both_address_rows_are_found(self, museum):
        assert museum["diagnostics"]["checkboxes_found"] == 2

    def test_page_controls_are_not_mistaken_for_addresses(self, museum):
        # The page also carries All Fields / Frac / Unit / Zip Code and an
        # "All" toggle. Counting those as parcels submits a display option.
        labels = " ".join(museum["diagnostics"]["parcels"]).lower()
        for control in ("all fields", "frac", "unit", "zip code"):
            assert control not in labels

    def test_both_rows_are_selected_by_name(self, museum):
        labels = museum["diagnostics"]["parcels"]
        assert any("W MUSEUM" in lb for lb in labels)
        assert any(lb.strip().startswith("234") and "W" not in lb.split()
                   for lb in labels)

    def test_records_from_both_rows_come_back(self, museum):
        # 25 from "234 MUSEUM DR" + 6 from "234 W MUSEUM DR"
        assert museum["total_records"] == 31

    def test_the_second_rows_records_are_present(self, museum):
        numbers = {r["doc_number"] for r in museum["records"]}
        assert any(n.startswith("1955LA") for n in numbers), \
            "records from the W Museum row are missing"


class TestParcelTableDetection:
    def test_display_field_checkboxes_are_excluded(self):
        from scraper import parse_checkboxes
        html = """
        <html><body>
          <input type="checkbox" name="AllFields"/>All Fields
          <input type="checkbox" name="Frac"/>Frac
          <input type="checkbox" name="Unit"/>Unit
          <input type="checkbox" name="ZipCode"/>Zip Code
          <input type="checkbox" name="All"/>All
          <table>
            <tr><th>Select</th><th>Beg Nbr</th><th>End Nbr</th>
                <th>Dir</th><th>Str Name</th><th>Str Type</th></tr>
            <tr><td><input type="checkbox" name="c$0" value="a"/></td>
                <td>234</td><td></td><td></td><td>MUSEUM</td><td>DR</td></tr>
            <tr><td><input type="checkbox" name="c$1" value="b"/></td>
                <td>234</td><td></td><td>W</td><td>MUSEUM</td><td>DR</td></tr>
          </table>
        </body></html>"""
        found = parse_checkboxes(html)
        assert [c["name"] for c in found] == ["c$0", "c$1"]

    def test_row_labels_describe_the_address(self):
        from scraper import parse_checkboxes
        html = """<table>
            <tr><th>Select</th><th>Beg Nbr</th><th>Dir</th><th>Str Name</th></tr>
            <tr><td><input type="checkbox" name="c$1" value="b"/></td>
                <td>234</td><td>W</td><td>MUSEUM</td></tr></table>"""
        assert "234 W MUSEUM" in parse_checkboxes(html)[0]["label"]

    def test_falls_back_when_there_is_no_grid(self):
        from scraper import parse_checkboxes
        html = """<html><body>
            <input type="checkbox" name="AllFields"/>
            <input type="checkbox" name="CheckAll"/>
            <input type="checkbox" name="chkAddress$0" value="234 MUSEUM DR"/>
        </body></html>"""
        assert [c["name"] for c in parse_checkboxes(html)] == ["chkAddress$0"]


class TestDiagnosticsAreReportedInEveryMode:
    """The caller ran parcel_mode="each" and got empty address_rows."""

    def test_parcels_recorded_when_walking_rows_one_at_a_time(self):
        result = scrape_address(MUSEUM_ADDRESS, parcel_mode="each")
        assert result["diagnostics"]["parcels"] == ["234 MUSEUM DR", "234 W MUSEUM DR"]

    def test_parcels_recorded_in_combined_mode(self, museum):
        assert museum["diagnostics"]["parcels"] == ["234 MUSEUM DR", "234 W MUSEUM DR"]

    def test_pager_markup_is_kept_without_asking(self, windowed):
        # Page counts are the most-disputed number; keep the evidence.
        assert "pnlNavigate" in windowed["diagnostics"]["pager_html"]

    def test_selection_page_markup_is_always_kept(self):
        diag = scrape(PAGER_AIN)["diagnostics"]
        assert diag["checkboxes_found"] == 1
        assert diag["selection_page"]["sample"]
        assert len(diag["selection_page"]["checkboxes"]) == 1


class TestUnresolvedImageEvidence:
    """When icons outnumber extracted ids, keep the row that explains it."""

    def _grid(self, row_html):
        return f"<table id='grdIdisResult'><tr><th>h</th></tr>{row_html}</table>"

    def test_captures_a_row_whose_icon_has_no_document_id(self):
        from scraper import unresolved_image_row
        row = ("<tr><td><a href=\"javascript:ShowImg(3)\">"
               "<img src='/i/camera.gif' alt='Digital Image'/></a></td>"
               "<td><a href=\"javascript:OpenWindow('1','Hidden','')\">P</a></td>"
               "</tr>")
        found = unresolved_image_row(self._grid(row))
        assert found and "camera.gif" in found

    def test_ignores_rows_whose_icon_resolves(self):
        from scraper import unresolved_image_row
        row = ("<tr><td><a href=\"javascript:OpenImage("
               "'{11111111-2222-3333-4444-555555555555}')\">"
               "<img src='camera.gif'/></a></td></tr>")
        assert unresolved_image_row(self._grid(row)) is None

    def test_ignores_rows_with_no_icon(self):
        from scraper import unresolved_image_row
        assert unresolved_image_row(self._grid("<tr><td>plain</td></tr>")) is None

    def test_no_grid_is_safe(self):
        from scraper import unresolved_image_row
        assert unresolved_image_row("<html><body>nothing</body></html>") is None


class TestHiddenIconsAreNotCounted:
    """LADBS renders a CSS-hidden icon on rows with no image; a hidden icon is
    not a claim that an image exists — counting it produced a false '15 icons,
    4 ids' alarm on a parcel whose true visible-icon count was 4."""

    def _row(self, html):
        return BeautifulSoup(f"<table><tr>{html}</tr></table>",
                             "html.parser").find("tr")

    def test_visible_icon_counts(self):
        assert row_has_image_icon(self._row(
            "<td><img src='images/image.gif' alt='View digital image'/></td>"))

    def test_icon_hidden_on_itself_does_not_count(self):
        assert not row_has_image_icon(self._row(
            "<td><img src='images/image.gif' alt='View digital image' "
            "style='VISIBILITY: Hidden'/></td>"))

    def test_icon_inside_hidden_anchor_does_not_count(self):
        # The exact shape live LADBS renders on imageless rows.
        assert not row_has_image_icon(self._row(
            "<td><a style=\"visibility:hidden\"> "
            "<img src='images/image.gif' alt='View digital image'/></a></td>"))

    def test_display_none_ancestor_does_not_count(self):
        assert not row_has_image_icon(self._row(
            "<td style='display:none'><img src='images/image.gif' "
            "alt='View digital image'/></td>"))

    def test_icon_and_id_counts_agree_on_the_mock_parcel(self, windowed):
        diag = windowed["diagnostics"]
        assert (diag["rows_showing_image_icon"]
                == diag["records_with_image"] == 20)
        assert not any("image extraction is incomplete" in w
                       for w in diag["warnings"])
