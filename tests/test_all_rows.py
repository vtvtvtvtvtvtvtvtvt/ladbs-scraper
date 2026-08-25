"""2100 Cypress Ave: four address rows, and all four must be pulled.

The LADBS selection page for "2100 Cypress" offers:
    2100          CYPRESS AVE
    2100   N      CYPRESS AVE
    2100   W      CYPRESS AVE
    2100 - 2120   CYPRESS AVE
Each row holds its own documents. The grid sits inside the page's layout
table, and the page also carries Display Fields controls and an "All" toggle.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import scraper as scraper_mod
from scraper import LADBSScraper, find_parcel_table, parse_checkboxes
from mock_ladbs import MockLADBS, IDIS
from bs4 import BeautifulSoup

scraper_mod.SETTLE_SECONDS = 0.05

# Four address rows, as "2100 Cypress" gives on the real site.
CYPRESS = "2100 Rows Ave, Los Angeles, CA 90065"

# The real page's shape: layout table wrapping the grid, controls outside it.
REAL_PAGE = """
<html><body>
<table id="layout"><tr><td>
  <div>Display Fields
    <input type="checkbox" id="AllFields" name="AllFields"/>All Fields
    <input type="checkbox" id="Frac" name="Frac"/>Frac
    <input type="checkbox" id="Unit" name="Unit"/>Unit
    <input type="checkbox" id="ZipCode" name="ZipCode"/>Zip Code
  </div>
  <input type="checkbox" id="All" name="All"/> All
  <table id="grid">
    <tr><th>Select</th><th>Beg Nbr</th><th>End Nbr</th>
        <th>Dir</th><th>Str Name</th><th>Str Type</th></tr>
    <tr><td><input type="checkbox" id="c0" name="c$0" value="a"/></td>
        <td>2100</td><td></td><td></td><td>CYPRESS</td><td>AVE</td></tr>
    <tr><td><input type="checkbox" id="c1" name="c$1" value="b"/></td>
        <td>2100</td><td></td><td>N</td><td>CYPRESS</td><td>AVE</td></tr>
    <tr><td><input type="checkbox" id="c2" name="c$2" value="c"/></td>
        <td>2100</td><td></td><td>W</td><td>CYPRESS</td><td>AVE</td></tr>
    <tr><td><input type="checkbox" id="c3" name="c$3" value="d"/></td>
        <td>2100</td><td>2120</td><td></td><td>CYPRESS</td><td>AVE</td></tr>
  </table>
</td></tr></table>
</body></html>"""


class TestRealSelectionPageShape:
    def test_finds_the_grid_not_the_layout_table(self):
        table = find_parcel_table(BeautifulSoup(REAL_PAGE, "html.parser"))
        assert table is not None and table.get("id") == "grid"

    def test_finds_all_four_address_rows(self):
        assert len(parse_checkboxes(REAL_PAGE)) == 4

    def test_excludes_display_controls_and_the_all_toggle(self):
        names = [c["name"] for c in parse_checkboxes(REAL_PAGE)]
        assert names == ["c$0", "c$1", "c$2", "c$3"]

    def test_labels_carry_direction_and_range(self):
        labels = [c["label"] for c in parse_checkboxes(REAL_PAGE)]
        assert any("N CYPRESS" in lb for lb in labels)
        assert any("W CYPRESS" in lb for lb in labels)
        assert any("2120" in lb for lb in labels)   # the range row


def scrape(**kwargs):
    with MockLADBS() as mock:
        old = scraper_mod.BASE_URL, scraper_mod.MAIN_URL
        scraper_mod.BASE_URL = f"{mock.base}{IDIS}"
        scraper_mod.MAIN_URL = mock.base
        try:
            return asyncio.run(LADBSScraper(**kwargs).scrape(
                CYPRESS, include_details=False))
        finally:
            scraper_mod.BASE_URL, scraper_mod.MAIN_URL = old


@pytest.fixture(scope="module")
def auto():
    return scrape()


class TestSelectionIsVerified:
    def test_every_row_is_confirmed_ticked_before_submitting(self, auto):
        # The ticks are read back, not assumed.
        diag = auto["diagnostics"]
        assert diag["address_rows_found"] == diag["address_rows_checked"]
        assert diag["address_rows_checked"] > 1

    def test_the_pages_all_control_is_used(self, auto):
        assert auto["diagnostics"]["used_all_control"] is True

    def test_every_row_contributes(self, auto):
        rows_walked = [s for s in auto["diagnostics"]["strategy"]
                       if s["step"] == "row"]
        assert len(rows_walked) == auto["diagnostics"]["address_rows_found"]

    def test_records_from_all_rows_are_merged_and_deduped(self, auto):
        ids = [r["record_id"] for r in auto["records"]]
        assert len(ids) == len(set(ids))
        assert auto["total_records"] > 0


class TestModes:
    def test_each_mode_walks_every_row(self):
        result = scrape(parcel_mode="each")
        walked = [s for s in result["diagnostics"]["strategy"] if s["step"] == "row"]
        assert len(walked) == result["diagnostics"]["checkboxes_found"]

    def test_all_mode_submits_once(self):
        result = scrape(parcel_mode="all")
        steps = [s["step"] for s in result["diagnostics"]["strategy"]]
        assert steps == ["combined"]

    def test_auto_does_both_when_rows_are_few(self, auto):
        steps = [s["step"] for s in auto["diagnostics"]["strategy"]]
        assert "combined" in steps and "row" in steps

    def test_all_modes_find_the_same_records(self, auto):
        each = scrape(parcel_mode="each")
        assert ({r["record_id"] for r in each["records"]}
                == {r["record_id"] for r in auto["records"]})
