import asyncio
import logging
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

BASE_URL = "https://ladbsdoc.lacity.org/IDISPublic_Records/idis"

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
                result = await self._run_search(page, number, street_name, address)
                return result
            finally:
                await browser.close()

    async def _goto(self, page, url):
        logger.info(f"Navigating to: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning(f"goto warning: {e}")
        await asyncio.sleep(2)

    async def _run_search(self, page, number, street_name, raw_address):
        await self._goto(page, f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR")

        # Inspect ALL inputs on the page so we know exact field names
        all_inputs = await page.query_selector_all("input, select")
        input_info = []
        for inp in all_inputs:
            info = {
                "tag": await inp.evaluate("el => el.tagName"),
                "type": await inp.get_attribute("type"),
                "id": await inp.get_attribute("id"),
                "name": await inp.get_attribute("name"),
                "value": await inp.get_attribute("value"),
            }
            input_info.append(info)
            logger.info(f"FIELD: {info}")

        # Try to fill using exact field inspection
        filled = await self._fill_form(page, number, street_name, input_info)
        if not filled:
            # Return debug info so we can see the form structure
            return {
                "address": raw_address,
                "error": "Could not fill address form",
                "debug_fields": input_info,
                "page_url": page.url,
                "page_content_snippet": (await page.content())[:2000],
            }

        submit = await self._find_submit(page)
        if not submit:
            return {"address": raw_address, "error": "No submit button found", "debug_fields": input_info}

        await submit.click()
        await asyncio.sleep(3)

        return await self._parse_results(page, raw_address)

    async def _fill_form(self, page, number, street_name, input_info):
        # Try by known LADBS ASP.NET field name patterns
        filled_number = False
        filled_name = False

        text_inputs = await page.query_selector_all("input[type='text']")
        logger.info(f"Text inputs count: {len(text_inputs)}")

        for inp in text_inputs:
            field_id = (await inp.get_attribute("id") or "").lower()
            field_name = (await inp.get_attribute("name") or "").lower()
            combined = field_id + " " + field_name
            logger.info(f"Checking field: id={field_id} name={field_name}")

            # Street number patterns
            if any(k in combined for k in ["begno", "beg_no", "stno", "streetno", "houseno", "strno", "hno", "txtbegno", "txtno"]):
                await inp.fill(number)
                logger.info(f"Filled number into {field_id}")
                filled_number = True

            # Street name patterns
            elif any(k in combined for k in ["streetname", "street_name", "strname", "txtstreet", "txtname", "stnm", "stname"]):
                await inp.fill(street_name)
                logger.info(f"Filled street name into {field_id}")
                filled_name = True

        if filled_number and filled_name:
            return True

        # Fallback: if exactly 2+ text inputs, assume first=number, second=name
        if len(text_inputs) >= 2 and not filled_number and not filled_name:
            await text_inputs[0].fill(number)
            await text_inputs[1].fill(street_name)
            logger.info("Filled by position (fallback)")
            return True

        # Partial fill fallback
        if len(text_inputs) == 1:
            await text_inputs[0].fill(f"{number} {street_name}")
            return True

        return False

    async def _find_submit(self, page):
        for selector in ["input[type='submit']", "input[type='button']", "button[type='submit']", "button"]:
            elements = await page.query_selector_all(selector)
            for el in elements:
                text = (await el.get_attribute("value") or await el.inner_text() or "").lower()
                if any(k in text for k in ["search", "next", "find", "submit"]):
                    return el
        return await page.query_selector("input[type='submit']")

    async def _parse_results(self, page, raw_address):
        content = await page.content()
        logger.info(f"Results URL: {page.url}")

        if any(p in content.lower() for p in ["no records found", "no results", "0 record"]):
            return {"address": raw_address, "total_records": 0, "records": [], "attachments": [], "summary": "No records found."}

        records = []
        rows = await page.query_selector_all("table tr")
        for row in rows[1:]:
            cells = await row.query_selector_all("td")
            if len(cells) < 3:
                continue
            cell_texts = [await c.inner_text() for c in cells]
            link = await row.query_selector("a")
            href = await link.get_attribute("href") if link else None
            records.append({
                "raw_cells": cell_texts,
                "link": f"{BASE_URL}/{href}" if href and not href.startswith("http") else href,
            })

        logger.info(f"Found {len(records)} records")

        detailed_records = []
        all_attachments = []

        for i, rec in enumerate(records[:50]):
            if rec.get("link"):
                try:
                    detail = await self._scrape_detail(page, rec["link"], rec["raw_cells"])
                    detailed_records.append(detail)
                    all_attachments.extend(detail.get("attachments", []))
                except Exception as e:
                    logger.warning(f"Failed record {i+1}: {e}")
                    detailed_records.append({"raw": rec["raw_cells"], "link": rec["link"], "error": str(e), "attachments": []})

        return {
            "address": raw_address,
            "total_records": len(detailed_records),
            "records": detailed_records,
            "attachments": all_attachments,
            "summary": self._build_summary(detailed_records, raw_address),
        }

    async def _scrape_detail(self, page, url, raw_cells):
        await self._goto(page, url)
        detail = {"url": url, "raw_cells": raw_cells, "attachments": []}

        field_map = {
            "doc_type": ["document type", "doc type"],
            "doc_number": ["document number", "permit number"],
            "doc_date": ["document date", "date"],
            "status": ["status"],
            "project_name": ["project name"],
            "address": ["property address"],
            "council_district": ["council district"],
        }

        rows = await page.query_selector_all("table tr")
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) >= 2:
                label = (await cells[0].inner_text()).strip().lower().rstrip(":")
                value = (await cells[1].inner_text()).strip()
                for field, keywords in field_map.items():
                    if any(k in label for k in keywords) and field not in detail:
                        detail[field] = value

        links = await page.query_selector_all("a")
        for link in links:
            href = await link.get_attribute("href") or ""
            text = (await link.inner_text()).strip()
            if any(k in href.lower() for k in [".pdf", "viewimage", "download"]) or \
               any(k in text.lower() for k in ["view image", "digital image", "view pdf"]):
                full_url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
                if href and full_url not in [a["url"] for a in detail["attachments"]]:
                    detail["attachments"].append({"label": text or "Attachment", "url": full_url})

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
