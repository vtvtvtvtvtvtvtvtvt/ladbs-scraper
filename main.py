from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from scraper import LADBSScraper
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LADBS Scraper API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Lock this down to your Replit domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScrapeRequest(BaseModel):
    address: str  # e.g. "2100 Cypress Ave, Los Angeles, CA 90065"

class HealthResponse(BaseModel):
    status: str

@app.get("/health", response_model=HealthResponse)
def health():
    return {"status": "ok"}

@app.post("/scrape")
async def scrape(request: ScrapeRequest):
    logger.info(f"Scrape request for: {request.address}")
    try:
        scraper = LADBSScraper()
        result = scraper.scrape(request.address)
        return result
    except Exception as e:
        logger.error(f"Scrape failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
