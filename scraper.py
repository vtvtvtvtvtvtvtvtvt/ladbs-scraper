import asyncio
import logging
import os
import re
import time
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

BASE_URL = "https://ladbsdoc.lacity.org/IDISPublic_Records/idis"
MAIN_URL = "https://ladbsdoc.lacity.org"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
NAV_TIMEOUT = 30000
SETTLE_SECONDS = float(os.environ.get("LADBS_SETTLE_SECONDS", "1.5"))
MAX_RESULT_PAGES = 25

# A scrape must always answer, even on a parcel with hundreds of documents.
# Past this many seconds it stops collecting and returns what it has, flagged
# as truncated, rather than letting the caller time out with nothing.
SCRAPE_BUDGET_SECONDS = float(os.environ.get("SCRAPE_TIMEOUT_SECONDS", "150"))

# Detail pages are independent GETs, and a busy parcel has hundreds of them.
# Fetched one at a time they dominate the whole scrape; a small pool cuts that
# to a fraction while staying polite enough not to trip upstream throttling.
DETAIL_CONCURRENCY = int(os.environ.get("LADBS_DETAIL_CONCURRENCY", "6"))

# Walking rows one at a time is the only selection strategy the live site
# honours: a combined multi-checkbox submit was observed returning 2 records
# where the per-row walk returned 28. This caps how many rows one scrape will
# walk; past it the result is returned partial rather than run forever.
FULL_WALK_MAX_ROWS = int(os.environ.get("LADBS_FULL_WALK_MAX_ROWS", "30"))

# Text that means the upstream refused us rather than "this parcel is empty".
# An empty result caused by one of these is a failure and must be reported as
# one; a genuinely permit-free parcel must stay quiet.
BLOCK_SIGNS = (
    "access denied", "forbidden", "request blocked", "request unsuccessful",
    "incapsula", "cloudflare", "unusual traffic", "rate limit", "too many requests",
    "session has expired", "session expired", "sessionexpired",
    "an error has occurred", "service unavailable", "temporarily unavailable",
)

DIRECTIONS = {
    "N", "S", "E", "W", "NE", "NW", "SE", "SW",
    "NORTH", "SOUTH", "EAST", "WEST",
}
STREET_SUFFIXES = {
    "AVE", "AVENUE", "ST", "STREET", "BLVD", "BOULEVARD", "RD", "ROAD",
    "DR", "DRIVE", "LN", "LANE", "PL", "PLACE", "CT", "COURT", "WAY",
    "TER", "TERRACE", "CIR", "CIRCLE", "PKWY", "PARKWAY", "HWY", "HIGHWAY",
    "TRL", "TRAIL", "PLZ", "PLAZA", "SQ", "SQUARE", "LOOP", "WALK", "PATH",
    "ALY", "ALLEY", "MALL",
}
UNIT_MARKERS = {"UNIT", "APT", "STE", "SUITE", "#", "NO"}

# LADBS holds records for the City of Los Angeles only. These LA County cities
# run their own building departments, so IDIS will never have anything for them.
OTHER_JURISDICTIONS = {
    "PASADENA", "SOUTH PASADENA", "GLENDALE", "BURBANK", "SANTA MONICA",
    "BEVERLY HILLS", "WEST HOLLYWOOD", "CULVER CITY", "INGLEWOOD", "TORRANCE",
    "LONG BEACH", "ALHAMBRA", "SAN MARINO", "EL SEGUNDO", "MANHATTAN BEACH",
    "HERMOSA BEACH", "REDONDO BEACH", "GARDENA", "HAWTHORNE", "COMPTON",
    "CARSON", "VERNON", "COMMERCE", "MONTEBELLO", "MONTEREY PARK",
    "SAN FERNANDO", "CALABASAS", "AGOURA HILLS", "MALIBU", "SANTA CLARITA",
    "POMONA", "DOWNEY", "NORWALK", "WHITTIER", "LAKEWOOD", "BELLFLOWER",
    "PARAMOUNT", "LYNWOOD", "SOUTH GATE", "HUNTINGTON PARK", "MAYWOOD",
    "BELL", "BELL GARDENS", "CUDAHY", "GLENDORA", "ARCADIA", "SIERRA MADRE",
    "TEMPLE CITY", "ROSEMEAD", "SAN GABRIEL", "DUARTE", "MONROVIA", "AZUSA",
    "COVINA", "WEST COVINA", "BALDWIN PARK", "EL MONTE", "SOUTH EL MONTE",
    "PICO RIVERA", "SANTA FE SPRINGS", "LA MIRADA", "CERRITOS", "ARTESIA",
    "SIGNAL HILL", "LAWNDALE", "LOMITA", "PALOS VERDES ESTATES",
    "RANCHO PALOS VERDES", "ROLLING HILLS", "WALNUT", "DIAMOND BAR",
    "LA VERNE", "SAN DIMAS", "CLAREMONT", "IRWINDALE", "INDUSTRY",
    "PICO RIVERA", "HIDDEN HILLS", "WESTLAKE VILLAGE",
}


def looks_like_ain(value: str) -> bool:
    """True for '5467018015' or '5467-018-015' — an AIN sent in the address slot.

    Callers (the CRM among them) sometimes pass a parcel number where an
    address is expected; treating it as a street address searches for nothing.
    """
    if not value or re.search(r"[A-Za-z]", value):
        return False
    return len(re.sub(r"[^0-9]", "", value)) == 10


def detect_other_jurisdiction(address: str) -> str:
    """Name the city if the address is plainly outside the City of Los Angeles.

    Only the parts after the street line are examined, so a street called
    "San Fernando Road" is not mistaken for the City of San Fernando.
    """
    parts = [p.strip().upper() for p in (address or "").split(",")[1:]]
    for part in parts:
        if part in OTHER_JURISDICTIONS:
            return part.title()
    return ""


def parse_address(raw: str):
    """Split a street address into (house_number, street_name, direction).

    LADBS wants the bare street name: no house number, no directional prefix
    and no street-type suffix. "1234 S San Fernando Rd" -> ("1234", "SAN FERNANDO", "S").
    """
    street = (raw or "").strip().split(",")[0].strip()
    tokens = [t for t in re.split(r"\s+", street) if t]
    if not tokens:
        raise ValueError(f"Cannot parse address: {raw!r}")

    number = tokens[0]
    if not re.match(r"^\d", number):
        raise ValueError(f"Address must start with a house number: {raw!r}")
    number = re.sub(r"[^0-9]", "", number)

    rest = [t.upper().strip(".") for t in tokens[1:]]

    # Drop anything from a unit marker onward ("... UNIT 4", "... #201").
    for i, tok in enumerate(rest):
        if tok in UNIT_MARKERS or tok.startswith("#"):
            rest = rest[:i]
            break

    direction = ""
    if rest and rest[0] in DIRECTIONS:
        direction = rest[0][0] if len(rest[0]) > 2 else rest[0]
        rest = rest[1:]

    # Drop a trailing street-type suffix, but never the only word left.
    if len(rest) > 1 and rest[-1] in STREET_SUFFIXES:
        rest = rest[:-1]

    if not rest:
        raise ValueError(f"Cannot determine street name from: {raw!r}")

    return number, " ".join(rest), direction


def format_ain(ain: str) -> str:
    """Format AIN for LADBS search. LADBS expects format: XXXX-XXX-XXX"""
    ain = re.sub(r'[^0-9]', '', ain)  # strip all non-digits
    if len(ain) == 10:
        return f"{ain[0:4]}-{ain[4:7]}-{ain[7:10]}"
    return ain  # return as-is if not 10 digits


def split_ain(ain: str):
    """Split a 10-digit AIN into (book, page, parcel)."""
    digits = re.sub(r'[^0-9]', '', ain or "")
    if len(digits) != 10:
        raise ValueError(f"AIN must be 10 digits, got: {digits!r}")
    return digits[0:4], digits[4:7], digits[7:10]


# Document GUIDs as LADBS writes them: {8-4-4-4-12}, sometimes several per row.
IMAGE_GUID_RE = re.compile(r"\{[0-9a-fA-F][0-9a-fA-F\-]{7,}\}")

# Attributes anywhere in a row that can carry an image reference.
LINK_ATTRS = ("href", "onclick", "src", "value", "data-docids")

# Substrings marking a link as the one that opens a document image.
IMAGE_LINK_HINTS = ("imagemain", "docids", "stpdfviewer", "openimage",
                    "viewimage", "imagetoopen")


def detail_image_guids(html: str) -> list:
    """Document ids on a record's detail page.

    Most result rows show an image icon but carry no id in the grid — the id
    lives on the record's own page. Links that name the image viewer are
    preferred; any id on the page is accepted as a fallback.
    """
    soup = BeautifulSoup(html, "html.parser")
    preferred, other = [], []
    for el in soup.find_all(True):
        for attr in LINK_ATTRS:
            value = el.get(attr)
            if not isinstance(value, str):
                continue
            found = IMAGE_GUID_RE.findall(value)
            if not found:
                continue
            bucket = (preferred if any(h in value.lower() for h in IMAGE_LINK_HINTS)
                      else other)
            for guid in found:
                if guid not in bucket:
                    bucket.append(guid)
    if preferred:
        return preferred
    if other:
        return other
    # Nothing in an attribute: fall back to any id in the markup.
    seen = []
    for guid in IMAGE_GUID_RE.findall(html):
        if guid not in seen:
            seen.append(guid)
    return seen


def row_image_guids(row, primary_guid: str) -> list:
    """Every document GUID referenced anywhere in a result row.

    The document link's OpenWindow() call carries a Hidden/Visible flag, but
    that only says whether the image pane opens expanded — it does NOT say
    whether an image exists. Gating on it dropped the image for every row that
    happened to default to Hidden, which is most of them.
    """
    guids = []

    def add(text):
        for guid in IMAGE_GUID_RE.findall(text or ""):
            if guid not in guids:
                guids.append(guid)

    add(primary_guid)
    for el in row.find_all(True):
        for attr in LINK_ATTRS:
            value = el.get(attr)
            if isinstance(value, str):
                add(value)

    # A GUID without the usual braces still identifies a document.
    if not guids and primary_guid and primary_guid.strip():
        guids.append(primary_guid.strip())
    return guids


def _element_hidden(el) -> bool:
    style = (el.get("style") or "").lower().replace(" ", "")
    return "visibility:hidden" in style or "display:none" in style


def row_has_image_icon(row) -> bool:
    """Does the row VISIBLY show an image icon?

    LADBS renders the icon element on every row and hides it with CSS when the
    record has no digital image — counting icons in the markup instead of
    icons a person can see reported 15 where the page shows 4.
    """
    for img in row.find_all("img"):
        src = (img.get("src") or "").lower()
        alt = (img.get("alt") or "").lower()
        if not any(w in src or w in alt
                   for w in ("image", "camera", "doc", "pdf", "view", "tif")):
            continue
        if _element_hidden(img):
            continue
        ancestor = img.parent
        hidden = False
        while ancestor is not None and ancestor is not row:
            if _element_hidden(ancestor):
                hidden = True
                break
            ancestor = ancestor.parent
        if not hidden:
            return True
    return False


def parse_results_html(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    records = []

    grid = soup.find("table", id="grdIdisResult")
    if not grid:
        logger.warning("grdIdisResult table not found")
        return records

    rows = grid.find_all("tr")
    logger.info(f"Grid rows: {len(rows)}")

    for i, row in enumerate(rows[1:]):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        doc_link = cells[1].find("a")
        if not doc_link:
            continue

        href = doc_link.get("href", "")
        m = re.search(r"OpenWindow\('(\d+)','(Hidden|Visible)','([^']*)'\)", href, re.I)
        if not m:
            continue

        record_id = m.group(1)
        image_visible = m.group(2) == "Visible"
        image_guid = m.group(3)

        doc_type = doc_link.get_text(strip=True)
        sub_type = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        doc_date = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        doc_number = cells[4].get_text(strip=True) if len(cells) > 4 else ""

        comment_input = row.find("input", id=re.compile(r"hidComments"))
        comments = comment_input.get("value", "") if comment_input else ""

        guids = row_image_guids(row, image_guid)
        has_icon = row_has_image_icon(row)
        digital_image_url = None
        if guids:
            digital_image_url = (
                f"{BASE_URL}/ImageMain.aspx?DocIds={','.join(guids)}")

        # Image=Visible so the page renders its document link; Hidden asks
        # LADBS to leave it out, which is where the id would have come from.
        detail_url = (f"{BASE_URL}/Report.aspx?Record_Id={record_id}"
                      f"&Image=Visible&ImageToOpen=")

        record = {
            "record_id": record_id,
            "doc_type": doc_type,
            "sub_type": sub_type,
            "doc_date": doc_date,
            "doc_number": doc_number,
            "comments": comments,
            "detail_url": detail_url,
            "digital_image_url": digital_image_url,
            "has_digital_image": bool(guids),
            # Whether the image pane opens expanded — not whether one exists.
            "image_pane_visible": image_visible,
            # An icon with no GUID we could extract means this parser is
            # missing something; surfaced so it cannot go unnoticed.
            "image_icon_in_row": has_icon,
            "attachments": [],
        }

        if digital_image_url:
            record["image_source"] = "grid"
            record["attachments"].append({
                "label": f"Digital Image - {doc_type} {doc_number}",
                "url": digital_image_url,
                "type": "digital_image",
            })

        records.append(record)
        logger.info(f"  {doc_type} | {sub_type} | {doc_date} | {doc_number} | "
                    f"img={bool(guids)} icon={has_icon}")

    return records


# Column headings that identify the parcel-selection grid. The page also
# carries unrelated "Display Fields" checkboxes (All Fields, Frac, Unit, Zip
# Code) and an "All" toggle; treating those as parcels means submitting a
# display option instead of an address.
PARCEL_HEADERS = ("beg nbr", "str name", "str type", "end nbr")

# Checkbox labels that are page controls, never parcels.
NON_PARCEL_LABELS = (
    "all fields", "frac", "unit", "zip code", "all", "checkall", "check all",
)


def find_parcel_table(soup):
    """The grid of matching addresses, identified by its column headings.

    LADBS nests tables for layout, and find_all returns the outermost first —
    matching on a joined string picks the page wrapper, whose rows include the
    Display Fields controls. Match heading cells exactly and keep the
    innermost table that qualifies.
    """
    candidates = []
    for table in soup.find_all("table"):
        headings = {cell.get_text(strip=True).lower()
                    for cell in table.find_all(["th", "td"], limit=14)}
        if "select" in headings and headings & set(PARCEL_HEADERS):
            candidates.append(table)
    if not candidates:
        return None
    # The innermost qualifying table is the grid itself.
    return min(candidates, key=lambda t: len(t.find_all("table")))


def unresolved_image_row(html: str):
    """The first result row showing an image icon that yields no document id.

    When the icon count outruns the extracted ids, this is the markup that
    explains why — captured automatically so the gap does not need a
    reproduction to diagnose.
    """
    soup = BeautifulSoup(html, "html.parser")
    grid = soup.find("table", id="grdIdisResult")
    if not grid:
        return None
    for row in grid.find_all("tr"):
        if row.find("th"):
            continue
        if row_has_image_icon(row) and not row_image_guids(row, ""):
            return str(row)[:2500]
    return None


def parse_checkboxes(html: str) -> list:
    """The address checkboxes on the LADBS selection page — those only.

    An address can match several rows that differ just by direction: a search
    for "234 Museum Dr" returns both "234 MUSEUM DR" and "234 W MUSEUM DR",
    and taking only one silently halves the results.
    """
    soup = BeautifulSoup(html, "html.parser")

    table = find_parcel_table(soup)
    if table:
        pairs = []
        for row in table.find_all("tr"):
            cb = row.find("input", {"type": "checkbox"})
            if not cb or not cb.get("name"):
                continue
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            # An address row carries a street number; the Display Fields row
            # and the All toggle do not.
            if not any(re.search(r"\d", c) for c in cells):
                continue
            label = " ".join(c for c in cells if c) or (cb.get("value") or "")
            pairs.append({
                "name": cb.get("name", ""),
                "value": cb.get("value", "") or "",
                "id": cb.get("id", "") or "",
                "label": re.sub(r"\s+", " ", label).strip(),
            })
        if pairs:
            return pairs
        logger.warning("parcel table found but it held no checkboxes")

    # No recognisable grid: fall back to every checkbox that is not obviously
    # a page control.
    logger.warning("no parcel table found; falling back to loose checkbox scan")
    pairs = []
    for cb in soup.find_all("input", {"type": "checkbox"}):
        name = cb.get("name", "") or ""
        cb_id = cb.get("id", "") or ""
        if not name:
            continue
        # Compare on letters only, so "All Fields", "AllFields" and
        # "all_fields" are all recognised as the same page control.
        def squash(text):
            return re.sub(r"[^a-z]", "", (text or "").lower())

        if "checkall" in squash(f"{name}{cb_id}"):
            continue
        controls = {squash(c) for c in NON_PARCEL_LABELS}
        if squash(name) in controls or squash(cb_id) in controls:
            continue
        pairs.append({"name": name, "value": cb.get("value", "") or "",
                      "id": cb_id, "label": cb.get("value", "") or name})
    return pairs


class LADBSScraper:
    """Drives the LADBS IDIS ASP.NET WebForms site in a real browser session.

    Every step (search, parcel selection, paging, detail pages) happens in the
    same Playwright context, so the ASP.NET session cookie, __VIEWSTATE and
    __EVENTVALIDATION are always the ones the server just issued. Replaying
    those tokens out-of-band is what made earlier versions fail intermittently.
    """

    def __init__(self, headless: bool = True, budget_seconds: float = None,
                 parcel_mode: str = "auto", debug: bool = False):
        # debug=True returns the raw results-grid and pager markup in
        # diagnostics, so a parsing gap can be diagnosed from one call
        # instead of guessing at the page's structure.
        self.debug = debug
        self.headless = headless
        # "auto": submit every address row together and, when there are only a
        #         few, also walk them one by one and merge.
        # "all":  one combined submit only.
        # "each": one search per address row only.
        self.parcel_mode = (parcel_mode if parcel_mode in ("auto", "all", "each")
                            else "auto")
        self.budget_seconds = (
            SCRAPE_BUDGET_SECONDS if budget_seconds is None else budget_seconds)
        self._page = None
        self._context = None
        self._diag = None
        self._deadline = None

    # ---------------- public API ----------------

    async def scrape(self, address: str, include_details: bool = True) -> dict:
        """Search by street address (or by AIN, if that is what was passed)."""
        if looks_like_ain(address):
            logger.info(f"{address!r} is an AIN, not an address — searching by AIN")
            result = await self.scrape_by_ain(address, include_details=include_details)
            result["address"] = address
            result["diagnostics"]["routed"] = (
                "value looked like an AIN, so the AIN search was used")
            return result

        number, street_name, direction = parse_address(address)
        logger.info(f"Parsed: number={number} street={street_name!r} dir={direction!r}")

        async def worker():
            self._diag["parsed_address"] = {
                "number": number, "street": street_name, "direction": direction,
            }
            outside = detect_other_jurisdiction(address)
            if outside:
                self._diag["outside_jurisdiction"] = outside
                self._warn(f"{outside} is not covered by LADBS (City of LA only)")
            records = await self._collect_records(
                lambda: self._start_address_search(number, street_name, direction)
            )
            return await self._finish(
                records, "address", address, f"{address}", include_details)

        return await self._run(worker)

    async def scrape_all(self, ain: str = None, address: str = None,
                         include_details: bool = True,
                         expand_directions: bool = True) -> dict:
        """Search by every identifier given, in one session, and merge.

        An AIN resolves to one assessor parcel, but LADBS files documents
        against address records, and a property can hold several that differ
        only by direction — "234 MUSEUM DR" and "234 W MUSEUM DR". The
        assessor search surfaces one of them; the address search surfaces all.
        Running only the AIN, as a caller supplying both would get, silently
        returns a fraction of the documents.
        """
        if not ain and address and looks_like_ain(address):
            ain, address = address, None
        if address and looks_like_ain(address):
            address = None          # same value as the AIN; no second search
        if not ain and not address:
            raise ValueError("scrape_all needs an ain or an address")

        async def worker():
            self._diag["searches"] = []
            collected = []

            if ain:
                book, pg, parcel = split_ain(ain)
                found = await self._collect_records(
                    lambda: self._start_ain_search(book, pg, parcel))
                self._note_search("ain", format_ain(ain), found)
                collected.extend(found)

            if address:
                try:
                    number, street_name, direction = parse_address(address)
                except ValueError as e:
                    self._warn(f"could not parse address {address!r}: {e}")
                else:
                    outside = detect_other_jurisdiction(address)
                    if outside:
                        self._diag["outside_jurisdiction"] = outside
                    found = await self._collect_records(
                        lambda: self._start_address_search(
                            number, street_name, direction))
                    self._note_search("address", address, found)
                    collected.extend(found)

                    if expand_directions:
                        # The same street number can exist with and without a
                        # directional prefix — 2100, 2100 N and 2100 W Cypress
                        # are separate address records, and a search without
                        # the direction resolves to just one of them. Search
                        # each direction outright; a miss returns quickly, a
                        # variant that lands on an already-walked parcel is
                        # skipped, and anything new is merged in.
                        for variant in ("", "N", "S", "E", "W"):
                            if variant == direction:
                                continue    # the base search covered this one
                            if self._time_left() < 20:
                                self._diag["truncated"] = True
                                self._warn("time budget low; remaining "
                                           "direction variants skipped")
                                break
                            label = f"{number} {variant} {street_name}".replace(
                                "  ", " ").strip()
                            found = await self._collect_records(
                                lambda v=variant: self._start_address_search(
                                    number, street_name, v))
                            self._note_search("address_variant", label, found)
                            collected.extend(found)

            merged = self._dedupe(collected)
            self._step(f"merged {len(collected)} record(s) from "
                       f"{len(self._diag['searches'])} search(es) -> {len(merged)} unique")

            identifier = " + ".join(
                s["query"] for s in self._diag["searches"]) or (ain or address)
            result = await self._finish(
                merged, "ain" if ain else "address", ain or address,
                identifier, include_details)
            if ain:
                result["ain"] = ain
            if address:
                result["address"] = address
            return result

        return await self._run(worker)

    def _note_search(self, kind, query, records):
        """Record what one search contributed, so a thin result is traceable."""
        self._diag["searches"].append({
            "type": kind,
            "query": query,
            "address_rows": self._diag.pop("parcels", []),
            # What each address row contributed on this leg.
            "steps": self._diag.pop("strategy", []),
            "records": len(records),
        })

    async def scrape_by_ain(self, ain: str, include_details: bool = True) -> dict:
        """Search by Assessor Identification Number (APN)."""
        book, pg, parcel = split_ain(ain)
        logger.info(f"AIN split: book={book} page={pg} parcel={parcel}")

        async def worker():
            self._diag["parsed_ain"] = {"book": book, "page": pg, "parcel": parcel}
            records = await self._collect_records(
                lambda: self._start_ain_search(book, pg, parcel)
            )
            return await self._finish(
                records, "ain", ain, f"AIN {format_ain(ain)}", include_details)

        return await self._run(worker)

    # ---------------- browser plumbing ----------------

    async def _run(self, worker):
        started = time.monotonic()
        self._deadline = started + self.budget_seconds
        self._diag = {
            "steps": [], "warnings": [], "checkboxes_found": 0, "result_pages": 0,
            "truncated": False, "time_budget_seconds": self.budget_seconds,
        }
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = await browser.new_context(user_agent=USER_AGENT)
            context.set_default_timeout(NAV_TIMEOUT)
            self._context = context
            self._walked_selections = set()
            self._page = await context.new_page()
            try:
                return await worker()
            finally:
                self._diag["elapsed_seconds"] = round(time.monotonic() - started, 1)
                await browser.close()
                self._page = None
                self._context = None

    @staticmethod
    def _address_grid_sample(html):
        soup = BeautifulSoup(html, "html.parser")
        table = find_parcel_table(soup)
        if table:
            return str(table)[:2500]
        # No recognisable grid: keep the checkboxes and their surroundings.
        boxes = soup.find_all("input", {"type": "checkbox"})
        return " ".join(str(b.parent)[:300] for b in boxes[:8])[:2500] or None

    def _capture_markup(self, html):
        """Keep the markup behind any result that does not add up."""
        soup = BeautifulSoup(html, "html.parser")

        # Always keep the pager: it is small, and page counts are the thing
        # most often disputed.
        nav = soup.find(id="pnlNavigate")
        if nav and "pager_html" not in self._diag:
            self._diag["pager_html"] = str(nav)[:1500]

        # Always keep one row that shows an icon but yields no document id.
        if "image_row_sample" not in self._diag:
            sample = unresolved_image_row(html)
            if sample:
                self._diag["image_row_sample"] = sample

        if not self.debug or "grid_html_sample" in self._diag:
            return
        grid = soup.find(id="grdIdisResult")
        if grid:
            rows = grid.find_all("tr")
            self._diag["grid_html_sample"] = "".join(str(r) for r in rows[:6])[:6000]
            self._diag["grid_row_count"] = len(rows)
    async def _snapshot(self) -> dict:
        """Describe the page the scrape ended on.

        When nothing is found the question is always "what did LADBS actually
        send back?" — a session-expired notice, a no-match page, or a redesign
        the selectors no longer match. The field inventory answers the last
        one: if LADBS renames its inputs, the new names show up here.
        """
        try:
            html = await self._page.content()
            url = self._page.url
        except Exception as e:
            return {"error": f"could not read the page: {e}"}

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()

        fields = []
        for el in soup.find_all(["input", "select", "textarea"]):
            name = el.get("name") or el.get("id")
            if name and "__" not in name:  # skip __VIEWSTATE and friends
                fields.append(f"{el.name}[{el.get('type', '')}] {name}")

        return {
            "url": url,
            "title": soup.title.get_text(strip=True) if soup.title else "",
            "has_results_grid": soup.find(id="grdIdisResult") is not None,
            "html_length": len(html),
            "form_fields": fields[:40],
            "visible_text": re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:1500],
        }

    def _time_left(self) -> float:
        return float("inf") if self._deadline is None else self._deadline - time.monotonic()

    def _out_of_time(self, what: str) -> bool:
        if self._time_left() > 0:
            return False
        self._diag["truncated"] = True
        self._warn(f"{self.budget_seconds:.0f}s time budget spent — {what}")
        return True

    def _step(self, msg):
        logger.info(msg)
        self._diag["steps"].append(msg)

    def _warn(self, msg):
        logger.warning(msg)
        self._diag["warnings"].append(msg)

    async def _goto(self, url, settle: float = None):
        """settle=None means the module default, so it stays tunable at runtime."""
        settle = SETTLE_SECONDS if settle is None else settle
        self._step(f"goto {url}")
        timeout = max(5000, min(NAV_TIMEOUT, int(self._time_left() * 1000)))
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:
            self._warn(f"goto failed for {url}: {e}")
        if settle:
            await asyncio.sleep(settle)

    async def _adopt_popup(self):
        """LADBS sometimes answers a postback in a new window; follow it."""
        pages = [p for p in self._context.pages if not p.is_closed()]
        if len(pages) > 1 and pages[-1] is not self._page:
            self._page = pages[-1]
            self._step(f"switched to popup {self._page.url}")

    async def _fill_first(self, selectors, value, label):
        for sel in selectors:
            try:
                loc = self._page.locator(sel).first
                if await loc.count() == 0:
                    continue
                await loc.fill(value, timeout=5000)
                self._step(f"filled {label} ({sel}) = {value}")
                return True
            except Exception as e:
                self._warn(f"fill {label} via {sel} failed: {e}")
        self._warn(f"no field matched for {label}; tried {selectors}")
        self._diag["search_form_missing"] = True
        return False

    async def _click_first(self, selectors, label, required=True):
        for sel in selectors:
            try:
                loc = self._page.locator(sel).first
                if await loc.count() == 0:
                    continue
            except Exception:
                continue
            self._step(f"click {label} ({sel})")
            await self._click_and_wait(loc)
            await self._adopt_popup()
            return True
        msg = f"no clickable element for {label}; tried {selectors}"
        if required:
            self._warn(msg)
        return False

    async def _click_and_wait(self, locator):
        """Click a WebForms control and wait for the resulting postback."""
        try:
            async with self._page.expect_navigation(
                wait_until="domcontentloaded", timeout=NAV_TIMEOUT
            ):
                await locator.click(timeout=NAV_TIMEOUT)
        except PlaywrightTimeoutError:
            # Some controls update in place instead of navigating.
            try:
                await self._page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
        except Exception as e:
            self._warn(f"click failed: {e}")
        await asyncio.sleep(SETTLE_SECONDS)

    # ---------------- search entry points ----------------

    async def _start_ain_search(self, book, pg, parcel):
        await self._goto(MAIN_URL)
        await self._goto(f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ASMT")

        await self._fill_first(
            ["input[name='Assessor$txtAssessorNoBook']", "input[id*='AssessorNoBook']"],
            book, "assessor book")
        await self._fill_first(
            ["input[name='Assessor$txtAssessorNoPage']", "input[id*='AssessorNoPage']"],
            pg, "assessor page")
        await self._fill_first(
            ["input[name='Assessor$txtAssessorNoParcel']", "input[id*='AssessorNoParcel']"],
            parcel, "assessor parcel")

        await self._click_first(
            ["input[name='btnSearchAssessor']",
             "input[id='btnSearchAssessor']",
             "input[type='submit'][value='Search']",
             "input[type='submit']"],
            "assessor search")
        self._step(f"after assessor search: {self._page.url}")

    async def _start_address_search(self, number, street_name, direction):
        await self._goto(MAIN_URL)
        await self._goto(f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR")

        await self._fill_first(
            ["input[name='Address$txtAddressBegNo']", "input[id*='AddressBegNo']"],
            number, "house number")
        await self._fill_first(
            ["input[name='Address$txtAddressStreetName']", "input[id*='AddressStreetName']"],
            street_name, "street name")
        if direction:
            await self._fill_first(
                ["input[name='Address$txtAddressDirection']", "input[id*='AddressDirection']"],
                direction, "direction")

        await self._click_first(
            ["input[name='btnNext1']",
             "input[id='btnNext1']",
             "input[type='submit'][value='Next']",
             "input[type='submit']"],
            "address search")
        self._step(f"after address search: {self._page.url}")

    # ---------------- selection / results ----------------

    async def _collect_records(self, restart):
        """Run the search, then walk every parcel selection the site offers."""
        await restart()
        html = await self._page.content()

        direct = parse_results_html(html)
        if direct:
            self._step(f"search landed straight on results: {len(direct)} record(s)")
            records = direct + await self._paginate()
            return self._dedupe(records)

        checkboxes = parse_checkboxes(html)
        if checkboxes:
            signature = tuple(sorted(
                (c["name"], c.get("value", "")) for c in checkboxes))
            if signature in self._walked_selections:
                self._step("this search resolved to a selection already walked "
                           "by an earlier leg; skipping the repeat")
                self._diag.setdefault("strategy", []).append(
                    {"step": "duplicate_parcel",
                     "rows": len(checkboxes)})
                return []
            self._walked_selections.add(signature)
        self._diag["checkboxes_found"] = len(checkboxes)
        # Recorded here so it is reported whatever parcel_mode is in use.
        self._diag["parcels"] = [c.get("label") or c["name"] for c in checkboxes]
        self._diag["address_rows_found"] = len(checkboxes)
        if checkboxes or "selection_page" not in self._diag:
            self._diag["selection_page"] = {
                "checkboxes": [{"name": c["name"], "id": c.get("id", ""),
                                "label": c.get("label", c.get("value", ""))}
                               for c in checkboxes],
                "sample": self._address_grid_sample(html),
            }

        self._step(f"selection page has {len(checkboxes)} address row(s): "
                   f"{[c.get('label') or c['name'] for c in checkboxes]}")

        if not checkboxes:
            self._warn(
                f"no results grid and no parcel checkboxes at {self._page.url} "
                f"(page length {len(html)})"
            )
            self._diag["final_html_len"] = len(html)
            return []

        return self._dedupe(await self._gather(checkboxes, restart))

    async def _gather(self, rows, restart):
        """Collect records for every matching selection row.

        Live LADBS honours one checkbox per submit: ticking several and
        submitting once was observed to return a fraction of what the same
        rows return walked individually. So "auto" (default) and "each" walk
        every row; "all" keeps the single combined submit for explicit use,
        falling back to the walk when it returns nothing.
        """
        mode = self.parcel_mode
        collected = []
        self._diag.setdefault("strategy", [])
        self._diag["parcel_mode"] = mode
        page_is_fresh = True

        if mode == "all" and len(rows) > 1:
            self._step(f"submitting all {len(rows)} row(s) together")
            found = await self._submit_rows(rows)
            self._diag["strategy"].append(
                {"step": "combined", "rows": len(rows), "records": len(found)})
            collected.extend(found)
            if found:
                return collected
            self._warn("combined submit returned nothing; walking rows individually")
            page_is_fresh = False

        walk = rows[:FULL_WALK_MAX_ROWS]
        if len(rows) > len(walk):
            self._diag["truncated"] = True
            self._warn(f"walking the first {len(walk)} of {len(rows)} rows")
        collected.extend(await self._walk_rows(walk, restart, page_is_fresh))
        return collected

    async def _walk_rows(self, rows, reopen, page_is_fresh, depth=0, prefix=""):
        """Select each row on its own and collect what it returns.

        A selection can land on another selection page instead of results —
        the address list leads to a per-parcel identifier list (assessor
        number, address range, legal description). Those sub-rows are walked
        too, re-selecting the parent row to get back to them each time.
        """
        out = []
        ticked = 0
        for idx, cb in enumerate(rows):
            if self._out_of_time(
                    f"stopped after {idx} of {len(rows)} row(s){prefix}"):
                break
            if idx > 0 or not page_is_fresh:
                await reopen()
            label = prefix + (cb.get("label") or cb["name"])
            self._step(f"selecting {idx + 1}/{len(rows)}: {label}")
            if not await self._check_and_continue(cb):
                continue
            ticked += 1
            html = await self._page.content()
            self._capture_markup(html)
            found = parse_results_html(html)
            if found:
                found += await self._paginate()
                self._step(f"  {label} yielded {len(found)} record(s)")
                self._diag["strategy"].append(
                    {"step": "row", "row": label, "records": len(found)})
                out.extend(found)
                continue

            subrows = parse_checkboxes(html)
            same_page = {c["name"] for c in subrows} == {c["name"] for c in rows}
            if subrows and not same_page and depth < 2:
                self._step(f"  {label} led to {len(subrows)} sub-selection(s)")
                self._diag["strategy"].append(
                    {"step": "sub_selection", "row": label, "rows": len(subrows)})

                async def reopen_sub(parent=cb):
                    await reopen()
                    await self._check_and_continue(parent)

                out.extend(await self._walk_rows(
                    subrows, reopen_sub, page_is_fresh=True,
                    depth=depth + 1, prefix=f"{label} > "))
            else:
                self._diag["strategy"].append(
                    {"step": "row", "row": label, "records": 0})
                self._warn(f"{label}: no results grid and no onward selection")
        if depth == 0:
            self._diag["address_rows_checked"] = ticked
        return out

    async def _submit_rows(self, rows):
        """Tick every address row — via the page's All control where possible
        — confirm the ticks landed, then submit once."""
        if not await self._select_every_row(rows):
            return []
        if not await self._continue_to_documents():
            return []
        html = await self._page.content()
        self._capture_markup(html)
        return parse_results_html(html) + await self._paginate()

    async def _select_every_row(self, rows):
        """Check all address rows and verify, rather than assume, that they are.

        The page offers an "All" toggle; use it first, since that is what a
        person clicks and it carries whatever scripting the site expects.
        """
        used_all_control = await self._tick_all_control()
        checked = await self._count_checked(rows)

        if checked < len(rows):
            if used_all_control:
                self._step(f"All control checked {checked}/{len(rows)}; "
                           f"ticking the remaining rows directly")
            for cb in rows:
                loc = self._locate_box(cb)
                if loc is None:
                    continue
                try:
                    if not await loc.is_checked():
                        await loc.check(timeout=5000)
                except Exception as e:
                    self._warn(f"could not tick {cb.get('label') or cb['name']}: {e}")
            checked = await self._count_checked(rows)

        self._diag["address_rows_found"] = len(rows)
        self._diag["address_rows_checked"] = checked
        self._diag["used_all_control"] = used_all_control

        if checked == 0:
            self._warn("no address row could be ticked")
            return False
        if checked < len(rows):
            self._warn(f"only {checked} of {len(rows)} address rows were ticked")
        else:
            self._step(f"all {checked} address row(s) ticked")
        return True

    async def _tick_all_control(self) -> bool:
        """The page's own "All" checkbox, which selects every row at once."""
        for sel in ('input[type="checkbox"][id="All"]',
                    'input[type="checkbox"][name="All"]',
                    'input[type="checkbox"][id="CheckAll"]',
                    'input[type="checkbox"][name="CheckAll"]',
                    'input[type="checkbox"][id*="chkAll" i]',
                    'input[type="checkbox"][id*="SelectAll" i]'):
            try:
                loc = self._page.locator(sel).first
                if await loc.count() == 0:
                    continue
                await loc.check(timeout=5000)
                self._step(f"clicked the page's All control ({sel})")
                await asyncio.sleep(0.3)   # let its script tick the rows
                return True
            except Exception as e:
                self._warn(f"All control {sel} failed: {e}")
        return False

    def _locate_box(self, cb):
        for sel in self._box_selectors(cb):
            try:
                loc = self._page.locator(sel).first
                if loc:
                    return loc
            except Exception:
                continue
        return None

    async def _count_checked(self, rows) -> int:
        checked = 0
        for cb in rows:
            for sel in self._box_selectors(cb):
                try:
                    loc = self._page.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    if await loc.is_checked():
                        checked += 1
                    break
                except Exception:
                    continue
        return checked

    @staticmethod
    def _box_selectors(cb):
        selectors = []
        if cb.get("id"):
            selectors.append(f'input[type="checkbox"][id="{cb["id"]}"]')
        if cb.get("value"):
            selectors.append(
                f'input[type="checkbox"][name="{cb["name"]}"][value="{cb["value"]}"]')
        selectors.append(f'input[type="checkbox"][name="{cb["name"]}"]')
        return selectors

        return self._dedupe(all_records)

    async def _check_box(self, cb) -> bool:
        for sel in self._box_selectors(cb):
            try:
                loc = self._page.locator(sel).first
                if await loc.count() == 0:
                    continue
                await loc.check(timeout=5000)
                return True
            except Exception as e:
                self._warn(f"check {cb['name']} via {sel} failed: {e}")
        return False

    async def _continue_to_documents(self):
        return await self._click_first(
            ["input[name='btnNext2']",
             "input[id='btnNext2']",
             "input[type='submit'][value='Continue']",
             "input[name='btnSearch']",
             "input[type='submit']"],
            "continue to documents")

    async def _check_and_continue(self, cb):
        if not await self._check_box(cb):
            self._warn(f"could not check checkbox {cb['name']}")
            return False
        return await self._continue_to_documents()

    async def _paginate(self):
        """Walk every result page, including pagers that show a moving window.

        LADBS shows a few page numbers at a time plus a next/ellipsis control.
        Following only the numbered links stops at the end of the first window
        — three pages of five — and, worse, looks like a clean finish.
        """
        extra = []
        visited = {1}
        advertised = 1

        for _ in range(MAX_RESULT_PAGES):
            soup = BeautifulSoup(await self._page.content(), "html.parser")
            nav = soup.find(id="pnlNavigate")
            if not nav:
                break

            advertised = max(advertised, self._pages_advertised(nav))
            links = nav.find_all("a")

            target = None
            for a in links:
                text = a.get_text(strip=True)
                if text.isdigit() and int(text) not in visited:
                    target = text
                    break
            step_label = target
            if not target:
                step_label = self._next_control(links)
                if not step_label:
                    break

            if self._out_of_time(f"stopped before result page {step_label}"):
                break

            self._step(f"  result page {step_label}")
            clicked = await self._click_first(
                [f"#pnlNavigate a:text-is('{step_label}')"],
                f"result page {step_label}", required=False)
            if not clicked:
                self._warn(f"could not open result page {step_label}")
                break

            landed = self._current_page(await self._page.content())
            if landed is None:
                landed = int(target) if target and target.isdigit() else max(visited) + 1
            if landed in visited:
                self._warn(f"pager did not advance past page {landed}; stopping")
                break
            visited.add(landed)
            extra.extend(parse_results_html(await self._page.content()))

        self._diag["result_pages"] = max(self._diag["result_pages"], len(visited))
        self._diag["pages_advertised"] = max(
            self._diag.get("pages_advertised", 0), advertised)
        if advertised > len(visited):
            # Never report a partial read as a clean finish.
            self._diag["truncated"] = True
            self._warn(f"read {len(visited)} of {advertised} result page(s)")
        return extra

    @staticmethod
    def _pages_advertised(nav) -> int:
        """Highest page number the pager mentions, link or not."""
        highest = 1
        for el in nav.find_all(True):
            text = el.get_text(strip=True)
            if text.isdigit():
                highest = max(highest, int(text))
        return highest

    @staticmethod
    def _next_control(links):
        """The 'go forward a window' control, when no numbered link is left."""
        wanted = (">", ">>", "»", "next", "next >", "...", "\u2026")
        for a in links:
            text = a.get_text(strip=True)
            if text.lower() in wanted:
                return text
        return None

    @staticmethod
    def _current_page(html):
        """Which page the pager says we are on (rendered unlinked)."""
        soup = BeautifulSoup(html, "html.parser")
        nav = soup.find(id="pnlNavigate")
        if not nav:
            return None
        for el in nav.find_all(["span", "b", "strong", "font", "td"]):
            if el.find("a"):
                continue
            text = el.get_text(strip=True)
            if text.isdigit():
                return int(text)
        return None

    @staticmethod
    def _dedupe(records):
        seen = set()
        out = []
        for r in records:
            if r["record_id"] in seen:
                continue
            seen.add(r["record_id"])
            out.append(r)
        return out

    # ---------------- detail pages ----------------

    async def _finish(self, records, key, key_value, identifier,
                      include_details: bool = True):
        self._step(f"total unique records: {len(records)}")
        if not records:
            snapshot = await self._snapshot()
            self._diag["page_snapshot"] = snapshot
            status, reason = self._classify_empty(snapshot)
            summary = f"No records found for {identifier}."
            if status == "blocked":
                summary = (f"LADBS did not return results for {identifier}: {reason}. "
                           f"This is a failure, not an empty parcel — retry later.")
                self._warn(f"upstream refusal: {reason}")
            else:
                outside = self._diag.get("outside_jurisdiction")
                if outside:
                    summary += (
                        f" {outside} is outside the City of Los Angeles, and LADBS"
                        f" only holds records for properties inside the city — check"
                        f" the {outside} building department instead.")
            return {
                key: key_value,
                "status": status,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": summary,
                "diagnostics": self._diag,
            }

        icons = sum(1 for r in records if r.get("image_icon_in_row"))
        from_grid = sum(1 for r in records if r.get("has_digital_image"))

        if include_details:
            await self._fetch_details(records)
        else:
            self._step("detail pages skipped (include_details=false)")
            self._diag["details_fetched"] = False

        with_image = sum(1 for r in records if r.get("has_digital_image"))
        self._diag["rows_showing_image_icon"] = icons
        self._diag["records_with_image"] = with_image
        self._diag["image_ids_from_grid"] = from_grid
        self._diag["image_ids_from_detail"] = sum(
            1 for r in records if r.get("image_source") == "detail")

        if icons > with_image:
            if not include_details:
                self._warn(
                    f"{icons} row(s) show an image icon but only {with_image} "
                    f"carry a document id, and detail pages were skipped. Most "
                    f"ids are only on the record's own page — retry with "
                    f"include_details=true to collect them.")
            else:
                self._warn(
                    f"{icons} row(s) show an image icon but only {with_image} "
                    f"yielded a document id — image extraction is incomplete")

        all_attachments = []
        for rec in records:
            all_attachments.extend(rec.get("attachments", []))

        return {
            key: key_value,
            "status": "partial" if self._diag.get("truncated") else "ok",
            "total_records": len(records),
            "records": records,
            "attachments": all_attachments,
            "summary": self._build_summary(records, identifier),
            "diagnostics": self._diag,
        }

    def _classify_empty(self, snapshot):
        """Separate an upstream refusal from a parcel that truly has nothing."""
        haystack = " ".join([
            snapshot.get("visible_text", ""), snapshot.get("title", ""),
        ]).lower()
        for sign in BLOCK_SIGNS:
            if sign in haystack:
                return "blocked", f"upstream returned {sign!r}"
        if self._diag.get("search_form_missing"):
            return "blocked", "the LADBS search form was not on the page"
        return "no_records", ""

    async def _fetch_details(self, records):
        """Load every record's detail page, a few at a time.

        Serially this is the whole cost of a scrape: a parcel with 500 records
        spends minutes on round trips that do not depend on each other. A small
        pool of pages in the same browser context shares the session cookie, so
        concurrency costs nothing in correctness.
        """
        width = max(1, min(DETAIL_CONCURRENCY, len(records)))
        self._step(f"fetching {len(records)} detail page(s), {width} at a time")

        pool = asyncio.Queue()
        pages = []
        for _ in range(width):
            page = await self._context.new_page()
            pages.append(page)
            pool.put_nowait(page)

        skipped = {"n": 0}

        async def fetch(index, rec):
            if self._time_left() <= 0:
                skipped["n"] += 1
                rec["detail_error"] = "skipped: scrape time budget spent"
                return
            page = await pool.get()
            try:
                timeout = max(5000, min(NAV_TIMEOUT, int(self._time_left() * 1000)))
                await page.goto(rec["detail_url"], wait_until="domcontentloaded",
                                timeout=timeout)
                if "SessionExpired" in page.url or "IdisError" in page.url:
                    rec["detail_error"] = "Session expired"
                else:
                    detail_html = await page.content()
                    rec.update(self._parse_detail_html(detail_html))
                    if not rec.get("has_digital_image"):
                        self._adopt_detail_image(rec, detail_html)
                    if (rec.get("image_icon_in_row")
                            and not rec.get("has_digital_image")
                            and "detail_page_sample" not in self._diag):
                        # The grid showed an icon and the record's own page
                        # still yielded no id: keep that page's links as the
                        # evidence the image parser is missing.
                        self._diag["detail_page_sample"] = {
                            "url": rec["detail_url"],
                            "doc_number": rec.get("doc_number"),
                            "links": self._page_links(detail_html),
                        }
            except Exception as e:
                rec["detail_error"] = str(e)
            finally:
                pool.put_nowait(page)

        try:
            await asyncio.gather(*(fetch(i, r) for i, r in enumerate(records)))
        finally:
            for page in pages:
                try:
                    await page.close()
                except Exception:
                    pass

        if skipped["n"]:
            self._diag["truncated"] = True
            self._warn(f"time budget spent — skipped {skipped['n']} detail lookup(s)")
        failed = sum(1 for r in records if "detail_error" in r)
        self._diag["details_fetched"] = True
        self._diag["detail_failures"] = failed
        self._step(f"details done: {len(records) - failed} ok, {failed} failed/skipped")

    @staticmethod
    def _page_links(html, cap=3500):
        """Every anchor and scripted link on a page, bounded."""
        soup = BeautifulSoup(html, "html.parser")
        links = []
        for el in soup.find_all(True):
            for attr in ("href", "onclick", "src"):
                value = el.get(attr)
                if isinstance(value, str) and value not in ("", "#"):
                    text = el.get_text(" ", strip=True)[:60]
                    links.append(f"{attr}={value[:220]} [{text}]")
        out = []
        used = 0
        for link in links:
            if used + len(link) > cap:
                out.append(f"... {len(links) - len(out)} more link(s) elided")
                break
            out.append(link)
            used += len(link)
        return out

    @staticmethod
    def _adopt_detail_image(rec, detail_html):
        """Take the document id from the record's own page, if it has one."""
        guids = detail_image_guids(detail_html)
        if not guids:
            return
        rec["digital_image_url"] = (
            f"{BASE_URL}/ImageMain.aspx?DocIds={','.join(guids)}")
        rec["has_digital_image"] = True
        rec["image_source"] = "detail"
        rec["attachments"].append({
            "label": f"Digital Image - {rec.get('doc_type', '')} "
                     f"{rec.get('doc_number', '')}".strip(),
            "url": rec["digital_image_url"],
            "type": "digital_image",
        })

    def _parse_detail_html(self, html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        detail = {}
        for b_tag in soup.find_all("b"):
            label = b_tag.get_text(strip=True).rstrip(":")
            next_sib = b_tag.next_sibling
            if next_sib and isinstance(next_sib, str):
                value = next_sib.strip()
                if value and value.lower() != "none":
                    key = label.lower().replace(" ", "_")
                    detail[key] = value
        return detail

    def _build_summary(self, records, identifier):
        if not records:
            return f"No records found for {identifier}."
        type_counts = {}
        total_attachments = 0
        for r in records:
            t = r.get("doc_type", "Unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
            total_attachments += len(r.get("attachments", []))
        lines = [f"Found {len(records)} record(s) for {identifier}:"]
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  • {t}: {c}")
        lines.append(f"Total attachments available: {total_attachments}")
        if self._diag and self._diag.get("truncated"):
            lines.append(
                f"NOTE: stopped at the {self.budget_seconds:.0f}s time limit — "
                f"this list may be incomplete. Raise SCRAPE_TIMEOUT_SECONDS to "
                f"collect more.")
        return "\n".join(lines)
