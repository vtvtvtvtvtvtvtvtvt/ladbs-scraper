import os
import httpx
import uvicorn
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from scraper import LADBSScraper

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
    address: str

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/scrape")
async def scrape(request: ScrapeRequest):
    logger.info(f"Scrape request for: {request.address}")
    try:
        scraper = LADBSScraper()
        result = await scraper.scrape(request.address)
        return result
    except Exception as e:
        logger.error(f"Scrape failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


@app.get("/fetch-image")
async def fetch_image(url: str):
    """
    Proxy endpoint that fetches a LADBS image URL using a fresh browser session
    and returns the raw bytes. Required because LADBS images need an active session.
    """
    from fastapi.responses import StreamingResponse
    import asyncio
    from playwright.async_api import async_playwright

    logger.info(f"Fetching image: {url}")

    async def get_image_bytes():
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = await context.new_page()
            try:
                # Establish session first
                await page.goto("https://ladbsdoc.lacity.org", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                # Fetch the image using session cookies
                cookies = await context.cookies()
                cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url, headers={
                        "Cookie": cookie_str,
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://ladbsdoc.lacity.org/",
                    }, follow_redirects=True)
                    return resp.content, resp.headers.get("content-type", "application/pdf")
            finally:
                await browser.close()

    try:
        content, content_type = await get_image_bytes()
        import io
        return StreamingResponse(
            io.BytesIO(content),
            media_type=content_type,
            headers={"Content-Disposition": f"inline"}
        )
    except Exception as e:
        logger.error(f"Image fetch failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
