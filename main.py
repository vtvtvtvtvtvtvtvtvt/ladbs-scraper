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

# Railway injects the commit SHA of the build it is serving. Stamping it into
# every response settles "is the fix actually deployed?" without guessing.
SERVICE_VERSION = (os.environ.get("RAILWAY_GIT_COMMIT_SHA", "") or "dev")[:7]
logger.info(f"LADBS scraper service version: {SERVICE_VERSION}")

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
    # auto: submit all address rows together and, when there are only a few,
    # also walk them individually and merge. all: together only. each: walk only.
    parcel_mode: str = Field(default="auto", pattern="^(auto|all|each)$")
    # Returns the raw results-grid and pager markup in diagnostics, for
    # diagnosing a parsing gap without a screenshot.
    debug: bool = False
    # Also search the address with each directional prefix (N/S/E/W and none):
    # 2100, 2100 N and 2100 W Cypress are separate address records, and a
    # direction-less search reaches only one of them. Variants resolving to an
    # already-searched parcel are skipped, so the cost is a few quick lookups.
    expand_directions: bool = True

@app.get("/health")
def health():
    return {"status": "ok", "version": SERVICE_VERSION}

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

    if not request.ain and not request.address:
        raise HTTPException(status_code=400, detail="Either 'address' or 'ain' is required")

    identifier = " / ".join(x for x in (request.ain, request.address) if x)
    logger.info(f"Scrape request: {identifier}")
    try:
        # Both identifiers are used when both are given. An AIN alone reaches
        # one assessor parcel; the address search reaches every address row
        # LADBS holds for the property, and they are not the same set.
        result = await scraper.scrape_all(
            ain=request.ain,
            address=request.address,
            include_details=request.include_details,
            expand_directions=request.expand_directions,
        )
        result.setdefault("diagnostics", {})["service_version"] = SERVICE_VERSION
        _log_if_empty(result, identifier)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Scrape failed for {identifier}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/scrape")
async def scrape_get(
    address: Optional[str] = Query(None),
    ain: Optional[str] = Query(None),
    include_details: bool = Query(True),
    parcel_mode: str = Query("auto", pattern="^(auto|all|each)$"),
    debug: bool = Query(False),
    time_budget_seconds: Optional[float] = Query(None, ge=10, le=900),
    expand_directions: bool = Query(True),
    format: str = Query("summary", pattern="^(summary|full)$"),
):
    """Browser-friendly scrape: open the URL, read the result.

    Exists so a live run can be inspected without curl or a client in the
    way — format=summary distills the fields that diagnose a bad pull.
    """
    request = ScrapeRequest(
        address=address, ain=ain, include_details=include_details,
        parcel_mode=parcel_mode, debug=debug,
        time_budget_seconds=time_budget_seconds,
        expand_directions=expand_directions,
    )
    result = await scrape(request)
    if format == "full":
        return result

    diag = result.get("diagnostics", {})
    # Each search leg consumes the parcels list into its own entry; the
    # address leg's is the full set the selection page offered.
    rows_offered = diag.get("parcels")
    for search in diag.get("searches", []):
        if search.get("address_rows"):
            rows_offered = search["address_rows"]
    return {
        "service_version": diag.get("service_version"),
        "status": result.get("status"),
        "total_records": result.get("total_records"),
        "address_rows_offered": rows_offered,
        "address_rows_checked": f"{diag.get('address_rows_checked')}"
                                f"/{diag.get('address_rows_found')}",
        "used_all_control": diag.get("used_all_control"),
        "searches": [
            {
                "type": s.get("type"),
                "query": s.get("query"),
                "records": s.get("records"),
                "address_rows": s.get("address_rows"),
                "per_row": [
                    {"row": st.get("row"), "records": st.get("records")}
                    for st in s.get("steps", []) if st.get("step") == "row"
                ],
                "combined": next(
                    (st.get("records") for st in s.get("steps", [])
                     if st.get("step") == "combined"), None),
            }
            for s in diag.get("searches", [])
        ],
        "images": {
            "rows_showing_icon": diag.get("rows_showing_image_icon"),
            "records_with_image": diag.get("records_with_image"),
            "from_grid": diag.get("image_ids_from_grid"),
            "from_detail": diag.get("image_ids_from_detail"),
        },
        "pages": f"{diag.get('result_pages')}/{diag.get('pages_advertised')}",
        "truncated": diag.get("truncated"),
        "elapsed_seconds": diag.get("elapsed_seconds"),
        "warnings": diag.get("warnings"),
        "summary": result.get("summary"),
        # Raw evidence for the parsing gaps, captured automatically.
        "evidence": {
            "selection_page": diag.get("selection_page"),
            "image_row_sample": diag.get("image_row_sample"),
            "detail_page_sample": diag.get("detail_page_sample"),
        },
    }


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
