import asyncio
import logging
import base64
import httpx
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
        # 1. Establish session
        await self._goto(page, MAIN_URL)

        # 2. Load search form
        await self._goto(page, f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR")

        # 3. Fill form
        text_inputs = await page.query_selector_all(
            "input[type='text'], input:not([type='hidden']):not([type='submit'])"
            ":not([type='button']):not([type='image']):not([type='checkbox'])"
        )
        if len(text_inputs) == 0:
            return {"address": raw_address, "error": "No inputs on search form", "url": page.url}

        await text_inputs[0].fill(number)
        if len(text_inputs) >= 2:
            await text_inputs[1].fill(street_name)

        # 4. Submit search
        submit = await page.query_selector("input[type='submit'], input[type='button']")
        if submit:
            await submit.click()
        else:
            await page.keyboard.press("Enter")
        await asyncio.sleep(3)

        # 5. Address selection page — check All and Continue
        checkboxes = await page.query_selector_all("input[type='checkbox']")
        if len(checkboxes) > 0:
            logger.info(f"Address selection: {len(checkboxes)} checkboxes")
            # Click the "All" checkbox
            all_cb = None
            for cb in checkboxes:
                cb_id = (await cb.get_attribute("id") or "").lower()
                cb_name = (await cb.get_attribute("name") or "").lower()
                if "all" in cb_id or "all" in cb_name:
                    all_cb = cb
                    break
            if all_cb:
                await all_cb.check()
            else:
                for cb in checkboxes:
                    await cb.check()

            # Click Continue
            btns = await page.query_selector_all("input[type='button'], input[type='submit'], input[value='Continue'], a")
            for btn in btns:
                text = (await btn.get_attribute("value") or await btn.inner_text() or "").strip().lower()
                if "continue" in text:
                    await btn.click()
                    break
            await asyncio.sleep(3)

        logger.info(f"Results page: {page.url}")

        # 6. Collect ALL records across all pages
        all_records = []
        page_num = 1
        while True:
            records = await self._parse_results_page(page)
            logger.info(f"Page {page_num}: {len(records)} records")
            all_records.extend(records)

            # Check for next page
            next_link = await page.query_selector("a[href*='Page=2'], a[href*='Page=3'], a:has-text('2'), a:has-text('Next')")
            # More reliable: look for page number links at bottom
            page_links = await page.query_selector_all("a")
            next_page_link = None
            for lnk in page_links:
                txt = (await lnk.inner_text()).strip()
                if txt == str(page_num + 1):
                    next_page_link = lnk
                    break

            if next_page_link:
                logger.info(f"Going to page {page_num + 1}")
                await next_page_link.click()
                await asyncio.sleep(3)
                page_num += 1
            else:
                break

        logger.info(f"Total records found: {len(all_records)}")

        if not all_records:
            return {
                "address": raw_address,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": "No records found.",
            }

        # 7. Get cookies for authenticated requests
        cookies = await page.context.cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

        # 8. Scrape each record detail
        detailed = []
        for i, rec in enumerate(all_records):
            logger.info(f"Scraping record {i+1}/{len(all_records)}: {rec.get('doc_type')} {rec.get('doc_number')}")
            try:
                detail = await self._scrape_record(page, rec, cookie_str)
                detailed.append(detail)
            except Exception as e:
                logger.warning(f"Record {i+1} failed: {e}")
                rec["error"] = str(e)
                detailed.append(rec)

        all_attachments = []
        for d in detailed:
            all_attachments.extend(d.get("attachments", []))

        return {
            "address": raw_address,
            "total_records": len(detailed),
            "records": detailed,
            "attachments": all_attachments,
            "summary": self._build_summary(detailed, raw_address),
        }

    async def _parse_results_page(self, page):
        records = []
        rows = await page.query_selector_all("table tr")
        for row in rows[1:]:  # skip header
            cells = await row.query_selector_all("td")
            if len(cells) < 4:
                continue

            # Col 0: checkbox, Col 1: Doc Type link, Col 2: Sub Type, Col 3: Doc Date, Col 4: Doc Number, Col 5: Digital Image
            doc_type_cell = cells[1] if len(cells) > 1 else None
            sub_type_cell = cells[2] if len(cells) > 2 else None
            doc_date_cell = cells[3] if len(cells) > 3 else None
            doc_number_cell = cells[4] if len(cells) > 4 else None
            digital_image_cell = cells[5] if len(cells) > 5 else None

            doc_type_link = await doc_type_cell.query_selector("a") if doc_type_cell else None
            doc_type = (await doc_type_link.inner_text()).strip() if doc_type_link else ""
            doc_type_href = await doc_type_link.get_attribute("href") if doc_type_link else None

            sub_type = (await sub_type_cell.inner_text()).strip() if sub_type_cell else ""
            doc_date = (await doc_date_cell.inner_text()).strip() if doc_date_cell else ""
            doc_number = (await doc_number_cell.inner_text()).strip() if doc_number_cell else ""

            # Digital image link
            digital_img_link = await digital_image_cell.query_selector("a, img") if digital_image_cell else None
            digital_img_href = None
            if digital_img_link:
                digital_img_href = await digital_img_link.get_attribute("href") or await digital_img_link.get_attribute("src")

            if not doc_type:
                continue

            rec = {
                "doc_type": doc_type,
                "sub_type": sub_type,
                "doc_date": doc_date,
                "doc_number": doc_number,
                "detail_url": (f"{BASE_URL}/{doc_type_href.lstrip('/')}" if doc_type_href and not doc_type_href.startswith("http") else doc_type_href),
                "digital_image_url": (f"{BASE_URL}/{digital_img_href.lstrip('/')}" if digital_img_href and not digital_img_href.startswith("http") else digital_img_href),
                "attachments": [],
            }
            records.append(rec)

        return records

    async def _scrape_record(self, page, rec, cookie_str):
        if not rec.get("detail_url"):
            return rec

        await self._goto(page, rec["detail_url"])

        # Extract all detail fields
        field_map = {
            "status": ["status"],
            "project_name": ["project name"],
            "address": ["property address", "address"],
            "council_district": ["council district"],
            "census_tract": ["census tract"],
            "applicant": ["applicant"],
            "contractor": ["contractor"],
            "description": ["description", "work description"],
        }

        rows = await page.query_selector_all("table tr")
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) >= 2:
                label = (await cells[0].inner_text()).strip().lower().rstrip(":")
                value = (await cells[1].inner_text()).strip()
                for field, keywords in field_map.items():
                    if any(k in label for k in keywords) and field not in rec:
                        rec[field] = value

        # Find any additional digital image links on detail page
        links = await page.query_selector_all("a")
        for link in links:
            href = await link.get_attribute("href") or ""
            text = (await link.inner_text()).strip()
            if any(k in text.lower() for k in ["view image", "digital image", "view document"]) or \
               any(k in href.lower() for k in ["viewimage", "getimage", "showimage"]):
                full_url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
                if full_url not in [a["url"] for a in rec["attachments"]]:
                    rec["attachments"].append({"label": text or "Digital Image", "url": full_url, "type": "digital_image"})

        # Add the digital image from results page if present
        if rec.get("digital_image_url") and rec["digital_image_url"] not in [a["url"] for a in rec["attachments"]]:
            rec["attachments"].append({
                "label": f"Digital Image - {rec['doc_type']} {rec['doc_number']}",
                "url": rec["digital_image_url"],
                "type": "digital_image"
            })

        return rec

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
