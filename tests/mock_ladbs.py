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

_counter = itertools.count(1)


class _State:
    def __init__(self):
        self.sessions = {}          # sid -> set of __VIEWSTATE tokens issued to it
        self.stale_rejections = 0   # posts carrying a token this session never got
        self.hits = []              # request log
        self.searched = set()       # sids that actually ran a document search
        self.detail_delay = 0.0     # simulate real per-request network latency


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


def _results_page(label, page_no, viewstate):
    pages = RECORDS.get(label, {})
    rows = ["<tr><th>#</th><th>Document Type</th><th>Sub Type</th>"
            "<th>Date</th><th>Number</th></tr>"]
    for i, (rid, dtype, sub, date, num, guid) in enumerate(pages.get(page_no, [])):
        vis = "Visible" if guid else "Hidden"
        rows.append(
            f"<tr><td>{i+1}</td>"
            f"<td><a href=\"javascript:OpenWindow('{rid}','{vis}','{guid}')\">{dtype}</a></td>"
            f"<td>{sub}</td><td>{date}</td><td>{num}</td>"
            f"<td><input type='hidden' id='grd_hidComments_{i}' value='note {rid}'/></td></tr>")
    grid = f"<table id='grdIdisResult'>{''.join(rows)}</table>"

    nav = ""
    if len(pages) > 1:
        links = []
        for p in sorted(pages):
            if p == page_no:
                links.append(f"<span>{p}</span>")
            else:
                links.append(
                    f"<a href=\"javascript:void(0)\" "
                    f"onclick=\"document.getElementById('pg{p}').submit();return false;\">{p}</a>")
        forms = "".join(
            f"<form id='pg{p}' method='post' action='{IDIS}/DocumentSearch.aspx?SearchType=DCMT_ASSR'>"
            f"<input type='hidden' name='__VIEWSTATE' value='{viewstate}'/>"
            f"<input type='hidden' name='PageNo' value='{p}'/>"
            f"<input type='hidden' name='SelectedLabel' value='{label}'/>"
            f"</form>"
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
            return self._send(
                f"<html><body><h3>Record {rid}</h3>"
                f"<b>Address:</b> 2100 CYPRESS AVE<br/>"
                f"<b>Status:</b> Finaled<br/>"
                f"<b>Applicant:</b> ACME Builders</body></html>", sid, is_new)

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
                elif ain == ("4030", "030", "030"):      # upstream refuses us
                    return self._send(
                        "<html><head><title>Access Denied</title></head><body>"
                        "<h1>Access Denied</h1><p>Your request was blocked.</p>"
                        "</body></html>", sid, is_new)
                else:
                    ok = ain == ("5443", "016", "018")
            else:
                ok = (one("Address$txtAddressBegNo") == "2100"
                      and one("Address$txtAddressStreetName").upper() == "CYPRESS")
            if not ok:
                return self._send(
                    "<html><body>No parcels matched your search.</body></html>", sid, is_new)

            boxes = ["<input type='checkbox' id='CheckAll' name='CheckAll' value='on'/> All<br/>"]
            for p in parcels:
                boxes.append(
                    f"<input type='checkbox' id='{p['dom_id']}' name='{p['id']}' "
                    f"value='{p['label']}'/> {p['label']}<br/>")
            boxes.append("<input type='submit' name='btnNext2' id='btnNext2' value='Continue'/>")
            action = f"{IDIS}/DocumentSearch.aspx?SearchType=DCMT_ASSR"
            return self._send(_page("".join(boxes), vs, action), sid, is_new)

        # Step 2 -> results for the selected parcel
        if "btnNext2" in form:
            selected = [v for k, vals in form.items()
                        if k.startswith("chkAddress") for v in vals]
            if not selected:
                return self._send(
                    "<html><body>Please select a parcel.</body></html>", sid, is_new)
            STATE.searched.add(sid)
            return self._send(_results_page(selected[0], 1, vs), sid, is_new)

        # Pagination postback
        if "PageNo" in form:
            STATE.searched.add(sid)
            return self._send(
                _results_page(one("SelectedLabel"), int(one("PageNo", "1")), vs), sid, is_new)

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
        STATE.stale_rejections = 0
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
