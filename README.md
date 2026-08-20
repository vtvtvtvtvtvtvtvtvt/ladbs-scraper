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
  "address": "2100 Cypress Ave, Los Angeles, CA 90065",
  "ain": "5467018015",
  "include_details": true,
  "time_budget_seconds": 150,
  "parcel_mode": "all"
}
```

Unknown fields are **rejected with a 422**, not ignored — an option that is
silently dropped makes a caller believe it took effect.

`time_budget_seconds` (10–900) overrides `SCRAPE_TIMEOUT_SECONDS` for one
request, so a caller who can wait longer does not need the service redeployed.

`parcel_mode` (`all` by default) decides how a multi-parcel address is handled.
An LA address can match a couple of dozen assessor parcels: `all` ticks every
parcel and submits once, `each` walks them one at a time — one full search per
parcel, which is far slower and exists only as a fallback.

Send `address` **or** `ain` (`ain` wins if both are present). An `address` that
is really a parcel number is detected and routed to the AIN search.

`include_details` (default `true`) controls whether the scraper opens the
detail page for every record. Those pages are the bulk of a scrape's runtime —
a parcel with 500 documents means 500 extra round trips — and they only add
fields like `status` and `applicant`. **If you render the grid columns
(`doc_type`, `sub_type`, `doc_date`, `doc_number`, `digital_image_url`), send
`"include_details": false` and results come back in a fraction of the time.**

Every response carries a `status`:

| `status` | meaning | how a caller should treat it |
| --- | --- | --- |
| `ok` | search completed, records returned | success |
| `partial` | records returned, but the time budget ran out first | success, flag as incomplete; raise `SCRAPE_TIMEOUT_SECONDS` or turn off details |
| `no_records` | search completed, the parcel genuinely has nothing | success — show a quiet "no records", not an error |
| `blocked` | upstream refused the request (block page, session expiry, missing form) | **failure** — do not show as "no records"; retry later |

`no_records` and `blocked` both come back with zero records, so a caller that
only counts records cannot tell a dead scrape from an empty parcel. Branch on
`status`.

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
| `status: "blocked"` | Upstream served a block or error page. `page_snapshot.visible_text` quotes it. |
| `status: "partial"` | The time budget ran out. Raise `SCRAPE_TIMEOUT_SECONDS` or pass `include_details: false`. |
| Empty records but no warnings | The parcel genuinely has no IDIS documents. |

**LADBS only covers the City of Los Angeles.** Pasadena, Glendale, Burbank,
Santa Monica and the rest of LA County run their own building departments, so
IDIS will never return anything for those addresses. When the address names one
of them, the summary says so instead of just reporting nothing found.

**AIN in the address field:** `POST /scrape` with `{"address": "5467018015"}`
is detected as a parcel number and routed to the AIN search, so a caller that
passes an AIN where an address is expected still gets results.

**Timeouts:** a scrape stops collecting at `SCRAPE_TIMEOUT_SECONDS` (default
150) and returns what it has with `status: "partial"`. Keep your client's own
timeout **above** this value — otherwise the client aborts a request the
service was about to answer, and you lose records that were already collected.

**Parcel fan-out:** an address matching many parcels used to mean one complete
search per parcel, which is what pushed busy addresses past the budget.
Searching by AIN avoids the fan-out entirely by resolving to a single parcel,
so pass `ain` whenever you have one.

**Concurrency:** detail pages are fetched `LADBS_DETAIL_CONCURRENCY` at a time
(default 6) through one browser context, which shares the session cookie. Raise
it for speed, lower it if LADBS starts throttling.

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

## If you keep a copy of this service in another repo

Copies drift. The scrape path here talks to LADBS **only** through the
Playwright browser context — it does not build its own HTTP requests and does
not set `Referer`, `Origin` or a hand-written `User-Agent` on any LADBS
request. An older revision did, replaying `__VIEWSTATE` over `httpx` with
forged headers; that pattern is both session-fragile and the shape upstream
filtering rejects, and a copy still carrying it can return empty results that
look like clean successes. If you are syncing a vendored copy, take this
version wholesale rather than porting individual changes onto the old one.

---

## Notes

- **Scraping takes 15–45 seconds** depending on how many records LADBS returns. Show a loading state in your UI.
- LADBS is a legacy ASP.NET system — if it goes down or changes, the scraper may need updates.
- Attachments from LADBS are often FileNET viewer links, not direct PDFs. Some may require additional scraping to download.
- Railway's free tier sleeps after inactivity — upgrade to Hobby ($5/mo) for always-on.
