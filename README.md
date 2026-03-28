# LADBS Scraper Service

A FastAPI + Playwright microservice that scrapes the LA Department of Building & Safety (LADBS) IDIS document system by address. Deploy on Railway, call from your Replit Next.js app.

---

## Deploy to Railway

### 1. Push this folder to a GitHub repo

```bash
git init
git add .
git commit -m "initial"
gh repo create ladbs-scraper --public --push
```

### 2. Deploy on Railway

1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **New Project → Deploy from GitHub repo**
3. Select your `ladbs-scraper` repo
4. Railway will auto-detect the Dockerfile and build it
5. Once deployed, go to **Settings → Networking → Generate Domain**
6. Copy your public URL, e.g. `https://ladbs-scraper-production.up.railway.app`

> **First deploy takes ~5 minutes** — Chromium is large. Subsequent deploys are faster.

---

## API Endpoints

### `GET /health`
Returns `{"status": "ok"}` — use to confirm the service is running.

### `POST /scrape`
```json
{
  "address": "2100 Cypress Ave, Los Angeles, CA 90065"
}
```

**Response:**
```json
{
  "address": "2100 Cypress Ave, Los Angeles, CA 90065",
  "total_records": 12,
  "summary": "Found 12 record(s) for 2100 Cypress Ave...\n  • Building Permit: 7\n  • Certificate of Occupancy: 3\n  • Plan Check: 2\nTotal attachments available: 9",
  "records": [
    {
      "doc_type": "Building Permit",
      "doc_number": "19010-10000-12345",
      "doc_date": "2019-03-15",
      "status": "Finaled",
      "address": "2100 CYPRESS AVE",
      "url": "https://ladbsdoc.lacity.org/...",
      "attachments": [
        {
          "label": "View Digital Image",
          "url": "https://ladbsdoc.lacity.org/..."
        }
      ]
    }
  ],
  "attachments": [
    {
      "label": "View Digital Image",
      "url": "https://ladbsdoc.lacity.org/..."
    }
  ]
}
```

---

## Add to Your Replit Next.js App

### 1. Set your Railway URL as an environment variable

In Replit → **Secrets**, add:
```
LADBS_SCRAPER_URL=https://your-service.up.railway.app
```

### 2. Create the API route

Create `app/api/ladbs/route.ts` (or `pages/api/ladbs.ts` for Pages Router):

```typescript
// app/api/ladbs/route.ts  (App Router)
import { NextRequest, NextResponse } from "next/server";

const SCRAPER_URL = process.env.LADBS_SCRAPER_URL;

export async function POST(req: NextRequest) {
  const { address } = await req.json();

  if (!address) {
    return NextResponse.json({ error: "address required" }, { status: 400 });
  }

  try {
    const res = await fetch(`${SCRAPER_URL}/scrape`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ address }),
    });

    if (!res.ok) {
      const err = await res.text();
      return NextResponse.json({ error: err }, { status: res.status });
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch (err: any) {
    return NextResponse.json({ error: err.message }, { status: 500 });
  }
}
```

### 3. Call it from your frontend component

```typescript
async function searchLADBS(address: string) {
  const res = await fetch("/api/ladbs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address }),
  });
  return res.json();
  // Returns: { address, total_records, summary, records, attachments }
}
```

---

## Local Testing (before Railway deploy)

```bash
# Install deps
pip install -r requirements.txt
playwright install chromium

# Run the server
uvicorn main:app --reload --port 8000

# Test it
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{"address": "2100 Cypress Ave, Los Angeles, CA 90065"}'
```

---

## Notes

- **Scraping takes 15–45 seconds** depending on how many records LADBS returns. Show a loading state in your UI.
- LADBS is a legacy ASP.NET system — if it goes down or changes, the scraper may need updates.
- Attachments from LADBS are often FileNET viewer links, not direct PDFs. Some may require additional scraping to download.
- Railway's free tier sleeps after inactivity — upgrade to Hobby ($5/mo) for always-on.
