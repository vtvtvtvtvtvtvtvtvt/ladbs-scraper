import asyncio
import logging
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

        # Step 2: POST search form with correct fields
        # From logs we know exact field names:
        # Address$txtAddressBegNo, Address$txtAddressStreetName, btnNext1
        post_data = {**hidden,
            "__VIEWSTATE": vs,
            "__VIEWSTATEGENERATOR": vsg,
            "__EVENTVALIDATION": ev,
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "Address$txtAddressBegNo": number,
            "Address$txtAddressStreetName": street_name,
            "btnNext1": "Next",  # This is the correct submit button
        }

        form_url = f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR"
        logger.info(f"POSTing search: number={number} street={street_name}")

        async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=30) as client:
            resp = await client.post(form_url, data=post_data, headers=headers)
            logger.info(f"Search POST: {resp.status_code} → {resp.url}")
            html1 = resp.text
            # Update cookies
            for k, v in resp.cookies.items():
                cookies[k] = v

        # Load into Playwright
        await page.set_content(html1)
        await asyncio.sleep(1)
        logger.info(f"Page 1 content length: {len(html1)}")

        # Step 3: Handle address selection page (checkboxes)
        checkboxes = await page.query_selector_all("input[type='checkbox']")
        logger.info(f"Checkboxes found: {len(checkboxes)}")

        if len(checkboxes) > 0:
            vs2, vsg2, ev2 = await self._get_viewstate(page)
            hidden2 = await self._get_hidden_fields(page)

            # Collect all checkbox names/values
            cb_data = {}
            for cb in checkboxes:
                cb_name = await cb.get_attribute("name") or ""
                cb_val = await cb.get_attribute("value") or ""
                if cb_name and "all" not in cb_name.lower():
                    cb_data[cb_name] = cb_val
                    logger.info(f"CB: {cb_name}={cb_val}")

            # Find continue button name
            continue_btn = await page.query_selector("input[value='Continue']")
            continue_name = await continue_btn.get_attribute("name") if continue_btn else "btnContinue"
            logger.info(f"Continue button: {continue_name}")

            post_data2 = {**hidden2,
                "__VIEWSTATE": vs2,
                "__VIEWSTATEGENERATOR": vsg2,
                "__EVENTVALIDATION": ev2,
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                continue_name: "Continue",
                **cb_data,
            }

            logger.info(f"POSTing continue with {len(cb_data)} checkboxes")
            async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=30) as client:
                resp2 = await client.post(form_url, data=post_data2, headers=headers)
                logger.info(f"Continue POST: {resp2.status_code} → {resp2.url}")
                html2 = resp2.text
                for k, v in resp2.cookies.items():
                    cookies[k] = v

            await page.set_content(html2)
            await asyncio.sleep(1)
            logger.info(f"After continue content length: {len(html2)}")

        # Step 4: Parse document results across all pages
        all_records = []
        page_num = 1
        while True:
            records = await self._parse_results_page(page)
            logger.info(f"Page {page_num}: {len(records)} records")
            all_records.extend(records)

            # Check for next page link
            next_link = None
            links = await page.query_selector_all("a")
            for lnk in links:
                txt = (await lnk.inner_text()).strip()
                if txt == str(page_num + 1):
                    next_link = lnk
                    break

            if next_link:
                await next_link.click()
                await asyncio.sleep(3)
                page_num += 1
            else:
                break

        logger.info(f"Total records: {len(all_records)}")

        if not all_records:
            content = await page.content()
            return {
                "address": raw_address,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": "No records found.",
                "debug_snippet": content[:3000],
            }

        # Step 5: Scrape each detail page
        detailed = []
        all_attachments = []
        for i, rec in enumerate(all_records):
            logger.info(f"Detail {i+1}/{len(all_records)}: {rec.get('doc_type')} {rec.get('doc_number')}")
            try:
                if rec.get("detail_url"):
                    await self._goto(page, rec["detail_url"])
                    rec = await self._extract_detail(page, rec)
            except Exception as e:
                logger.warning(f"Detail {i+1} failed: {e}")
                rec["error"] = str(e)
            detailed.append(rec)
            all_attachments.extend(rec.get("attachments", []))

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
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 4:
                continue

            cell_texts = [(await c.inner_text()).strip() for c in cells]

            # Skip header rows
            if cell_texts[0].lower() in ["", "all"] and any(h in " ".join(cell_texts).lower() for h in ["document type", "doc type", "sub type"]):
                continue

            # Find doc type link and image link
            doc_link = None
            img_link = None
            for cell in cells:
                links = await cell.query_selector_all("a")
                for lnk in links:
                    href = await lnk.get_attribute("href") or ""
                    img = await lnk.query_selector("img")
                    if img:
                        img_link = href
                    elif href and not doc_link:
                        doc_link = href

            # Parse columns: [checkbox, doc_type, sub_type, doc_date, doc_number, digital_image]
            doc_type = cell_texts[1] if len(cell_texts) > 1 else ""
            sub_type = cell_texts[2] if len(cell_texts) > 2 else ""
            doc_date = cell_texts[3] if len(cell_texts) > 3 else ""
            doc_number = cell_texts[4] if len(cell_texts) > 4 else ""

            if not doc_type or doc_type.lower() in ["document type", "all", ""]:
                continue

            rec = {
                "doc_type": doc_type,
                "sub_type": sub_type,
                "doc_date": doc_date,
                "doc_number": doc_number,
                "detail_url": (f"{BASE_URL}/{doc_link.lstrip('/')}" if doc_link and not doc_link.startswith("http") else doc_link),
                "digital_image_url": (f"{BASE_URL}/{img_link.lstrip('/')}" if img_link and not img_link.startswith("http") else img_link),
                "attachments": [],
            }
            records.append(rec)

        return records

    async def _extract_detail(self, page, rec):
        field_map = {
            "status": ["status"],
            "project_name": ["project name"],
            "address": ["property address", "address"],
            "council_district": ["council district"],
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

        links = await page.query_selector_all("a")
        for link in links:
            href = await link.get_attribute("href") or ""
            text = (await link.inner_text()).strip()
            if any(k in text.lower() for k in ["view image", "digital image"]) or \
               any(k in href.lower() for k in ["viewimage", "getimage"]):
                full_url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
                if href and full_url not in [a["url"] for a in rec["attachments"]]:
                    rec["attachments"].append({"label": text or "Digital Image", "url": full_url, "type": "digital_image"})

        if rec.get("digital_image_url"):
            url = rec["digital_image_url"]
            if url not in [a["url"] for a in rec["attachments"]]:
                rec["attachments"].append({
                    "label": f"{rec['doc_type']} - {rec['doc_number']}",
                    "url": url,
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
