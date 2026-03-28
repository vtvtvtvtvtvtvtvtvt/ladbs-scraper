import re
import time
import logging
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

BASE_URL = "https://ladbsdoc.lacity.org/IDISPublic_Records/idis"

def parse_address(raw: str):
    """
    Parse a raw address string into street number and street name.
    e.g. "2100 Cypress Ave, Los Angeles, CA 90065" -> ("2100", "Cypress")
    The LADBS search only needs the street number and the first word of the street name.
    """
    raw = raw.strip()
    # Remove city/state/zip
    parts = raw.split(",")
    street = parts[0].strip()

    # Split into tokens
    tokens = street.split()
    if not tokens:
        raise ValueError(f"Cannot parse address: {raw}")

    number = tokens[0]
    # Street name: everything except the last token if it looks like a type (Ave, St, Blvd...)
    # LADBS search works best with just the first word of the street name
    name = tokens[1] if len(tokens) > 1 else ""

    return number, name

class LADBSScraper:
    def __init__(self):
        self.browser = None
        self.page = None

    def _launch(self):
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        context = self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        self.page = context.new_page()
        self.page.set_default_timeout(30000)

    def _close(self):
        if self.browser:
            self.browser.close()
        if self._playwright:
            self._playwright.stop()

    def _goto(self, url, wait="domcontentloaded"):
        logger.info(f"Navigating to: {url}")
        self.page.goto(url, wait_until=wait, timeout=30000)
        time.sleep(1.5)  # Give ASP.NET pages a moment to settle

    def scrape(self, address: str) -> dict:
        number, street_name = parse_address(address)
        logger.info(f"Parsed address: number={number}, street={street_name}")

        self._launch()
        try:
            results = self._run_parcel_search(number, street_name, address)
            return results
        finally:
            self._close()

    def _run_parcel_search(self, number: str, street_name: str, raw_address: str) -> dict:
        # Step 1: Go to address search page
        self._goto(f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR")

        # Step 2: Fill in the form
        # The form has fields: BegNo (street number), StreetName
        try:
            self.page.wait_for_selector("input", timeout=10000)
        except PlaywrightTimeout:
            raise RuntimeError("Search form did not load")

        # Find and fill street number field
        # LADBS uses ASP.NET WebForms — field IDs vary, so we find by label proximity or name
        inputs = self.page.query_selector_all("input[type='text']")
        logger.info(f"Found {len(inputs)} text inputs on page")

        # Log all input names/ids to understand form structure
        input_info = []
        for inp in inputs:
            input_info.append({
                "id": inp.get_attribute("id"),
                "name": inp.get_attribute("name"),
                "value": inp.get_attribute("value"),
            })
        logger.info(f"Input fields: {input_info}")

        # Fill fields by name pattern (LADBS ASP.NET field names)
        filled = self._fill_address_form(number, street_name)
        if not filled:
            raise RuntimeError("Could not locate address form fields")

        # Step 3: Submit the form
        submit_btn = self._find_submit_button()
        if not submit_btn:
            raise RuntimeError("Could not find search submit button")

        logger.info("Submitting search form...")
        submit_btn.click()
        time.sleep(3)

        # Step 4: Parse results page
        results = self._parse_results_page(raw_address)
        return results

    def _fill_address_form(self, number: str, street_name: str) -> bool:
        """Try multiple strategies to fill the LADBS address form."""
        page = self.page

        # Strategy 1: Fill by checking field names/IDs that contain common keywords
        filled_number = False
        filled_name = False

        for inp in page.query_selector_all("input[type='text']"):
            field_id = (inp.get_attribute("id") or "").lower()
            field_name = (inp.get_attribute("name") or "").lower()
            combined = field_id + field_name

            if any(k in combined for k in ["begno", "beg_no", "stno", "streetno", "houseno", "number"]):
                inp.fill(number)
                logger.info(f"Filled number '{number}' into field id={field_id}")
                filled_number = True

            elif any(k in combined for k in ["streetname", "street_name", "strname", "name"]):
                inp.fill(street_name)
                logger.info(f"Filled street name '{street_name}' into field id={field_id}")
                filled_name = True

        if filled_number and filled_name:
            return True

        # Strategy 2: Fill by position (first field = number, second = street name)
        inputs = page.query_selector_all("input[type='text']")
        if len(inputs) >= 2:
            inputs[0].fill(number)
            inputs[1].fill(street_name)
            logger.info("Filled by position fallback")
            return True

        return False

    def _find_submit_button(self):
        """Find the search/next submit button."""
        page = self.page

        # Try input[type=submit] or button
        for selector in [
            "input[type='submit']",
            "input[type='button']",
            "button[type='submit']",
            "button",
        ]:
            elements = page.query_selector_all(selector)
            for el in elements:
                text = (el.get_attribute("value") or el.inner_text() or "").lower()
                if any(k in text for k in ["search", "next", "find", "submit"]):
                    return el

        # Fallback: first submit input
        el = page.query_selector("input[type='submit']")
        return el

    def _parse_results_page(self, raw_address: str) -> dict:
        """Parse the results list page and drill into each record."""
        page = self.page
        url = page.url
        content = page.content()
        logger.info(f"Results page URL: {url}")

        # Check for no results
        if any(phrase in content.lower() for phrase in ["no records found", "no results", "0 record"]):
            return {
                "address": raw_address,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": "No records found for this address.",
            }

        # Parse result rows — LADBS shows a table of documents
        records = []
        rows = page.query_selector_all("table tr")
        logger.info(f"Found {len(rows)} table rows")

        for row in rows[1:]:  # Skip header row
            cells = row.query_selector_all("td")
            if len(cells) < 3:
                continue

            cell_texts = [c.inner_text().strip() for c in cells]
            link = row.query_selector("a")
            href = link.get_attribute("href") if link else None

            record = {
                "raw_cells": cell_texts,
                "link": f"{BASE_URL}/{href}" if href and not href.startswith("http") else href,
            }
            records.append(record)

        logger.info(f"Parsed {len(records)} raw records")

        # Now drill into each record to get details + attachments
        detailed_records = []
        all_attachments = []

        for i, rec in enumerate(records[:50]):  # Cap at 50 to avoid infinite scraping
            if rec.get("link"):
                try:
                    detail = self._scrape_record_detail(rec["link"], rec["raw_cells"])
                    detailed_records.append(detail)
                    all_attachments.extend(detail.get("attachments", []))
                    logger.info(f"Record {i+1}: {detail.get('doc_type', 'Unknown')} - {len(detail.get('attachments', []))} attachments")
                except Exception as e:
                    logger.warning(f"Failed to scrape record detail: {e}")
                    detailed_records.append({
                        "raw": rec["raw_cells"],
                        "link": rec["link"],
                        "error": str(e),
                        "attachments": [],
                    })

        summary = self._build_summary(detailed_records, raw_address)

        return {
            "address": raw_address,
            "total_records": len(detailed_records),
            "records": detailed_records,
            "attachments": all_attachments,
            "summary": summary,
        }

    def _scrape_record_detail(self, url: str, raw_cells: list) -> dict:
        """Scrape a single document detail page."""
        self._goto(url)
        page = self.page
        content = page.content()

        detail = {
            "url": url,
            "raw_cells": raw_cells,
            "attachments": [],
        }

        # Extract key fields from the detail page
        # LADBS detail pages have labeled rows like "Document Type:", "Date:", "Status:", etc.
        field_map = {
            "doc_type": ["document type", "doc type"],
            "doc_subtype": ["document subtype", "subtype"],
            "doc_number": ["document number", "doc number", "permit number"],
            "doc_date": ["document date", "date"],
            "status": ["status"],
            "project_name": ["project name"],
            "address": ["address", "property address"],
            "council_district": ["council district"],
            "census_tract": ["census tract"],
        }

        rows = page.query_selector_all("table tr")
        for row in rows:
            cells = row.query_selector_all("td")
            if len(cells) >= 2:
                label = cells[0].inner_text().strip().lower().rstrip(":")
                value = cells[1].inner_text().strip()
                for field, keywords in field_map.items():
                    if any(k in label for k in keywords) and field not in detail:
                        detail[field] = value

        # Find attachment links (PDFs / images)
        attachment_links = page.query_selector_all("a[href*='.pdf'], a[href*='ViewImage'], a[href*='document'], a[href*='Download']")
        for link in attachment_links:
            href = link.get_attribute("href") or ""
            text = link.inner_text().strip()
            if href:
                full_url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
                detail["attachments"].append({
                    "label": text or "Attachment",
                    "url": full_url,
                })

        # Also look for image viewer links (LADBS uses FileNET viewer)
        img_links = page.query_selector_all("a")
        for link in img_links:
            href = link.get_attribute("href") or ""
            text = link.inner_text().strip().lower()
            if any(k in text for k in ["view image", "digital image", "view document", "view pdf"]):
                full_url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
                if full_url not in [a["url"] for a in detail["attachments"]]:
                    detail["attachments"].append({
                        "label": link.inner_text().strip(),
                        "url": full_url,
                    })

        return detail

    def _build_summary(self, records: list, address: str) -> str:
        """Build a human-readable summary of all records."""
        if not records:
            return f"No records found for {address}."

        type_counts = {}
        total_attachments = 0
        errors = 0

        for r in records:
            doc_type = r.get("doc_type", "Unknown")
            type_counts[doc_type] = type_counts.get(doc_type, 0) + 1
            total_attachments += len(r.get("attachments", []))
            if r.get("error"):
                errors += 1

        lines = [f"Found {len(records)} record(s) for {address}:"]
        for dtype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  • {dtype}: {count}")
        lines.append(f"Total attachments available: {total_attachments}")
        if errors:
            lines.append(f"({errors} records had errors during detail scraping)")

        return "\n".join(lines)
