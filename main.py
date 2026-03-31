import os
import io
import httpx
import asyncio
import uvicorn
import logging
import re
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from scraper import LADBSScraper
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_URL = "https://ladbsdoc.lacity.org/IDISPublic_Records/idis"

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

@app.get("/fetch-image")
async def fetch_image(url: str = Query(...)):
    """
    Fetches an actual LADBS document image.
    
    The ImageMain.aspx URL is a frameset containing:
      - frameImgList (ImageList.aspx) — lists available images
      - frameImgDisplay — displays the actual image
    
    We navigate into ImageList.aspx to find the actual image URL,
    then download the raw TIFF/PDF bytes.
    """
    logger.info(f"Fetching image: {url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            # Step 1: Establish session
            await page.goto("https://ladbsdoc.lacity.org", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1)

            # Step 2: Navigate to ImageMain.aspx (the frameset page)
            logger.info(f"Loading frameset: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            # Step 3: Get the ImageList.aspx URL from the frameset
            # The frameset has: <frame id="frameImgList" src="ImageList.aspx" ...>
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")

            image_list_src = None
            for frame in soup.find_all("frame"):
                src = frame.get("src", "")
                if "ImageList" in src or "imagelist" in src.lower():
                    image_list_src = src
                    break

            if not image_list_src:
                # Try iframes too
                for iframe in soup.find_all("iframe"):
                    src = iframe.get("src", "")
                    if "ImageList" in src or "imagelist" in src.lower():
                        image_list_src = src
                        break

            logger.info(f"ImageList src: {image_list_src}")

            if not image_list_src:
                raise HTTPException(status_code=404, detail="Could not find ImageList frame in document")

            # Build full URL for ImageList
            if image_list_src.startswith("http"):
                image_list_url = image_list_src
            else:
                # Relative URL - build from base
                base = url.rsplit("/", 1)[0]
                image_list_url = f"{base}/{image_list_src.lstrip('/')}"

            logger.info(f"Fetching ImageList: {image_list_url}")

            # Step 4: Load ImageList.aspx to get the actual image download link
            img_page = await context.new_page()
            await img_page.goto(image_list_url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            img_html = await img_page.content()
            img_soup = BeautifulSoup(img_html, "html.parser")

            # Look for image download links - LADBS uses GetImage.aspx or similar
            image_download_url = None

            # Check for direct image links
            for a in img_soup.find_all("a"):
                href = a.get("href", "")
                if any(k in href.lower() for k in ["getimage", "download", ".tif", ".pdf", ".jpg", ".png"]):
                    image_download_url = href
                    break

            # Check for JavaScript-based image URLs
            if not image_download_url:
                script_content = img_html
                # Look for GetImage.aspx patterns
                m = re.search(r"GetImage\.aspx[^'\"]*", script_content)
                if m:
                    image_download_url = m.group(0)

            # Check for img src tags
            if not image_download_url:
                for img in img_soup.find_all("img"):
                    src = img.get("src", "")
                    if any(k in src.lower() for k in ["getimage", "image", ".tif"]):
                        image_download_url = src
                        break

            logger.info(f"Image download URL: {image_download_url}")
            logger.info(f"ImageList HTML snippet: {img_html[:500]}")

            await img_page.close()

            if not image_download_url:
                raise HTTPException(status_code=404, detail=f"Could not find image download URL in ImageList. HTML: {img_html[:300]}")

            # Build full download URL
            if not image_download_url.startswith("http"):
                base = image_list_url.rsplit("/", 1)[0]
                image_download_url = f"{base}/{image_download_url.lstrip('/')}"

            logger.info(f"Downloading image from: {image_download_url}")

            # Step 5: Download the actual image using session cookies
            cookies = await context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            async with httpx.AsyncClient(cookies=cookie_dict, follow_redirects=True, timeout=60) as client:
                resp = await client.get(image_download_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": image_list_url,
                })
                logger.info(f"Image download response: {resp.status_code}, content-type: {resp.headers.get('content-type')}, size: {len(resp.content)}")

                content_type = resp.headers.get("content-type", "application/octet-stream")

                # If we got HTML back instead of an image, something went wrong
                if "html" in content_type.lower():
                    raise HTTPException(
                        status_code=422,
                        detail=f"Got HTML instead of image. URL may require different session. Content: {resp.text[:200]}"
                    )

                return StreamingResponse(
                    io.BytesIO(resp.content),
                    media_type=content_type,
                    headers={
                        "Content-Disposition": "inline",
                        "Content-Length": str(len(resp.content)),
                    }
                )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"fetch-image failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await browser.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
