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
pip install -r requirements-dev.txt
playwright install chromium

# Offline test suite — parsers plus a full browser run against a mock
# LADBS site. No internet needed; this is what to run after any change.
python -m pytest tests/ -q

# One-off live scrape straight from the CLI
python test_scraper.py "2100 Cypress Ave, Los Angeles, CA 90065"
python test_scraper.py --ain 5443-016-018

# Run the server
uvicorn main:app --reload --port 8000

# Test it
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{"address": "2100 Cypress Ave, Los Angeles, CA 90065"}'
```

---

## Troubleshooting

Every `/scrape` response carries a `diagnostics` object — the steps the scraper
took, the URLs it landed on, how many parcel checkboxes it saw, and any
warnings. When a scrape comes back with `total_records: 0`, read that first;
the same lines are written to the Railway logs.

| What you see | Likely cause |
| --- | --- |
| `no results grid and no parcel checkboxes at <url>` | LADBS returned a page the scraper didn't recognise — usually a session-expired or "no parcels matched" page. Check the `steps` for the URL and open it in a browser. |
| `no field matched for <field>` | LADBS renamed a form input. Update the selector list in `scraper.py`. |
| `checkboxes_found: 0` on a real parcel | The first search step didn't match — check `parsed_address` / `parsed_ain` in the diagnostics. |
| `detail_error: Session expired` | The detail page was requested outside the search session. |
| Empty records but no warnings | The parcel genuinely has no IDIS documents. |

**Address parsing:** LADBS wants the bare street name. `parse_address` strips
the house number, a directional prefix and the street-type suffix, so
`1234 S San Fernando Rd` is searched as number `1234`, direction `S`, street
`SAN FERNANDO`.

**Why the scraper drives a real browser:** LADBS IDIS is ASP.NET WebForms, so
every form post must carry the `__VIEWSTATE` / `__EVENTVALIDATION` pair the
server issued for *that* page in *that* session. The scraper therefore performs
every step — search, parcel selection, paging and detail pages — in one
Playwright context, and never replays captured tokens over a separate HTTP
client.

---

## Notes

- **Scraping takes 15–45 seconds** depending on how many records LADBS returns. Show a loading state in your UI.
- LADBS is a legacy ASP.NET system — if it goes down or changes, the scraper may need updates.
- Attachments from LADBS are often FileNET viewer links, not direct PDFs. Some may require additional scraping to download.
- Railway's free tier sleeps after inactivity — upgrade to Hobby ($5/mo) for always-on.
