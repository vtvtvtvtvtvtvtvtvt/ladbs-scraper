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
SETTLE_SECONDS = 1.5
MAX_RESULT_PAGES = 25

# A scrape must always answer, even on a parcel with hundreds of documents.
# Past this many seconds it stops collecting and returns what it has, flagged
# as truncated, rather than letting the caller time out with nothing.
SCRAPE_BUDGET_SECONDS = float(os.environ.get("SCRAPE_TIMEOUT_SECONDS", "150"))

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

        digital_image_url = None
        if image_visible and image_guid:
            digital_image_url = f"{BASE_URL}/ImageMain.aspx?DocIds={image_guid}"

        detail_url = f"{BASE_URL}/Report.aspx?Record_Id={record_id}&Image=Hidden&ImageToOpen="

        record = {
            "record_id": record_id,
            "doc_type": doc_type,
            "sub_type": sub_type,
            "doc_date": doc_date,
            "doc_number": doc_number,
            "comments": comments,
            "detail_url": detail_url,
            "digital_image_url": digital_image_url,
            "has_digital_image": image_visible,
            "attachments": [],
        }

        if digital_image_url:
            record["attachments"].append({
                "label": f"Digital Image - {doc_type} {doc_number}",
                "url": digital_image_url,
                "type": "digital_image",
            })

        records.append(record)
        logger.info(f"  {doc_type} | {sub_type} | {doc_date} | {doc_number} | img={image_visible}")

    return records


def parse_checkboxes(html: str) -> list:
    """Parcel/address selection checkboxes on the intermediate LADBS page."""
    soup = BeautifulSoup(html, "html.parser")
    pairs = []
    for cb in soup.find_all("input", {"type": "checkbox"}):
        name = cb.get("name", "") or ""
        value = cb.get("value", "") or ""
        cb_id = cb.get("id", "") or ""
        if not name:
            continue
        if "checkall" in name.lower() or "checkall" in cb_id.lower():
            continue
        pairs.append({"name": name, "value": value, "id": cb_id})
    return pairs


class LADBSScraper:
    """Drives the LADBS IDIS ASP.NET WebForms site in a real browser session.

    Every step (search, parcel selection, paging, detail pages) happens in the
    same Playwright context, so the ASP.NET session cookie, __VIEWSTATE and
    __EVENTVALIDATION are always the ones the server just issued. Replaying
    those tokens out-of-band is what made earlier versions fail intermittently.
    """

    def __init__(self, headless: bool = True, budget_seconds: float = None):
        self.headless = headless
        self.budget_seconds = (
            SCRAPE_BUDGET_SECONDS if budget_seconds is None else budget_seconds)
        self._page = None
        self._context = None
        self._diag = None
        self._deadline = None

    # ---------------- public API ----------------

    async def scrape(self, address: str) -> dict:
        """Search by street address (or by AIN, if that is what was passed)."""
        if looks_like_ain(address):
            logger.info(f"{address!r} is an AIN, not an address — searching by AIN")
            result = await self.scrape_by_ain(address)
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
            return await self._finish(records, "address", address, f"{address}")

        return await self._run(worker)

    async def scrape_by_ain(self, ain: str) -> dict:
        """Search by Assessor Identification Number (APN)."""
        book, pg, parcel = split_ain(ain)
        logger.info(f"AIN split: book={book} page={pg} parcel={parcel}")

        async def worker():
            self._diag["parsed_ain"] = {"book": book, "page": pg, "parcel": parcel}
            records = await self._collect_records(
                lambda: self._start_ain_search(book, pg, parcel)
            )
            return await self._finish(records, "ain", ain, f"AIN {format_ain(ain)}")

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
            self._page = await context.new_page()
            try:
                return await worker()
            finally:
                self._diag["elapsed_seconds"] = round(time.monotonic() - started, 1)
                await browser.close()
                self._page = None
                self._context = None

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

    async def _goto(self, url, settle: float = SETTLE_SECONDS):
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
        self._diag["checkboxes_found"] = len(checkboxes)
        self._step(f"selection page has {len(checkboxes)} parcel checkbox(es)")

        if not checkboxes:
            self._warn(
                f"no results grid and no parcel checkboxes at {self._page.url} "
                f"(page length {len(html)})"
            )
            self._diag["final_html_len"] = len(html)
            return []

        all_records = []
        for idx, cb in enumerate(checkboxes):
            if self._out_of_time(
                    f"stopped after {idx} of {len(checkboxes)} parcel(s)"):
                break
            if idx > 0:
                # Re-run the search so each selection starts from a fresh,
                # server-issued __VIEWSTATE instead of a stale one.
                await restart()
            self._step(f"selecting checkbox {idx + 1}/{len(checkboxes)}: {cb['name']}")
            if not await self._check_and_continue(cb):
                continue
            page_records = parse_results_html(await self._page.content())
            page_records += await self._paginate()
            self._step(f"  checkbox {idx + 1} yielded {len(page_records)} record(s)")
            all_records.extend(page_records)

        return self._dedupe(all_records)

    async def _check_and_continue(self, cb):
        selectors = []
        if cb.get("id"):
            selectors.append(f'input[type="checkbox"][id="{cb["id"]}"]')
        if cb.get("value"):
            selectors.append(
                f'input[type="checkbox"][name="{cb["name"]}"][value="{cb["value"]}"]')
        selectors.append(f'input[type="checkbox"][name="{cb["name"]}"]')

        checked = False
        for sel in selectors:
            try:
                loc = self._page.locator(sel).first
                if await loc.count() == 0:
                    continue
                await loc.check(timeout=5000)
                checked = True
                break
            except Exception as e:
                self._warn(f"check {cb['name']} via {sel} failed: {e}")
        if not checked:
            self._warn(f"could not check checkbox {cb['name']}")
            return False

        return await self._click_first(
            ["input[name='btnNext2']",
             "input[id='btnNext2']",
             "input[type='submit'][value='Continue']",
             "input[name='btnSearch']",
             "input[type='submit']"],
            "continue to documents")

    async def _paginate(self):
        """Click through the numbered result pages, if any."""
        extra = []
        visited = {1}
        while len(visited) < MAX_RESULT_PAGES:
            soup = BeautifulSoup(await self._page.content(), "html.parser")
            nav = soup.find(id="pnlNavigate")
            if not nav:
                break
            next_page = None
            for a in nav.find_all("a"):
                text = a.get_text(strip=True)
                if text.isdigit() and int(text) not in visited:
                    next_page = text
                    break
            if not next_page:
                break
            if self._out_of_time(f"stopped before result page {next_page}"):
                break
            visited.add(int(next_page))
            self._step(f"  result page {next_page}")
            clicked = await self._click_first(
                [f"#pnlNavigate a:text-is('{next_page}')"],
                f"result page {next_page}", required=False)
            if not clicked:
                self._warn(f"could not open result page {next_page}")
                break
            extra.extend(parse_results_html(await self._page.content()))
        self._diag["result_pages"] = max(self._diag["result_pages"], len(visited))
        return extra

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

    async def _finish(self, records, key, key_value, identifier):
        self._step(f"total unique records: {len(records)}")
        if not records:
            summary = f"No records found for {identifier}."
            outside = self._diag.get("outside_jurisdiction")
            if outside:
                summary += (
                    f" {outside} is outside the City of Los Angeles, and LADBS"
                    f" only holds records for properties inside the city — check"
                    f" the {outside} building department instead.")
            return {
                key: key_value,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": summary,
                "diagnostics": self._diag,
            }

        detailed = []
        all_attachments = []
        for i, rec in enumerate(records):
            if self._out_of_time(
                    f"skipped detail lookups for {len(records) - i} record(s)"):
                for skipped in records[i:]:
                    skipped["detail_error"] = "skipped: scrape time budget spent"
                    detailed.append(skipped)
                    all_attachments.extend(skipped.get("attachments", []))
                break

            logger.info(f"Detail {i+1}/{len(records)}: {rec['doc_type']} {rec['doc_number']}")
            try:
                # Report.aspx is a static page; no settle needed.
                await self._goto(rec["detail_url"], settle=0.2)
                if "SessionExpired" in self._page.url or "IdisError" in self._page.url:
                    self._warn(f"session expired on detail {i+1}")
                    rec["detail_error"] = "Session expired"
                else:
                    rec.update(self._parse_detail_html(await self._page.content()))
            except Exception as e:
                self._warn(f"detail {i+1} failed: {e}")
                rec["detail_error"] = str(e)

            detailed.append(rec)
            all_attachments.extend(rec.get("attachments", []))

        return {
            key: key_value,
            "total_records": len(detailed),
            "records": detailed,
            "attachments": all_attachments,
            "summary": self._build_summary(detailed, identifier),
            "diagnostics": self._diag,
        }

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
