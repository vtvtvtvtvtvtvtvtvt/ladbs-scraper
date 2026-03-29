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
        # Step 1: Load main page for session
        await self._goto(page, MAIN_URL)
        await self._goto(page, f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR")

        # Step 2: Extract ASP.NET form state
        viewstate = await page.eval_on_selector(
            "input[name='__VIEWSTATE']", "el => el.value"
        ) or ""
        viewstate_gen = await page.eval_on_selector(
            "input[name='__VIEWSTATEGENERATOR']", "el => el.value"
        ) or ""
        event_validation = ""
        try:
            event_validation = await page.eval_on_selector(
                "input[name='__EVENTVALIDATION']", "el => el.value"
            ) or ""
        except:
            pass

        logger.info(f"VIEWSTATE length: {len(viewstate)}")

        # Step 3: Find all input field names on the form
        inputs = await page.query_selector_all("input, select")
        form_fields = {}
        for inp in inputs:
            name = await inp.get_attribute("name")
            val = await inp.get_attribute("value") or ""
            inp_type = await inp.get_attribute("type") or "text"
            if name:
                form_fields[name] = {"type": inp_type, "value": val}
                logger.info(f"Field: name={name} type={inp_type} value={val[:30]}")

        # Step 4: Find the street number and name field names
        # Try to identify by examining field names
        number_field = None
        name_field = None
        submit_field = None

        for fname, finfo in form_fields.items():
            fname_lower = fname.lower()
            if finfo["type"] in ["text", ""] or finfo["type"] is None:
                if any(k in fname_lower for k in ["beg", "no", "nbr", "num", "str", "hse", "house"]):
                    if not number_field:
                        number_field = fname
                if any(k in fname_lower for k in ["name", "nm", "street", "str"]):
                    if not name_field:
                        name_field = fname
            if finfo["type"] in ["submit", "button", "image"]:
                submit_field = fname

        logger.info(f"Guessed: number_field={number_field}, name_field={name_field}, submit={submit_field}")

        # Step 5: Build POST data
        post_data = {
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": viewstate_gen,
        }
        if event_validation:
            post_data["__EVENTVALIDATION"] = event_validation

        # Add all hidden fields
        for fname, finfo in form_fields.items():
            if finfo["type"] == "hidden":
                post_data[fname] = finfo["value"]

        # Set address fields
        if number_field:
            post_data[number_field] = number
        if name_field:
            post_data[name_field] = street_name
        if submit_field:
            post_data[submit_field] = form_fields[submit_field]["value"]

        # Step 6: Get cookies from browser session
        cookies = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        # Step 7: POST the form using httpx with browser cookies
        form_url = f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": form_url,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://ladbsdoc.lacity.org",
        }

        logger.info(f"POSTing to: {form_url}")
        logger.info(f"POST fields: { {k: v[:20] if isinstance(v, str) and len(v) > 20 else v for k, v in post_data.items()} }")

        async with httpx.AsyncClient(cookies=cookie_dict, follow_redirects=True, timeout=30) as client:
            resp = await client.post(form_url, data=post_data, headers=headers)
            logger.info(f"POST response: {resp.status_code} → {resp.url}")
            html = resp.text

        # Step 8: Load the response into Playwright for parsing
        await page.set_content(html)
        await asyncio.sleep(1)

        current_url = str(resp.url)
        logger.info(f"Result page URL: {current_url}")
        logger.info(f"Result HTML snippet: {html[:500]}")

        # Step 9: Handle address selection or results
        return await self._handle_page(page, context, cookie_dict, headers, current_url, raw_address, html)

    async def _handle_page(self, page, context, cookie_dict, headers, current_url, raw_address, html):
        # Check if this is address selection page (has checkboxes in a table)
        checkboxes = await page.query_selector_all("input[type='checkbox']")
        logger.info(f"Checkboxes found: {len(checkboxes)}")

        if len(checkboxes) > 1:
            logger.info("Address selection page detected")
            # Get all checkbox values (parcel IDs)
            cb_values = []
            for cb in checkboxes:
                cb_name = await cb.get_attribute("name")
                cb_val = await cb.get_attribute("value")
                if cb_name and cb_val and "all" not in (cb_name or "").lower():
                    cb_values.append((cb_name, cb_val))
                    logger.info(f"Checkbox: name={cb_name} value={cb_val}")

            # Get viewstate from this page
            viewstate = ""
            viewstate_gen = ""
            try:
                viewstate = await page.eval_on_selector("input[name='__VIEWSTATE']", "el => el.value") or ""
                viewstate_gen = await page.eval_on_selector("input[name='__VIEWSTATEGENERATOR']", "el => el.value") or ""
            except:
                pass

            # Find continue button
            continue_btn = await page.query_selector("input[value='Continue'], input[value='continue']")
            continue_name = await continue_btn.get_attribute("name") if continue_btn else "btnContinue"
            continue_val = await continue_btn.get_attribute("value") if continue_btn else "Continue"

            # Build POST for continue
            post_data = {
                "__VIEWSTATE": viewstate,
                "__VIEWSTATEGENERATOR": viewstate_gen,
                continue_name: continue_val,
            }
            # Add all checkboxes
            for cb_name, cb_val in cb_values:
                post_data[cb_name] = cb_val

            # Also add hidden fields
            hiddens = await page.query_selector_all("input[type='hidden']")
            for h in hiddens:
                n = await h.get_attribute("name")
                v = await h.get_attribute("value") or ""
                if n and n not in post_data:
                    post_data[n] = v

            selection_url = f"{BASE_URL}/ParcelSearch.aspx?SearchType=PRCL_ADDR"
            logger.info(f"POSTing continue to: {selection_url}")

            async with httpx.AsyncClient(cookies=cookie_dict, follow_redirects=True, timeout=30) as client:
                resp = await client.post(selection_url, data=post_data, headers=headers)
                logger.info(f"Continue response: {resp.status_code} → {resp.url}")
                html = resp.text

            await page.set_content(html)
            await asyncio.sleep(1)
            logger.info(f"After continue HTML snippet: {html[:500]}")

        # Now parse the actual results
        return await self._parse_results(page, context, cookie_dict, headers, raw_address)

    async def _parse_results(self, page, context, cookie_dict, headers, raw_address):
        content = await page.content()
        logger.info(f"Parsing results, content length: {len(content)}")

        if any(p in content.lower() for p in ["no records found", "no results", "0 record"]):
            return {"address": raw_address, "total_records": 0, "records": [], "attachments": [], "summary": "No records found."}

        records = []
        rows = await page.query_selector_all("table tr")
        logger.info(f"Table rows: {len(rows)}")

        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 4:
                continue
            cell_texts = [(await c.inner_text()).strip() for c in cells]
            logger.info(f"Row: {cell_texts[:5]}")

            # Find doc type link
            links = await row.query_selector_all("a")
            doc_link = None
            img_link = None

            for lnk in links:
                href = await lnk.get_attribute("href") or ""
                text = (await lnk.inner_text()).strip()
                src = await lnk.query_selector("img")
                if src:
                    img_link = href
                elif text:
                    doc_link = href

            if not cell_texts[0] and len(cell_texts) > 1:
                # Skip header/empty rows
                if not any(cell_texts):
                    continue

            rec = {
                "doc_type": cell_texts[1] if len(cell_texts) > 1 else "",
                "sub_type": cell_texts[2] if len(cell_texts) > 2 else "",
                "doc_date": cell_texts[3] if len(cell_texts) > 3 else "",
                "doc_number": cell_texts[4] if len(cell_texts) > 4 else "",
                "detail_url": (f"{BASE_URL}/{doc_link.lstrip('/')}" if doc_link and not doc_link.startswith("http") else doc_link),
                "digital_image_url": (f"{BASE_URL}/{img_link.lstrip('/')}" if img_link and not img_link.startswith("http") else img_link),
                "attachments": [],
            }

            if rec["doc_type"] and rec["doc_type"] not in ["Document Type", "All"]:
                records.append(rec)

        logger.info(f"Parsed {len(records)} records")

        if not records:
            return {
                "address": raw_address,
                "total_records": 0,
                "records": [],
                "attachments": [],
                "summary": "No records found.",
                "debug_snippet": content[:3000],
            }

        # Scrape detail pages
        detailed = []
        all_attachments = []
        for i, rec in enumerate(records[:50]):
            try:
                if rec.get("detail_url"):
                    await self._goto(page, rec["detail_url"])
                    rec = await self._extract_detail(page, rec)
                detailed.append(rec)
                all_attachments.extend(rec.get("attachments", []))
            except Exception as e:
                logger.warning(f"Record {i+1} detail failed: {e}")
                rec["error"] = str(e)
                detailed.append(rec)

        return {
            "address": raw_address,
            "total_records": len(detailed),
            "records": detailed,
            "attachments": all_attachments,
            "summary": self._build_summary(detailed, raw_address),
        }

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

        # Get digital image links
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
