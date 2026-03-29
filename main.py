import os
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
