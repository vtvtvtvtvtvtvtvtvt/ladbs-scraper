import os
import io
import re
import httpx
import asyncio
import uvicorn
import logging
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
from scraper import LADBSScraper
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LADBS Scraper API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScrapeRequest(BaseModel):
    # Reject unknown fields. A misspelled or unsupported option that is quietly
    # accepted and ignored is worse than an error: the caller believes it took
    # effect and measures the wrong thing.
    model_config = ConfigDict(extra="forbid")

    address: Optional[str] = None
    ain: Optional[str] = None
    # Detail pages add only fields like status/applicant. Callers that render
    # the grid columns (type, sub-type, date, number, image link) can turn
    # them off.
    include_details: bool = True
    # Overrides SCRAPE_TIMEOUT_SECONDS for this request, so a caller that can
    # afford to wait longer does not need the service redeployed.
    time_budget_seconds: Optional[float] = Field(default=None, ge=10, le=900)
    # "all" submits every matched parcel at once; "each" walks them one at a
    # time (slower, kept as a fallback).
    parcel_mode: str = Field(default="all", pattern="^(all|each)$")
    # Returns the raw results-grid and pager markup in diagnostics, for
    # diagnosing a parsing gap without a screenshot.
    debug: bool = False

@app.get("/health")
def health():
    return {"status": "ok"}

def _log_if_empty(result: dict, identifier: str):
    """An empty result is usually a broken flow, not an empty parcel — say why."""
    if result.get("total_records"):
        if result.get("status") == "partial":
            logger.warning(
                f"{identifier}: returned {result['total_records']} record(s) but hit "
                f"the time budget — raise SCRAPE_TIMEOUT_SECONDS or pass "
                f"include_details=false")
        return
    logger.warning(f"{identifier}: status={result.get('status')}")
    diag = result.get("diagnostics", {}) or {}
    logger.warning(f"No records for {identifier}. Steps: {diag.get('steps')}")
    for warning in diag.get("warnings", []):
        logger.warning(f"  ! {warning}")
    snap = diag.get("page_snapshot")
    if snap:
        logger.warning(f"  page it ended on: {snap.get('url')}")
        logger.warning(f"  title: {snap.get('title')!r} grid={snap.get('has_results_grid')}")
        logger.warning(f"  fields: {snap.get('form_fields')}")
        logger.warning(f"  text: {snap.get('visible_text')}")


@app.post("/scrape")
async def scrape(request: ScrapeRequest):
    scraper = LADBSScraper(
        budget_seconds=request.time_budget_seconds,
        parcel_mode=request.parcel_mode,
        debug=request.debug,
    )

    # Prefer AIN over address if both provided
    if request.ain:
        logger.info(f"Scrape request by AIN: {request.ain}")
        try:
            result = await scraper.scrape_by_ain(
                request.ain, include_details=request.include_details)
            _log_if_empty(result, f"AIN {request.ain}")
            return result
        except Exception as e:
            logger.error(f"AIN scrape failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    elif request.address:
        logger.info(f"Scrape request by address: {request.address}")
        try:
            result = await scraper.scrape(
                request.address, include_details=request.include_details)
            _log_if_empty(result, request.address)
            return result
        except Exception as e:
            logger.error(f"Address scrape failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    else:
        raise HTTPException(status_code=400, detail="Either 'address' or 'ain' is required")

@app.get("/fetch-image")
async def fetch_image(url: str = Query(...)):
    logger.info(f"fetch-image: {url}")

    m = re.search(r'\{([0-9a-f\-]+)\}', url, re.I)
    if not m:
        raise HTTPException(status_code=400, detail=f"Cannot extract GUID from URL: {url}")

    guid = m.group(1)
    doc_id = "{" + guid + "}"
    library = "IDIS"

    pdf_url = (
        f"https://ladbsdoc.lacity.org/IDISPublic_Records/idis/StPdfViewer.aspx"
        f"?Library={library}&Id={quote(doc_id, safe='')}&ObjType=2&Op=View"
    )
    logger.info(f"PDF URL: {pdf_url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        try:
            page = await context.new_page()
            await page.goto("https://ladbsdoc.lacity.org", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1)
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            # Fetch through the browser context so the request carries the
            # session's own cookies and headers. Hand-built headers -- notably a
            # forged Referer alongside a truncated User-Agent -- are exactly the
            # shape upstream filtering rejects.
            resp = await context.request.get(pdf_url, headers={"Accept": "application/pdf,*/*"})
            body = await resp.body()

            ct = resp.headers.get("content-type", "")
            size = len(body)
            logger.info(f"StPdfViewer: status={resp.status} ct={ct} size={size}")

            if size > 500 and "html" not in ct.lower():
                return StreamingResponse(
                    io.BytesIO(body),
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": f'attachment; filename="ladbs_{guid}.pdf"',
                        "Content-Length": str(size),
                    }
                )

            raise HTTPException(status_code=422, detail=f"Could not retrieve PDF ({size} bytes, ct={ct})")

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"fetch-image error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await browser.close()

@app.get("/map-tile")
async def map_tile(url: str = Query(...)):
    """Proxy ArcGIS map requests with ZIMAS referer header"""
    allowed = ["zimas.lacity.org", "gis.lacity.org", "cache.gis.lacounty.gov"]
    if not any(domain in url for domain in allowed):
        raise HTTPException(status_code=403, detail="Domain not allowed")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers={
            "Referer": "https://zimas.lacity.org/",
            "Origin": "https://zimas.lacity.org",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        return Response(
            content=resp.content,
            media_type=resp.headers.get("content-type", "image/png"),
            headers={"Access-Control-Allow-Origin": "*"}
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
