"""Offline unit tests for the LADBS parsers — no network required.

Run: python -m pytest tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from scraper import (
    format_ain,
    parse_address,
    parse_checkboxes,
    parse_results_html,
    split_ain,
    LADBSScraper,
)

RESULTS_HTML = """
<html><body>
<table id="grdIdisResult">
  <tr><th>#</th><th>Type</th><th>Sub</th><th>Date</th><th>Number</th></tr>
  <tr>
    <td>1</td>
    <td><a href="javascript:OpenWindow('123456','Visible','{ABCD-EF01}')">Building Permit</a></td>
    <td>New Construction</td><td>03/15/2019</td><td>19010-10000-12345</td>
    <td><input type="hidden" id="grd_hidComments_0" value="Some comment"/></td>
  </tr>
  <tr>
    <td>2</td>
    <td><a href="javascript:OpenWindow('123457','Hidden','')">Certificate of Occupancy</a></td>
    <td>CofO</td><td>06/01/2020</td><td>20016-20000-00001</td>
  </tr>
  <tr><td colspan="5">footer junk</td></tr>
</table>
</body></html>
"""

SELECTION_HTML = """
<html><body><form>
<input type="checkbox" id="CheckAll" name="CheckAll" value="on"/>
<input type="checkbox" id="chkAddress_0" name="chkAddress$0" value="2100 CYPRESS AVE"/>
<input type="checkbox" id="chkAddress_1" name="chkAddress$1" value="2102 CYPRESS AVE"/>
</form></body></html>
"""


class TestParseResults:
    def test_extracts_both_records(self):
        recs = parse_results_html(RESULTS_HTML)
        assert len(recs) == 2

    def test_first_record_fields(self):
        rec = parse_results_html(RESULTS_HTML)[0]
        assert rec["record_id"] == "123456"
        assert rec["doc_type"] == "Building Permit"
        assert rec["sub_type"] == "New Construction"
        assert rec["doc_date"] == "03/15/2019"
        assert rec["doc_number"] == "19010-10000-12345"
        assert rec["comments"] == "Some comment"
        assert rec["has_digital_image"] is True
        assert "{ABCD-EF01}" in rec["digital_image_url"]
        assert len(rec["attachments"]) == 1

    def test_record_without_image_has_no_attachment(self):
        rec = parse_results_html(RESULTS_HTML)[1]
        assert rec["has_digital_image"] is False
        assert rec["digital_image_url"] is None
        assert rec["attachments"] == []

    def test_detail_url_is_built(self):
        rec = parse_results_html(RESULTS_HTML)[0]
        assert "Report.aspx?Record_Id=123456" in rec["detail_url"]

    def test_missing_grid_returns_empty(self):
        assert parse_results_html("<html><body>Session expired</body></html>") == []


class TestParseCheckboxes:
    def test_skips_check_all(self):
        cbs = parse_checkboxes(SELECTION_HTML)
        assert [c["name"] for c in cbs] == ["chkAddress$0", "chkAddress$1"]

    def test_keeps_values(self):
        cbs = parse_checkboxes(SELECTION_HTML)
        assert cbs[0]["value"] == "2100 CYPRESS AVE"
        assert cbs[0]["id"] == "chkAddress_0"

    def test_no_checkboxes(self):
        assert parse_checkboxes("<html><body>nothing</body></html>") == []


class TestParseAddress:
    @pytest.mark.parametrize("raw,expected", [
        ("2100 Cypress Ave, Los Angeles, CA 90065", ("2100", "CYPRESS", "")),
        ("1234 S San Fernando Rd", ("1234", "SAN FERNANDO", "S")),
        ("500 West 7th Street, Los Angeles", ("500", "7TH", "W")),
        ("742 Evergreen", ("742", "EVERGREEN", "")),
        ("100 Beverly Glen Blvd", ("100", "BEVERLY GLEN", "")),
        ("1 Broadway", ("1", "BROADWAY", "")),
        ("9000 Sunset Blvd Unit 4", ("9000", "SUNSET", "")),
        ("9000 Sunset Blvd #4", ("9000", "SUNSET", "")),
    ])
    def test_parses(self, raw, expected):
        assert parse_address(raw) == expected

    def test_multiword_street_is_not_truncated(self):
        # The old parser returned only "SAN" here.
        assert parse_address("1234 San Fernando Rd")[1] == "SAN FERNANDO"

    def test_direction_is_not_mistaken_for_street(self):
        # The old parser returned "S" as the street name.
        assert parse_address("1234 S Broadway")[1] == "BROADWAY"

    @pytest.mark.parametrize("bad", ["", "   ", "Cypress Ave"])
    def test_rejects_unparseable(self, bad):
        with pytest.raises(ValueError):
            parse_address(bad)


class TestAin:
    def test_format(self):
        assert format_ain("5443016018") == "5443-016-018"
        assert format_ain("5443-016-018") == "5443-016-018"

    def test_split(self):
        assert split_ain("5443-016-018") == ("5443", "016", "018")

    @pytest.mark.parametrize("bad", ["123", "", "abc", "12345678901"])
    def test_split_rejects_bad_length(self, bad):
        with pytest.raises(ValueError):
            split_ain(bad)


class TestSummary:
    def test_counts_types_and_attachments(self):
        recs = parse_results_html(RESULTS_HTML)
        summary = LADBSScraper()._build_summary(recs, "AIN 5443-016-018")
        assert "Found 2 record(s) for AIN 5443-016-018" in summary
        assert "Building Permit: 1" in summary
        assert "Total attachments available: 1" in summary

    def test_empty(self):
        assert "No records found" in LADBSScraper()._build_summary([], "x")


class TestDedupe:
    def test_removes_repeats_keeps_order(self):
        recs = [{"record_id": "1"}, {"record_id": "2"}, {"record_id": "1"}]
        out = LADBSScraper._dedupe(recs)
        assert [r["record_id"] for r in out] == ["1", "2"]
