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

def parse_results_html(html: str) -> list:
    """
    Parse the document results table from raw HTML.
    Each row has:
    - OpenWindow('RECORD_ID', 'Hidden'/'Visible', 'GUID') on all links
    - Hidden input with comments: grdIdisResult_hidComments_N
    - Digital image link: OpenDocument('{GUID},') — only visible if image exists
    """
    soup = BeautifulSoup(html, "html.parser")
    records = []

    # Find the results grid table
    grid = soup.find("table", id="grdIdisResult")
    if not grid:
        logger.warning("grdIdisResult table not found")
        # Try fallback: any table with OpenWindow links
        all_links = soup.find_all("a", href=re.compile(r"OpenWindow", re.I))
        logger.info(f"Found {len(all_links)} OpenWindow links")
        return records

    rows = grid.find_all("tr")
    logger.info(f"Grid rows: {len(rows)}")

    for i, row in enumerate(rows[1:]):  # skip header
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        # Extract record ID and image GUID from OpenWindow call in first link
        doc_link = cells[1].find("a")
        if not doc_link:
            continue

        href = doc_link.get("href", "")
        # Match: OpenWindow('RECORD_ID', 'Hidden'/'Visible', 'GUID')
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

        # Extract comments from hidden input
        comment_input = row.find("input", id=re.compile(r"hidComments"))
        comments = comment_input.get("value", "") if comment_input else ""

        # Digital image URL — only if Visible
        digital_image_url = None
        if image_visible and image_guid:
            digital_image_url = f"{BASE_URL}/ImageMain.aspx?DocIds={image_guid}"

        # Detail page URL
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
        logger.info(f"Row {i+1}: {doc_type} | {sub_type} | {doc_date} | {doc_number} | img={image_visible}")

    return records

class LADBSScraper:

    async def scrape(self, address: str) -> dict:
        number, street_name = parse_address(address)
        logger.info(f"Parsed: number={number}, street={street_name}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = await context.new_page()
            page.set_default_timeout(30000)
            try:
                return await self._run(page, context, number, street_name, address)
            finally:
                await browser.close()

    async def _goto(self, page, url):
        logger.info(f"→ {url}")
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

    async def _run(self, page, context, number, street_name, raw_address):
        # Step 1: Establish session
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

        # Step 2: POST search form
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

        form_url = f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR"
        logger.info(f"POST search: {number} {street_name}")

        async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=30) as client:
            resp = await client.post(form_url, data=post_data, headers=headers)
            html1 = resp.text
            for k, v in resp.cookies.items():
                cookies[k] = v

        await page.set_content(html1)
        await asyncio.sleep(1)

        # Step 3: Handle address selection checkboxes
        checkboxes = await page.query_selector_all("input[type='checkbox']")
        logger.info(f"Checkboxes: {len(checkboxes)}")

        if len(checkboxes) > 0:
            vs2, vsg2, ev2 = await self._get_viewstate(page)
            hidden2 = await self._get_hidden_fields(page)

            cb_data = {}
            for cb in checkboxes:
                cb_name = await cb.get_attribute("name") or ""
                cb_val = await cb.get_attribute("value") or ""
                if cb_name and "all" not in cb_name.lower():
                    cb_data[cb_name] = cb_val

            continue_btn = await page.query_selector("input[value='Continue']")
            continue_name = await continue_btn.get_attribute("name") if continue_btn else "btnSearch"

            post_data2 = {**hidden2,
                "__VIEWSTATE": vs2,
                "__VIEWSTATEGENERATOR": vsg2,
                "__EVENTVALIDATION": ev2,
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                continue_name: "Continue",
                **cb_data,
            }

            async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=30) as client:
                resp2 = await client.post(form_url, data=post_data2, headers=headers)
                results_html = resp2.text
                results_url = str(resp2.url)
                for k, v in resp2.cookies.items():
                    cookies[k] = v
        else:
            results_html = html1
            results_url = form_url

        logger.info(f"Results page: {results_url}, length: {len(results_html)}")

        # Step 4: Parse page 1
        all_records = parse_results_html(results_html)
        logger.info(f"Page 1: {len(all_records)} records")

        # Step 5: Handle pagination using goPage POST
        # Check for page 2+ links
        soup = BeautifulSoup(results_html, "html.parser")
        page_nav = soup.find("div", id="pnlNavigate")
        if page_nav:
            page_links = page_nav.find_all("a")
            for pg_link in page_links:
                pg_text = pg_link.get_text(strip=True)
                if pg_text.isdigit() and int(pg_text) > 1:
                    pg_num = int(pg_text)
                    logger.info(f"Fetching page {pg_num}...")

                    # Load results page in Playwright to get its form state
                    await page.set_content(results_html)
                    await asyncio.sleep(1)

                    vs3, vsg3, ev3 = await self._get_viewstate(page)
                    hidden3 = await self._get_hidden_fields(page)

                    # Update headers referer
                    headers["Referer"] = results_url

                    post_pg = {**hidden3,
                        "__VIEWSTATE": vs3,
                        "__VIEWSTATEGENERATOR": vsg3,
                        "__EVENTVALIDATION": ev3,
                        "__EVENTTARGET": "",
                        "__EVENTARGUMENT": "",
                        "PageNavigate": "true",
                        "PageNo": str(pg_num),
                    }

                    async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=30) as client:
                        resp_pg = await client.post(results_url, data=post_pg, headers=headers)
                        pg_html = resp_pg.text
                        for k, v in resp_pg.cookies.items():
                            cookies[k] = v

                    pg_records = parse_results_html(pg_html)
                    logger.info(f"Page {pg_num}: {len(pg_records)} records")
                    all_records.extend(pg_records)

        logger.info(f"Total records: {len(all_records)}")

        if not all_records:
            return {
                "address": raw_address,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": "No records found.",
            }

        # Step 6: Scrape detail pages for additional info
        detailed = []
        all_attachments = []

        for i, rec in enumerate(all_records):
            logger.info(f"Detail {i+1}/{len(all_records)}: {rec['doc_type']} {rec['doc_number']}")
            try:
                detail = await self._scrape_detail(rec)
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

    async def _scrape_detail(self, rec: dict) -> dict:
        """Fetch the detail page and extract structured fields."""
        detail_url = rec.get("detail_url")
        if not detail_url:
            return {}

        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(detail_url)
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")
        detail = {}

        # The detail page uses <b>Field: </b>value pattern inside <Font> tags
        # Extract all bold labels and their following text
        for b_tag in soup.find_all("b"):
            label_text = b_tag.get_text(strip=True).rstrip(":")
            # Get the text immediately after the <b> tag
            next_text = b_tag.next_sibling
            if next_text and isinstance(next_text, str):
                value = next_text.strip()
                if value and value != "None":
                    key = label_text.lower().replace(" ", "_")
                    detail[key] = value

        return detail

    def _build_summary(self, records, address):
        if not records:
            return f"No records found for {address}."
        type_counts = {}
        total_attachments = 0
        for r in records:
            t = r.get("doc_type", "Unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
            total_attachments += len(r.get("attachments", []))
        lines = [f"Found {len(records)} record(s) for {address}:"]
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  • {t}: {c}")
        lines.append(f"Total attachments available: {total_attachments}")
        return "\n".join(lines)
