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

def extract_open_window_id(href: str):
    """Extract document ID from JavaScript:OpenWindow('12345',...) calls."""
    m = re.search(r"OpenWindow\('(\d+)'", href, re.IGNORECASE)
    return m.group(1) if m else None

def build_viewer_url(doc_id: str, guid: str = None):
    """Build the viewer URL for a digital image."""
    if guid:
        return f"{BASE_URL}/ViewReport.aspx?rpt=DocumentView&DocID={doc_id}"
    return f"{BASE_URL}/ViewReport.aspx?rpt=DocumentView&DocID={doc_id}"

def parse_results_html(html: str) -> list:
    """Parse the document results table from raw HTML using BeautifulSoup."""
    soup = BeautifulSoup(html, "html.parser")
    records = []

    # Find all rows that contain document type links
    # Real data rows have a link with href containing "DocumentReport" or "lnkDocType"
    # or href that goes to a document detail page
    all_rows = soup.find_all("tr")

    for row in all_rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        # The results table has these columns:
        # [checkbox] [Document Type link] [Sub Type link] [Doc Date link] [User Doc Number link] [Digital Image link/img]
        # We identify real rows by: cell[1] contains an <a> whose text looks like a document type

        doc_type_cell = cells[1] if len(cells) > 1 else None
        sub_type_cell = cells[2] if len(cells) > 2 else None
        doc_date_cell = cells[3] if len(cells) > 3 else None
        doc_number_cell = cells[4] if len(cells) > 4 else None
        digital_image_cell = cells[5] if len(cells) > 5 else None

        if not doc_type_cell:
            continue

        # Get doc type link
        doc_type_link = doc_type_cell.find("a")
        if not doc_type_link:
            continue

        doc_type = doc_type_link.get_text(strip=True)
        doc_type_href = doc_type_link.get("href", "")

        # Filter out non-data rows - real doc types are things like "BUILDING PERMIT", "CERTIFICATE OF OCCUPANCY" etc.
        # They should NOT be navigation/UI text
        skip_words = ["document type", "sort by", "then by", "sub type", "doc date", "doc number",
                      "ascending", "descending", "all", "printer", "address", "legal", "assessor"]
        if any(skip in doc_type.lower() for skip in skip_words):
            continue
        if len(doc_type) < 3:
            continue

        # Build detail URL
        detail_url = None
        if doc_type_href and not doc_type_href.lower().startswith("javascript"):
            detail_url = doc_type_href if doc_type_href.startswith("http") else f"{BASE_URL}/{doc_type_href.lstrip('/')}"

        # Get sub type
        sub_type_link = sub_type_cell.find("a") if sub_type_cell else None
        sub_type = sub_type_link.get_text(strip=True) if sub_type_link else (sub_type_cell.get_text(strip=True) if sub_type_cell else "")

        # Get doc date
        doc_date_link = doc_date_cell.find("a") if doc_date_cell else None
        doc_date = doc_date_link.get_text(strip=True) if doc_date_link else (doc_date_cell.get_text(strip=True) if doc_date_cell else "")

        # Get doc number
        doc_number_link = doc_number_cell.find("a") if doc_number_cell else None
        doc_number = doc_number_link.get_text(strip=True) if doc_number_link else (doc_number_cell.get_text(strip=True) if doc_number_cell else "")

        # Get digital image
        digital_image_url = None
        digital_image_doc_id = None
        if digital_image_cell:
            img_link = digital_image_cell.find("a")
            if img_link:
                img_href = img_link.get("href", "")
                doc_id = extract_open_window_id(img_href)
                if doc_id:
                    digital_image_doc_id = doc_id
                    digital_image_url = f"{BASE_URL}/ViewReport.aspx?rpt=DocumentView&DocID={doc_id}"

        # Also check doc_type link for detail URL from doc_number link
        if not detail_url and doc_number_link:
            dn_href = doc_number_link.get("href", "")
            if dn_href and not dn_href.lower().startswith("javascript"):
                detail_url = dn_href if dn_href.startswith("http") else f"{BASE_URL}/{dn_href.lstrip('/')}"

        record = {
            "doc_type": doc_type,
            "sub_type": sub_type,
            "doc_date": doc_date,
            "doc_number": doc_number,
            "detail_url": detail_url,
            "digital_image_url": digital_image_url,
            "digital_image_doc_id": digital_image_doc_id,
            "attachments": [],
        }
        records.append(record)
        logger.info(f"Parsed record: {doc_type} | {sub_type} | {doc_date} | {doc_number} | img_id={digital_image_doc_id}")

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

        # Step 2: POST search
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
        logger.info(f"POSTing search: number={number} street={street_name}")

        async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=30) as client:
            resp = await client.post(form_url, data=post_data, headers=headers)
            logger.info(f"Search POST: {resp.status_code} → {resp.url}")
            html1 = resp.text
            for k, v in resp.cookies.items():
                cookies[k] = v

        # Load into Playwright to extract form state
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
                logger.info(f"Continue POST: {resp2.status_code} → {resp2.url}")
                results_html = resp2.text
                results_url = str(resp2.url)
                for k, v in resp2.cookies.items():
                    cookies[k] = v
        else:
            results_html = html1
            results_url = form_url

        logger.info(f"Results page length: {len(results_html)}")

        # Step 4: Parse ALL pages of results using BeautifulSoup
        all_records = parse_results_html(results_html)
        logger.info(f"Page 1: {len(all_records)} records")

        # Check for page 2, 3, etc.
        soup = BeautifulSoup(results_html, "html.parser")
        page_links = soup.find_all("a")
        page_nums_found = set()
        for lnk in page_links:
            txt = lnk.get_text(strip=True)
            if txt.isdigit() and int(txt) > 1:
                page_nums_found.add(int(txt))

        logger.info(f"Additional pages found: {page_nums_found}")

        # For pagination, load the results in Playwright and click page numbers
        if page_nums_found:
            await page.set_content(results_html)
            await asyncio.sleep(1)
            for pg in sorted(page_nums_found):
                try:
                    pg_link = await page.query_selector(f"a:text('{pg}')")
                    if pg_link:
                        await pg_link.click()
                        await asyncio.sleep(3)
                        pg_html = await page.content()
                        pg_records = parse_results_html(pg_html)
                        logger.info(f"Page {pg}: {len(pg_records)} records")
                        all_records.extend(pg_records)
                except Exception as e:
                    logger.warning(f"Page {pg} failed: {e}")

        logger.info(f"Total records: {len(all_records)}")

        if not all_records:
            return {
                "address": raw_address,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": "No records found.",
            }

        # Step 5: Scrape detail pages
        detailed = []
        all_attachments = []

        for i, rec in enumerate(all_records):
            logger.info(f"Detail {i+1}/{len(all_records)}: {rec['doc_type']} {rec['doc_number']}")
            try:
                if rec.get("detail_url"):
                    await self._goto(page, rec["detail_url"])
                    rec = await self._extract_detail(page, rec)
            except Exception as e:
                logger.warning(f"Detail {i+1} failed: {e}")
                rec["error"] = str(e)

            # Add digital image as attachment
            if rec.get("digital_image_url"):
                rec["attachments"].append({
                    "label": f"Digital Image - {rec['doc_type']} {rec['doc_number']}",
                    "url": rec["digital_image_url"],
                    "type": "digital_image",
                    "doc_id": rec.get("digital_image_doc_id"),
                })

            detailed.append(rec)
            all_attachments.extend(rec.get("attachments", []))

        return {
            "address": raw_address,
            "total_records": len(detailed),
            "records": detailed,
            "attachments": all_attachments,
            "summary": self._build_summary(detailed, raw_address),
        }

    async def _extract_detail(self, page, rec):
        """Extract fields from individual document detail page."""
        field_map = {
            "status": ["status"],
            "project_name": ["project name"],
            "address": ["property address", "address"],
            "council_district": ["council district"],
            "applicant": ["applicant"],
            "contractor": ["contractor"],
            "description": ["description", "work description"],
            "valuation": ["valuation", "value"],
        }
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).lower().rstrip(":")
                value = cells[1].get_text(strip=True)
                for field, keywords in field_map.items():
                    if any(k in label for k in keywords) and field not in rec:
                        rec[field] = value

        # Look for additional digital image links on detail page
        links = soup.find_all("a")
        for link in links:
            href = link.get("href", "")
            text = link.get_text(strip=True)
            doc_id = extract_open_window_id(href)
            if doc_id:
                viewer_url = f"{BASE_URL}/ViewReport.aspx?rpt=DocumentView&DocID={doc_id}"
                if viewer_url not in [a["url"] for a in rec["attachments"]]:
                    rec["attachments"].append({
                        "label": text or f"Digital Image {doc_id}",
                        "url": viewer_url,
                        "type": "digital_image",
                        "doc_id": doc_id,
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
