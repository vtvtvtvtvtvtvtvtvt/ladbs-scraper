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
from pydantic import BaseModel
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
    address: Optional[str] = None
    ain: Optional[str] = None

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/scrape")
async def scrape(request: ScrapeRequest):
    scraper = LADBSScraper()

    # Prefer AIN over address if both provided
    if request.ain:
        logger.info(f"Scrape request by AIN: {request.ain}")
        try:
            result = await scraper.scrape_by_ain(request.ain)
            return result
        except Exception as e:
            logger.error(f"AIN scrape failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    elif request.address:
        logger.info(f"Scrape request by address: {request.address}")
        try:
            result = await scraper.scrape(request.address)
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

            cookies = await context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            async with httpx.AsyncClient(cookies=cookie_dict, follow_redirects=True, timeout=60) as client:
                resp = await client.get(pdf_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": url,
                    "Accept": "application/pdf,*/*",
                })

                ct = resp.headers.get("content-type", "")
                size = len(resp.content)
                logger.info(f"StPdfViewer: status={resp.status_code} ct={ct} size={size}")

                if size > 500 and "html" not in ct.lower():
                    return StreamingResponse(
                        io.BytesIO(resp.content),
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
