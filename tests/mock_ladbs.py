"""A small stand-in for the LADBS IDIS ASP.NET site, used by tests/test_flow.py.

It reproduces the behaviour that matters for the scraper:
  * a per-session cookie,
  * a __VIEWSTATE token that is re-issued on every render and rejected once
    stale (this is what a real WebForms app does, and what breaks any client
    that replays captured tokens),
  * the three-step flow: search form -> parcel selection -> paged results,
  * detail pages.
"""
import itertools
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

IDIS = "/IDISPublic_Records/idis"

# Data the mock will "find": AIN 5443-016-018 / 2100 CYPRESS
PARCELS = [
    {"id": "chkAddress$0", "dom_id": "chkAddress_0", "label": "2100 CYPRESS AVE"},
    {"id": "chkAddress$1", "dom_id": "chkAddress_1", "label": "2102 CYPRESS AVE"},
]
RECORDS = {
    # checkbox value -> {page number: [(record_id, type, sub, date, number, guid)]}
    "2100 CYPRESS AVE": {
        1: [("100", "Building Permit", "New", "03/15/2019", "19010-10000-12345", "{G-100}"),
            ("101", "Certificate of Occupancy", "CofO", "06/01/2020", "20016-20000-1", "")],
        2: [("102", "Plan Check", "PC", "01/09/2018", "18010-30000-9", "{G-102}")],
    },
    "2102 CYPRESS AVE": {
        1: [("103", "Building Permit", "Alteration", "07/07/2021", "21016-10000-7", ""),
            ("100", "Building Permit", "New", "03/15/2019", "19010-10000-12345", "{G-100}")],
    },
}

# A high-history parcel: what a busy LA property looks like.
BULK_LABEL = "1000 BULK AVE"
BULK_PARCELS = [{"id": "chkAddress$9", "dom_id": "chkAddress_9", "label": BULK_LABEL}]
RECORDS[BULK_LABEL] = {
    1: [(str(500 + i), "Building Permit", "Alteration", "01/01/1990",
         f"90010-10000-{i:05d}", "{G-%d}" % i if i % 3 == 0 else "")
        for i in range(60)]
}

# An address that matches many assessor parcels — the 6801 Hollywood case.
FAN_PARCELS = [
    {"id": f"chkAddress${i}", "dom_id": f"chkAddress_{i}",
     "label": f"{6801 + i} FANOUT BLVD"}
    for i in range(24)
]
for _p in FAN_PARCELS:
    _n = int(_p["label"].split()[0]) - 6801
    RECORDS[_p["label"]] = {
        1: [(f"{7000 + _n * 10 + j}", "Building Permit", "Alteration",
             "05/05/2005", f"05010-10000-{_n:02d}{j}", "")
            for j in range(5)]
    }

# A parcel like 234 Museum Dr: 5 pages behind a windowed pager, and rows whose
# image pane defaults to Hidden even though the document has an image.
WINDOW_LABEL = "234 MUSEUM DR"
WINDOW_LABEL_W = "234 W MUSEUM DR"
WINDOW_PARCELS = [
    {"id": "chkAddress$5", "dom_id": "chkAddress_5", "label": WINDOW_LABEL},
    {"id": "chkAddress$6", "dom_id": "chkAddress_6", "label": WINDOW_LABEL_W},
]
RECORDS[WINDOW_LABEL] = {
    page: [(f"{800 + page * 10 + j}", "Building Permit", "Alteration",
            "04/10/1983", f"1983LA{64434 + page * 10 + j}",
            # Only the first row of page 1 opens Visible; the rest are Hidden
            # but still carry a document id.
            "{%08d-aaaa-bbbb-cccc-%012d}" % (page, j) if j < 4 else "")
           for j in range(5)]
    for page in range(1, 6)
}

PAGER_LABEL = "500 DEEP AVE"
PAGER_PARCELS = [{"id": "chkAddress$7", "dom_id": "chkAddress_7",
                  "label": PAGER_LABEL}]
RECORDS[PAGER_LABEL] = {
    page: [(f"{600 + page * 10 + j}", "Building Permit", "Alteration",
            "04/10/1983", f"1983LA{64434 + page * 10 + j}",
            "{%08d-aaaa-bbbb-cccc-%012d}" % (page, j) if j < 4 else "")
           for j in range(5)]
    for page in range(1, 6)
}

RECORDS[WINDOW_LABEL_W] = {
    1: [(f"{900 + j}", "Building Permit", "New", "06/01/1955",
         f"1955LA{16019 + j}", "{99999999-aaaa-bbbb-cccc-%012d}" % j)
        for j in range(6)]
}

CYPRESS_PARCELS = [
    {"id": "chkAddress$c0", "dom_id": "chkAddress_c0", "label": "2100 ROWS AVE"},
    {"id": "chkAddress$c1", "dom_id": "chkAddress_c1", "label": "2100 N ROWS AVE"},
    {"id": "chkAddress$c2", "dom_id": "chkAddress_c2", "label": "2100 W ROWS AVE"},
    {"id": "chkAddress$c3", "dom_id": "chkAddress_c3", "label": "2100 2120 ROWS AVE"},
]
for _i, _p in enumerate(CYPRESS_PARCELS):
    RECORDS[_p["label"]] = {
        1: [(f"{300 + _i * 10 + j}", "Building Permit", "Alteration",
             "07/27/1960", f"1960LA{66334 + _i * 10 + j}",
             "{cccccccc-dddd-eeee-ffff-%012d}" % (_i * 10 + j))
            for j in range(4)]
    }

ICON_LABEL = "900 ICONS BLVD"
ICON_PARCELS = [{"id": "chkAddress$i0", "dom_id": "chkAddress_i0",
                 "label": ICON_LABEL}]
RECORDS[ICON_LABEL] = {
    1: [(f"{400 + j}", "Building Permit", "Alteration", "09/02/2005",
         f"05016-10000-{18708 + j}", f"ICON:{j}")
        for j in range(8)]
}

# A search whose address rows lead to a second selection page (parcel
# identifiers: assessor number / address range), as live LADBS does.
TWO_LEVEL_PARCELS = [
    {"id": "chkAddress$t0", "dom_id": "chkAddress_t0", "label": "7100 TWOSTEP AVE"},
    {"id": "chkAddress$t1", "dom_id": "chkAddress_t1", "label": "7100 N TWOSTEP AVE"},
]
TWO_LEVEL_LABELS = {p["label"] for p in TWO_LEVEL_PARCELS}
for _p in TWO_LEVEL_PARCELS:
    _lb = _p["label"]
    RECORDS[f"AS {_lb}"] = {1: [
        (f"{500 + hash(_lb) % 50 + j}", "Building Permit", "New", "01/01/2000",
         f"AS-{_lb[:6]}-{j}", "") for j in range(2)]}
    RECORDS[f"AR {_lb}"] = {1: [
        (f"{560 + hash(_lb) % 50 + j}", "Building Permit", "Alteration",
         "02/02/2001", f"AR-{_lb[:6]}-{j}", "") for j in range(3)]}

# A street where the direction decides which parcel the search resolves to,
# as live LADBS does: a direction-less search reaches only one address record.
VARIANT_SHARED = [{"id": "chkAddress$v0", "dom_id": "chkAddress_v0",
                   "label": "4000 VARIANT AVE"}]
VARIANT_NORTH = [{"id": "chkAddress$v1", "dom_id": "chkAddress_v1",
                  "label": "4000 N VARIANT AVE"}]
RECORDS["4000 VARIANT AVE"] = {1: [
    (f"{700 + j}", "Building Permit", "New", "03/03/1990",
     f"1990LA{70000 + j}", "") for j in range(2)]}
RECORDS["4000 N VARIANT AVE"] = {1: [
    (f"{710 + j}", "Building Permit", "Alteration", "04/04/1991",
     f"1991LA{71000 + j}", "") for j in range(2)]}

_counter = itertools.count(1)


class _State:
    def __init__(self):
        self.sessions = {}          # sid -> set of __VIEWSTATE tokens issued to it
        self.stale_rejections = 0   # posts carrying a token this session never got
        self.hits = []              # request log
        self.searched = set()       # sids that actually ran a document search
        self.detail_delay = 0.0     # simulate real per-request network latency
        self.searches = 0           # how many times the search form was loaded


STATE = _State()


def _page(body, viewstate, action, extra_form="", pre_form=""):
    return f"""<html><head><title>LADBS IDIS (mock)</title></head><body>
{pre_form}
<form id="mainform" method="post" action="{action}">
<input type="hidden" name="__VIEWSTATE" value="{viewstate}" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="GEN123" />
<input type="hidden" name="__EVENTVALIDATION" value="EV-{viewstate}" />
{body}
{extra_form}
</form></body></html>"""


def _expired():
    return ("<html><body><h2>Your session has expired.</h2>"
            "<p>Please start your search again.</p></body></html>")


def _pages_for(labels):
    """One parcel keeps its own paging; several are merged and re-paged."""
    if len(labels) == 1:
        return RECORDS.get(labels[0], {})
    merged = [r for label in labels
              for pg in sorted(RECORDS.get(label, {}))
              for r in RECORDS[label][pg]]
    chunks = [merged[i:i + 25] for i in range(0, len(merged), 25)]
    return {i + 1: chunk for i, chunk in enumerate(chunks)}


def _results_page(labels, page_no, viewstate):
    if isinstance(labels, str):
        labels = [labels]
    pages = _pages_for(labels)
    rows = ["<tr><th>#</th><th>Document Type</th><th>Sub Type</th>"
            "<th>Date</th><th>Number</th></tr>"]
    for i, (rid, dtype, sub, date, num, guid) in enumerate(pages.get(page_no, [])):
        # Real LADBS rows are mostly Hidden even when an image exists; only the
        # occasional row opens Visible.
        icon_only = guid.startswith("ICON:")
        real_guid = "" if icon_only else guid
        vis = "Visible" if (real_guid and i == 0 and page_no == 1) else "Hidden"
        if icon_only:
            # Like the real grid: an icon saying an image exists, no id here.
            icon = "<img src='/images/camera.gif' alt='Digital Image'/>"
        elif guid:
            icon = (f"<a href=\"javascript:OpenImage('{guid}')\">"
                    f"<img src='/images/camera.gif' alt='Digital Image'/></a>")
        else:
            # As live LADBS does: the icon element exists but is CSS-hidden.
            icon = ("<a style='visibility:hidden'><img src='/images/image.gif' "
                    "alt='View digital image' style='VISIBILITY: Hidden'/></a>")
        rows.append(
            f"<tr><td>{i+1}{icon}</td>"
            f"<td><a href=\"javascript:OpenWindow('{rid}','{vis}','{real_guid}')\">{dtype}</a></td>"
            f"<td>{sub}</td><td>{date}</td><td>{num}</td>"
            f"<td><input type='hidden' id='grd_hidComments_{i}' value='note {rid}'/></td></tr>")
    grid = f"<table id='grdIdisResult'>{''.join(rows)}</table>"

    nav = ""
    if len(pages) > 1:
        all_pages = sorted(pages)
        windowed = len(all_pages) > 3
        if windowed:
            start = ((page_no - 1) // 3) * 3
            shown = all_pages[start:start + 3]
        else:
            shown = all_pages
        links = []
        for p in shown:
            if p == page_no:
                links.append(f"<span>{p}</span>")
            else:
                links.append(
                    f"<a href=\"javascript:void(0)\" "
                    f"onclick=\"document.getElementById('pg{p}').submit();return false;\">{p}</a>")
        if windowed and shown and shown[-1] < all_pages[-1]:
            nxt = shown[-1] + 1
            links.append(
                f"<a href=\"javascript:void(0)\" "
                f"onclick=\"document.getElementById('pg{nxt}').submit();return false;\">&gt;</a>")
        forms = "".join(
            f"<form id='pg{p}' method='post' action='{IDIS}/DocumentSearch.aspx?SearchType=DCMT_ASSR'>"
            f"<input type='hidden' name='__VIEWSTATE' value='{viewstate}'/>"
            f"<input type='hidden' name='PageNo' value='{p}'/>"
            + "".join(f"<input type='hidden' name='SelectedLabel' value='{lb}'/>"
                      for lb in labels)
            + "</form>"
            for p in sorted(pages) if p != page_no)
        nav = f"<div id='pnlNavigate'>{''.join(links)}</div>{forms}"

    return f"<html><body>{grid}{nav}</body></html>"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # -- helpers -------------------------------------------------------
    def _sid(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "SID" in cookie:
            return cookie["SID"].value, False
        return f"sid-{next(_counter)}", True

    def _issue_viewstate(self, sid):
        vs = f"VS-{next(_counter)}"
        STATE.sessions.setdefault(sid, set()).add(vs)
        return vs

    def _send(self, html, sid=None, new_cookie=False):
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if new_cookie and sid:
            self.send_header("Set-Cookie", f"SID={sid}; Path=/")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location, sid=None, new_cookie=False):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if new_cookie and sid:
            self.send_header("Set-Cookie", f"SID={sid}; Path=/")
        self.end_headers()

    # -- GET -----------------------------------------------------------
    def do_GET(self):
        sid, is_new = self._sid()
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        STATE.hits.append(("GET", url.path, url.query))
        if url.path == "/favicon.ico":
            return self._send("", sid, is_new)
        vs = self._issue_viewstate(sid)

        if url.path.endswith("/ParcelSearch.aspx"):
            STATE.searches += 1
            search_type = (qs.get("SearchType") or [""])[0]
            if search_type == "PRCL_ASMT":
                body = ("Book <input type='text' name='Assessor$txtAssessorNoBook' "
                        "id='Assessor_txtAssessorNoBook'/>"
                        "Page <input type='text' name='Assessor$txtAssessorNoPage' "
                        "id='Assessor_txtAssessorNoPage'/>"
                        "Parcel <input type='text' name='Assessor$txtAssessorNoParcel' "
                        "id='Assessor_txtAssessorNoParcel'/>"
                        "<input type='submit' name='btnSearchAssessor' "
                        "id='btnSearchAssessor' value='Search'/>")
                action = f"{IDIS}/DocumentSearch.aspx?SearchType=DCMT_ASSR"
            else:
                body = ("No <input type='text' name='Address$txtAddressBegNo' "
                        "id='Address_txtAddressBegNo'/>"
                        "Dir <input type='text' name='Address$txtAddressDirection' "
                        "id='Address_txtAddressDirection'/>"
                        "Street <input type='text' name='Address$txtAddressStreetName' "
                        "id='Address_txtAddressStreetName'/>"
                        "<input type='submit' name='btnNext1' id='btnNext1' value='Next'/>")
                action = f"{IDIS}/DocumentSearch.aspx?SearchType=DCMT_ADDR"
            return self._send(_page(body, vs, action), sid, is_new)

        if url.path.endswith("/SessionExpired.aspx"):
            return self._send(_expired(), sid, is_new)

        if url.path.endswith("/Report.aspx"):
            rid = (qs.get("Record_Id") or ["?"])[0]
            if sid not in STATE.searched:
                return self._redirect(f"{IDIS}/SessionExpired.aspx", sid, is_new)
            if STATE.detail_delay:
                import time as _t
                _t.sleep(STATE.detail_delay)
            image_link = ""
            if (qs.get("Image") or [""])[0] == "Visible":
                guid = "{deadbeef-0000-1111-2222-%012d}" % int(rid)
                image_link = (f"<a href='ImageMain.aspx?DocIds={guid}'>"
                              f"View Digital Image</a>")
            return self._send(
                f"<html><body><h3>Record {rid}</h3>"
                f"<b>Address:</b> 2100 CYPRESS AVE<br/>"
                f"<b>Status:</b> Finaled<br/>"
                f"<b>Applicant:</b> ACME Builders<br/>{image_link}</body></html>",
                sid, is_new)

        return self._send("<html><body>LADBS mock home</body></html>", sid, is_new)

    # -- POST ----------------------------------------------------------
    def do_POST(self):
        sid, is_new = self._sid()
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        STATE.hits.append(("POST", url.path, sorted(form)))

        def one(name, default=""):
            return (form.get(name) or [default])[0]

        # A real WebForms app rejects a __VIEWSTATE that was not issued to
        # this session (e.g. one replayed from a different cookie jar).
        if one("__VIEWSTATE") not in STATE.sessions.get(sid, set()):
            STATE.stale_rejections += 1
            return self._send(_expired(), sid, is_new)

        vs = self._issue_viewstate(sid)

        # Step 1 submissions -> parcel selection page
        if "btnSearchAssessor" in form or "btnNext1" in form:
            parcels = PARCELS
            if "btnSearchAssessor" in form:
                ain = (one("Assessor$txtAssessorNoBook"),
                       one("Assessor$txtAssessorNoPage"),
                       one("Assessor$txtAssessorNoParcel"))
                if ain == ("9999", "999", "999"):        # high-history parcel
                    ok, parcels = True, BULK_PARCELS
                elif ain == ("5467", "018", "015"):
                    # Like the real site: the assessor lookup surfaces only
                    # the directional address row, not its sibling.
                    ok, parcels = True, WINDOW_PARCELS[1:]
                elif ain == ("7100", "100", "100"):      # two-level selection
                    ok, parcels = True, TWO_LEVEL_PARCELS
                elif ain == ("9000", "000", "000"):      # ids only on detail pages
                    ok, parcels = True, ICON_PARCELS
                elif ain == ("5468", "018", "015"):      # 5 pages, windowed pager
                    ok, parcels = True, PAGER_PARCELS
                elif ain == ("7777", "777", "777"):      # 24-parcel address
                    ok, parcels = True, FAN_PARCELS
                elif ain == ("4030", "030", "030"):      # upstream refuses us
                    return self._send(
                        "<html><head><title>Access Denied</title></head><body>"
                        "<h1>Access Denied</h1><p>Your request was blocked.</p>"
                        "</body></html>", sid, is_new)
                else:
                    ok = ain == ("5443", "016", "018")
            else:
                street = one("Address$txtAddressStreetName").upper()
                if street == "VARIANT" and one("Address$txtAddressBegNo") == "4000":
                    direction = one("Address$txtAddressDirection").upper().strip()
                    if direction in ("", "W"):
                        # Blank and W resolve to the same parcel.
                        ok, parcels = True, VARIANT_SHARED
                    elif direction == "N":
                        ok, parcels = True, VARIANT_NORTH
                    else:
                        ok = False
                elif street == "FANOUT":
                    ok, parcels = True, FAN_PARCELS
                elif street == "MUSEUM" and one("Address$txtAddressBegNo") == "234":
                    # The address search returns every matching row.
                    ok, parcels = True, WINDOW_PARCELS
                else:
                    if street == "ROWS" and one("Address$txtAddressBegNo") == "2100":
                        ok, parcels = True, CYPRESS_PARCELS   # four address rows
                    else:
                        ok = (one("Address$txtAddressBegNo") == "2100"
                              and street == "CYPRESS")
            if not ok:
                return self._send(
                    "<html><body>No parcels matched your search.</body></html>", sid, is_new)

            # Mirrors the real page: unrelated "Display Fields" checkboxes and
            # an "All" toggle sit outside the address grid.
            boxes = [
                "<div>Display Fields "
                "<input type='checkbox' id='AllFields' name='AllFields'/>All Fields"
                "<input type='checkbox' id='Frac' name='Frac'/>Frac"
                "<input type='checkbox' id='Unit' name='Unit'/>Unit"
                "<input type='checkbox' id='ZipCode' name='ZipCode'/>Zip Code"
                "</div>",
                "<input type='checkbox' id='All' name='All'/> All "
                "(Note: Historical addresses are in red text)",
                # Wrap the grid in a layout table, as the real page does.
                "<table id='pageLayout'><tr><td>"
                "<table id='grdAddress'>"
                "<tr><th>Select</th><th>Beg Nbr</th><th>End Nbr</th>"
                "<th>Dir</th><th>Str Name</th><th>Str Type</th></tr>",
            ]
            for p_ in parcels:
                parts = p_["label"].split()
                beg = parts[0]
                direction = parts[1] if len(parts) > 3 else ""
                str_name = parts[2] if direction else parts[1]
                str_type = parts[-1]
                boxes.append(
                    f"<tr><td><input type='checkbox' id='{p_['dom_id']}' "
                    f"name='{p_['id']}' value='{p_['label']}'/></td>"
                    f"<td>{beg}</td><td></td><td>{direction}</td>"
                    f"<td>{str_name}</td><td>{str_type}</td></tr>")
            boxes.append("</table></td></tr></table>")
            boxes.append("<input type='submit' name='btnNext2' id='btnNext2' value='Continue'/>")
            action = f"{IDIS}/DocumentSearch.aspx?SearchType=DCMT_ASSR"
            return self._send(_page("".join(boxes), vs, action), sid, is_new)

        # Step 2 -> results, or a second selection page for two-level parcels
        if "btnNext2" in form:
            idents = [v for k, vals in form.items()
                      if k.startswith("chkIdent") for v in vals]
            if idents:
                STATE.searched.add(sid)
                return self._send(_results_page(idents, 1, vs), sid, is_new)

            selected = [v for k, vals in form.items()
                        if k.startswith("chkAddress") for v in vals]
            if not selected:
                return self._send(
                    "<html><body>Please select a parcel.</body></html>", sid, is_new)

            if all(v in TWO_LEVEL_LABELS for v in selected):
                # Identifier page: plain checkboxes, no address grid.
                lb = selected[0]
                body = (
                    "Please choose from the following to limit your search<br/>"
                    f"<input type='checkbox' id='chkIdent_0' name='chkIdent$0' "
                    f"value='AS {lb}'/> AS {lb}<br/>"
                    f"<input type='checkbox' id='chkIdent_1' name='chkIdent$1' "
                    f"value='AR {lb}'/> AR {lb}<br/>"
                    "<input type='submit' name='btnNext2' id='btnNext2' "
                    "value='Continue'/>")
                action = f"{IDIS}/DocumentSearch.aspx?SearchType=DCMT_ASSR"
                return self._send(_page(body, vs, action), sid, is_new)

            STATE.searched.add(sid)
            return self._send(_results_page(selected, 1, vs), sid, is_new)

        # Pagination postback
        if "PageNo" in form:
            STATE.searched.add(sid)
            return self._send(
                _results_page(form.get("SelectedLabel", []),
                              int(one("PageNo", "1")), vs), sid, is_new)

        return self._send("<html><body>Unhandled post</body></html>", sid, is_new)


class MockLADBS:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        STATE.sessions.clear()
        STATE.hits.clear()
        STATE.searched.clear()
        STATE.searches = 0
        STATE.stale_rejections = 0
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
