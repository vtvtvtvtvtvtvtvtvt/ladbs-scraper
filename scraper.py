import asyncio
import logging
import base64
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
        # Step 1: Main page for session
        await self._goto(page, MAIN_URL)

        # Step 2: Go to address search form
        await self._goto(page, f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR")
        logger.info(f"Form URL: {page.url}")

        # Step 3: Find and fill text inputs
        text_inputs = await page.query_selector_all(
            "input[type='text'], input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='image']):not([type='checkbox'])"
        )
        logger.info(f"Found {len(text_inputs)} text inputs")

        if len(text_inputs) == 0:
            return {"address": raw_address, "error": "No text inputs on form", "url": page.url}

        await text_inputs[0].fill(number)
        if len(text_inputs) >= 2:
            await text_inputs[1].fill(street_name)

        # Step 4: Submit the search form
        submit = await page.query_selector("input[type='submit'], input[type='button'][value*='Next'], input[type='button'][value*='Search'], a[href*='Next']")
        if not submit:
            # Try any button-like element
            buttons = await page.query_selector_all("input[type='button'], button")
            for btn in buttons:
                val = (await btn.get_attribute("value") or await btn.inner_text() or "").lower()
                if any(k in val for k in ["next", "search", "find", "continue", "go"]):
                    submit = btn
                    break

        if submit:
            await submit.click()
        else:
            await page.keyboard.press("Enter")

        await asyncio.sleep(3)
        logger.info(f"After form submit URL: {page.url}")

        # Step 5: Handle the address selection page
        # The page shows a table of matching addresses with checkboxes
        # We need to check ALL checkboxes (or the first one) and click Continue
        return await self._handle_address_selection(page, raw_address)

    async def _handle_address_selection(self, page, raw_address):
        content = await page.content()
        url = page.url
        logger.info(f"Address selection page URL: {url}")

        # Check if this is the address selection intermediate page
        # It has checkboxes for address matches
        checkboxes = await page.query_selector_all("input[type='checkbox']")
        logger.info(f"Found {len(checkboxes)} checkboxes")

        if len(checkboxes) > 0:
            # Check "All" checkbox if present, otherwise check all individual ones
            all_checkbox = await page.query_selector("input[type='checkbox'][id*='All'], input[type='checkbox'][name*='All']")
            if all_checkbox:
                await all_checkbox.check()
                logger.info("Checked 'All' checkbox")
            else:
                # Check all checkboxes (skip the header "All" one if it exists)
                for cb in checkboxes:
                    try:
                        cb_id = await cb.get_attribute("id") or ""
                        cb_name = await cb.get_attribute("name") or ""
                        if "all" not in cb_id.lower() and "all" not in cb_name.lower():
                            await cb.check()
                    except:
                        pass
                logger.info(f"Checked {len(checkboxes)} individual checkboxes")

            # Click Continue button
            continue_btn = None
            for selector in ["input[value='Continue']", "input[value='continue']", "a[href*='Continue']"]:
                continue_btn = await page.query_selector(selector)
                if continue_btn:
                    break

            if not continue_btn:
                # Find by text
                buttons = await page.query_selector_all("input[type='button'], input[type='submit'], button, a")
                for btn in buttons:
                    text = (await btn.get_attribute("value") or await btn.inner_text() or "").lower()
                    if "continue" in text:
                        continue_btn = btn
                        break

            if continue_btn:
                logger.info("Clicking Continue...")
                await continue_btn.click()
                await asyncio.sleep(3)
                logger.info(f"After Continue URL: {page.url}")
            else:
                logger.warning("No Continue button found")

        # Step 6: Now parse the actual document results
        return await self._parse_document_results(page, raw_address)

    async def _parse_document_results(self, page, raw_address):
        content = await page.content()
        url = page.url
        logger.info(f"Document results URL: {url}")

        if any(p in content.lower() for p in ["no records found", "no results", "0 record"]):
            return {"address": raw_address, "total_records": 0, "records": [], "attachments": [], "summary": "No records found."}

        # Parse result rows from all frames
        records = []
        for frame in page.frames:
            rows = await frame.query_selector_all("table tr")
            for row in rows[1:]:
                cells = await row.query_selector_all("td")
                if len(cells) < 2:
                    continue
                cell_texts = [(await c.inner_text()).strip() for c in cells]
                if not any(cell_texts):
                    continue
                link = await row.query_selector("a")
                href = await link.get_attribute("href") if link else None
                if href:
                    full_url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
                    records.append({"raw_cells": cell_texts, "link": full_url})

        logger.info(f"Found {len(records)} document records")

        if len(records) == 0:
            return {
                "address": raw_address,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": "No records found.",
                "debug_url": url,
                "debug_snippet": content[:2000],
            }

        detailed_records = []
        all_attachments = []

        for i, rec in enumerate(records[:50]):
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

        for frame in page.frames:
            rows = await frame.query_selector_all("table tr")
            for row in rows:
                cells = await row.query_selector_all("td")
                if len(cells) >= 2:
                    label = (await cells[0].inner_text()).strip().lower().rstrip(":")
                    value = (await cells[1].inner_text()).strip()
                    for field, keywords in field_map.items():
                        if any(k in label for k in keywords) and field not in detail:
                            detail[field] = value

            links = await frame.query_selector_all("a")
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
