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
    soup = BeautifulSoup(html, "html.parser")
    records = []

    grid = soup.find("table", id="grdIdisResult")
    if not grid:
        logger.warning("grdIdisResult table not found in HTML")
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
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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

    async def _run(self, page, context, number, street_name, raw_address):
        # Step 1: Load main page for session, then search form
        await self._goto(page, MAIN_URL)
        await self._goto(page, f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR")

        # Step 2: Fill and submit search form via Playwright
        await page.fill("input[name='Address$txtAddressBegNo']", number)
        await page.fill("input[name='Address$txtAddressStreetName']", street_name)
        await page.click("input[name='btnNext1']")
        await asyncio.sleep(3)
        logger.info(f"After search submit: {page.url}")

        # Step 3: Address selection — check all and continue
        checkboxes = await page.query_selector_all("input[type='checkbox']:not([id*='CheckAll'])")
        logger.info(f"Address checkboxes: {len(checkboxes)}")

        if len(checkboxes) > 0:
            for cb in checkboxes:
                try:
                    await cb.check()
                except: pass

            # Click continue/search button
            continue_btn = await page.query_selector("input[name='btnSearch'], input[value='Continue']")
            if continue_btn:
                await continue_btn.click()
                await asyncio.sleep(4)
            logger.info(f"After continue: {page.url}")

        # Step 4: Parse results — we're now on the DocumentSearch results page
        # Use the browser's actual rendered HTML
        html = await page.content()
        logger.info(f"Results HTML length: {len(html)}")

        all_records = parse_results_html(html)
        logger.info(f"Page 1: {len(all_records)} records")

        # Step 5: Handle pagination — click page 2, 3 etc
        soup = BeautifulSoup(html, "html.parser")
        page_nav = soup.find("div", id="pnlNavigate")
        if page_nav:
            page_links = page_nav.find_all("a")
            for pg_link in page_links:
                pg_text = pg_link.get_text(strip=True)
                if pg_text.isdigit() and int(pg_text) > 1:
                    logger.info(f"Navigating to page {pg_text}...")
                    try:
                        # Use JavaScript to call goPage
                        await page.evaluate(f"goPage('{pg_text}')")
                        await asyncio.sleep(4)
                        pg_html = await page.content()
                        pg_records = parse_results_html(pg_html)
                        logger.info(f"Page {pg_text}: {len(pg_records)} records")
                        all_records.extend(pg_records)
                    except Exception as e:
                        logger.warning(f"Page {pg_text} failed: {e}")

        logger.info(f"Total records: {len(all_records)}")

        if not all_records:
            return {
                "address": raw_address,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": "No records found.",
            }

        # Step 6: Scrape detail pages using the same browser session
        detailed = []
        all_attachments = []

        for i, rec in enumerate(all_records):
            logger.info(f"Detail {i+1}/{len(all_records)}: {rec['doc_type']} {rec['doc_number']}")
            try:
                await self._goto(page, rec["detail_url"])
                detail_html = await page.content()

                # Check for session expired
                if "SessionExpired" in page.url or "IdisError" in page.url:
                    logger.warning(f"Session expired on detail {i+1}, skipping")
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

        # Extract bold label: value pairs
        for b_tag in soup.find_all("b"):
            label = b_tag.get_text(strip=True).rstrip(":")
            next_sib = b_tag.next_sibling
            if next_sib and isinstance(next_sib, str):
                value = next_sib.strip()
                if value and value.lower() != "none":
                    key = label.lower().replace(" ", "_")
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
