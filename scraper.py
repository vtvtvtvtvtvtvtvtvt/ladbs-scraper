import asyncio
import logging
import re
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

BASE_URL = "https://ladbsdoc.lacity.org/IDISPublic_Records/idis"
MAIN_URL = "https://ladbsdoc.lacity.org"

def parse_address(raw: str):
    raw = raw.strip()
    parts = raw.split(",")
    street = parts[0].strip()
    tokens = street.split()
    if not tokens:
        raise ValueError(f"Cannot parse address: {raw}")
    number = tokens[0]
    name = tokens[1] if len(tokens) > 1 else ""
    return number, name

def format_ain(ain: str) -> str:
    """Format AIN for LADBS search. LADBS expects format: XXXX-XXX-XXX"""
    ain = re.sub(r'[^0-9]', '', ain)  # strip all non-digits
    if len(ain) == 10:
        return f"{ain[0:4]}-{ain[4:7]}-{ain[7:10]}"
    return ain  # return as-is if not 10 digits

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

class LADBSScraper:

    async def scrape(self, address: str) -> dict:
        """Search by address (legacy)"""
        number, street_name = parse_address(address)
        logger.info(f"Parsed: number={number}, street={street_name}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            page.set_default_timeout(30000)
            try:
                return await self._run_address(page, context, number, street_name, address)
            finally:
                await browser.close()

    async def scrape_by_ain(self, ain: str) -> dict:
        """Search by Assessor Identification Number (APN)"""
        formatted_ain = format_ain(ain)
        logger.info(f"Scraping by AIN: {ain} -> formatted: {formatted_ain}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            page.set_default_timeout(30000)
            try:
                return await self._run_ain(page, context, formatted_ain, ain)
            finally:
                await browser.close()

    async def _goto(self, page, url):
        logger.info(f"-> {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning(f"goto warning: {e}")
        await asyncio.sleep(2)

    async def _get_viewstate(self, page):
        vs, vsg, ev = "", "", ""
        try:
            vs = await page.eval_on_selector("input[name='__VIEWSTATE']", "el => el.value") or ""
        except: pass
        try:
            vsg = await page.eval_on_selector("input[name='__VIEWSTATEGENERATOR']", "el => el.value") or ""
        except: pass
        try:
            ev = await page.eval_on_selector("input[name='__EVENTVALIDATION']", "el => el.value") or ""
        except: pass
        return vs, vsg, ev

    async def _get_hidden_fields(self, page):
        fields = {}
        hiddens = await page.query_selector_all("input[type='hidden']")
        for h in hiddens:
            n = await h.get_attribute("name")
            v = await h.get_attribute("value") or ""
            if n:
                fields[n] = v
        return fields

    async def _scrape_one_checkbox(self, page, context, cookies, headers, form_url, hidden, vs, vsg, ev, cb_name, cb_val, btn_name="btnSearch", btn_val="Continue"):
        """Submit the parcel search with a single checkbox and collect all records."""
        post_data = {**hidden,
            "__VIEWSTATE": vs,
            "__VIEWSTATEGENERATOR": vsg,
            "__EVENTVALIDATION": ev,
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            btn_name: btn_val,
            cb_name: cb_val,
        }

        all_records = []
        seen_ids = set()

        async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=30) as client:
            resp = await client.post(form_url, data=post_data, headers=headers)
            logger.info(f"Checkbox POST {cb_name}: {resp.status_code} -> {resp.url}")
            html = resp.text
            for k, v in resp.cookies.items():
                cookies[k] = v

        await page.set_content(html)
        await asyncio.sleep(1)

        records = parse_results_html(html)
        for r in records:
            if r["record_id"] not in seen_ids:
                seen_ids.add(r["record_id"])
                all_records.append(r)

        # Handle pagination
        soup = BeautifulSoup(html, "html.parser")
        page_nav = soup.find("div", id="pnlNavigate")
        if page_nav:
            for pg_link in page_nav.find_all("a"):
                pg_text = pg_link.get_text(strip=True)
                if pg_text.isdigit() and int(pg_text) > 1:
                    logger.info(f"  Page {pg_text}...")
                    try:
                        vs2, vsg2, ev2 = await self._get_viewstate(page)
                        hidden2 = await self._get_hidden_fields(page)
                        pg_data = {**hidden2,
                            "__VIEWSTATE": vs2,
                            "__VIEWSTATEGENERATOR": vsg2,
                            "__EVENTVALIDATION": ev2,
                            "__EVENTTARGET": "",
                            "__EVENTARGUMENT": "",
                            "PageNavigate": "true",
                            "PageNo": pg_text,
                        }
                        results_url = str(resp.url)
                        async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=30) as client:
                            pg_resp = await client.post(results_url, data=pg_data, headers=headers)
                            pg_html = pg_resp.text
                            for k, v in pg_resp.cookies.items():
                                cookies[k] = v
                        pg_records = parse_results_html(pg_html)
                        for r in pg_records:
                            if r["record_id"] not in seen_ids:
                                seen_ids.add(r["record_id"])
                                all_records.append(r)
                    except Exception as e:
                        logger.warning(f"Page {pg_text} failed: {e}")

        logger.info(f"  Total for {cb_name}: {len(all_records)}")
        return all_records, cookies

    async def _run_ain(self, page, context, formatted_ain, raw_ain):
        """Search LADBS by AIN (Assessor Identification Number)"""
        # Parse AIN into Book/Page/Parcel - LADBS uses 3 separate fields
        ain_clean = re.sub(r'[^0-9]', '', raw_ain)
        if len(ain_clean) != 10:
            raise ValueError(f"AIN must be 10 digits, got: {ain_clean}")
        book   = ain_clean[0:4]
        pg     = ain_clean[4:7]
        parcel = ain_clean[7:10]
        logger.info(f"AIN split: book={book} page={pg} parcel={parcel}")

        # Step 1: Navigate to assessor search page via Playwright
        await self._goto(page, MAIN_URL)
        await self._goto(page, f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ASMT")

        # Step 2: Fill fields and submit via httpx POST using session cookies
        vs, vsg, ev = await self._get_viewstate(page)
        hidden = await self._get_hidden_fields(page)
        cookies = {c["name"]: c["value"] for c in await context.cookies()}

        form_url = f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ASMT"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": form_url,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://ladbsdoc.lacity.org",
        }

        post_data = {**hidden,
            "__VIEWSTATE": vs,
            "__VIEWSTATEGENERATOR": vsg,
            "__EVENTVALIDATION": ev,
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "Assessor$txtAssessorNoBook": book,
            "Assessor$txtAssessorNoPage": pg,
            "Assessor$txtAssessorNoParcel": parcel,
            "btnSearchAssessor": "Search",
        }

        async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=30) as client:
            resp = await client.post(form_url, data=post_data, headers=headers)
            logger.info(f"AIN Search POST: {resp.status_code} -> {resp.url}")
            html1 = resp.text
            for k, v in resp.cookies.items():
                cookies[k] = v

        await page.set_content(html1)
        await asyncio.sleep(1)
        logger.info(f"Address selection HTML length: {len(html1)}")
        logger.info(f"AIN search landed at: {resp.url}")

        # The checkbox form posts to the actual response URL
        checkbox_form_url = str(resp.url)

        # Get viewstate from the response HTML directly
        soup_vs = BeautifulSoup(html1, "html.parser")
        def get_val(name):
            el = soup_vs.find("input", {"name": name})
            return el.get("value", "") if el else ""

        vs2 = get_val("__VIEWSTATE")
        vsg2 = get_val("__VIEWSTATEGENERATOR")
        ev2 = get_val("__EVENTVALIDATION")
        hidden2 = {}
        for inp in soup_vs.find_all("input", {"type": "hidden"}):
            n = inp.get("name", "")
            v = inp.get("value", "")
            if n:
                hidden2[n] = v

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": checkbox_form_url,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://ladbsdoc.lacity.org",
        }

        # Check for direct results first
        direct_records = parse_results_html(html1)
        if direct_records:
            logger.info(f"Direct results: {len(direct_records)} records")
            all_records = []
            seen_ids = set()
            for r in direct_records:
                if r["record_id"] not in seen_ids:
                    seen_ids.add(r["record_id"])
                    all_records.append(r)
            cb_pairs = []
        else:
            # Extract checkboxes from HTML using BeautifulSoup
            soup1 = BeautifulSoup(html1, "html.parser")
            cb_inputs = soup1.find_all("input", {"type": "checkbox"})
            cb_pairs = []
            for cb in cb_inputs:
                cb_name = cb.get("name", "")
                cb_val = cb.get("value", "")
                cb_id = cb.get("id", "")
                if cb_name and cb_val and "All" not in cb_id and "All" not in cb_name:
                    cb_pairs.append((cb_name, cb_val))
                    logger.info(f"Checkbox: {cb_name} = {cb_val[:60]}")

            if not cb_pairs:
                logger.warning(f"No checkboxes and no direct results for AIN {formatted_ain}")
                return {
                    "ain": raw_ain,
                    "total_records": 0,
                    "records": [],
                    "attachments": [],
                    "summary": f"No records found for AIN {raw_ain}.",
                }
            all_records = []
            seen_ids = set()

        # Step 4: Submit each checkbox individually
        if cb_pairs:
            for cb_name, cb_val in cb_pairs:
                logger.info(f"Processing checkbox: {cb_name}")
                records, cookies = await self._scrape_one_checkbox(
                    page, context, cookies, headers, checkbox_form_url,
                    hidden2, vs2, vsg2, ev2, cb_name, cb_val,
                    btn_name="btnNext2", btn_val="Continue"
                )
                for r in records:
                    if r["record_id"] not in seen_ids:
                        seen_ids.add(r["record_id"])
                        all_records.append(r)

        logger.info(f"Total unique records: {len(all_records)}")

        if not all_records:
            return {
                "ain": raw_ain,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": f"No records found for AIN {raw_ain}.",
            }

        # Step 5: Scrape detail pages
        detailed = []
        all_attachments = []

        for i, rec in enumerate(all_records):
            logger.info(f"Detail {i+1}/{len(all_records)}: {rec['doc_type']} {rec['doc_number']}")
            try:
                await self._goto(page, rec["detail_url"])
                detail_html = await page.content()
                if "SessionExpired" in page.url or "IdisError" in page.url:
                    logger.warning(f"Session expired on detail {i+1}")
                    rec["detail_error"] = "Session expired"
                else:
                    detail = self._parse_detail_html(detail_html)
                    rec.update(detail)
            except Exception as e:
                logger.warning(f"Detail {i+1} failed: {e}")
                rec["detail_error"] = str(e)

            detailed.append(rec)
            all_attachments.extend(rec.get("attachments", []))

        return {
            "ain": raw_ain,
            "total_records": len(detailed),
            "records": detailed,
            "attachments": all_attachments,
            "summary": self._build_summary(detailed, f"AIN {raw_ain}"),
        }

    async def _run_address(self, page, context, number, street_name, raw_address):
        """Search LADBS by address (legacy method)"""
        await self._goto(page, MAIN_URL)
        await self._goto(page, f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR")

        vs, vsg, ev = await self._get_viewstate(page)
        hidden = await self._get_hidden_fields(page)
        cookies = {c["name"]: c["value"] for c in await context.cookies()}

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://ladbsdoc.lacity.org",
        }

        form_url = f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR"
        post_data = {**hidden,
            "__VIEWSTATE": vs,
            "__VIEWSTATEGENERATOR": vsg,
            "__EVENTVALIDATION": ev,
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "Address$txtAddressBegNo": number,
            "Address$txtAddressStreetName": street_name,
            "btnNext1": "Next",
        }

        async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=30) as client:
            resp = await client.post(form_url, data=post_data, headers=headers)
            logger.info(f"Search POST: {resp.status_code} -> {resp.url}")
            html1 = resp.text
            for k, v in resp.cookies.items():
                cookies[k] = v

        await page.set_content(html1)
        await asyncio.sleep(1)

        checkboxes = await page.query_selector_all("input[type='checkbox']:not([id*='All'])")
        cb_pairs = []
        for cb in checkboxes:
            cb_name = await cb.get_attribute("name") or ""
            cb_val = await cb.get_attribute("value") or ""
            if cb_name and cb_val:
                cb_pairs.append((cb_name, cb_val))
                logger.info(f"Checkbox: {cb_name} = {cb_val[:50]}")

        vs2, vsg2, ev2 = await self._get_viewstate(page)
        hidden2 = await self._get_hidden_fields(page)

        all_records = []
        seen_ids = set()

        for cb_name, cb_val in cb_pairs:
            logger.info(f"Processing checkbox: {cb_name}")
            records, cookies = await self._scrape_one_checkbox(
                page, context, cookies, headers, form_url,
                hidden2, vs2, vsg2, ev2, cb_name, cb_val
            )
            for r in records:
                if r["record_id"] not in seen_ids:
                    seen_ids.add(r["record_id"])
                    all_records.append(r)

        logger.info(f"Total unique records: {len(all_records)}")

        if not all_records:
            return {
                "address": raw_address,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": "No records found.",
            }

        detailed = []
        all_attachments = []

        for i, rec in enumerate(all_records):
            logger.info(f"Detail {i+1}/{len(all_records)}: {rec['doc_type']} {rec['doc_number']}")
            try:
                await self._goto(page, rec["detail_url"])
                detail_html = await page.content()
                if "SessionExpired" in page.url or "IdisError" in page.url:
                    logger.warning(f"Session expired on detail {i+1}")
                    rec["detail_error"] = "Session expired"
                else:
                    detail = self._parse_detail_html(detail_html)
                    rec.update(detail)
            except Exception as e:
                logger.warning(f"Detail {i+1} failed: {e}")
                rec["detail_error"] = str(e)

            detailed.append(rec)
            all_attachments.extend(rec.get("attachments", []))

        return {
            "address": raw_address,
            "total_records": len(detailed),
            "records": detailed,
            "attachments": all_attachments,
            "summary": self._build_summary(detailed, raw_address),
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
        return "\n".join(lines)
