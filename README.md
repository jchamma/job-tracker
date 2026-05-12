# Job Tracker

Scrapes Senior PM-and-above product roles (Israel + Remote) daily from a list of target companies, tracks open/closed status, displays results in a Claude artifact dashboard.

## What's tracked

**Automated (works end-to-end via GitHub Actions cron):**

| Company | ATS | Reliability |
|---------|-----|-------------|
| Melio | Greenhouse API | ✅ High |
| Forter | Greenhouse API | ✅ High |
| Riskified | Greenhouse API | ✅ High |
| HoneyBook | Greenhouse API | ✅ High |
| Amazon | Amazon Jobs API | ✅ High |
| Monday.com | Custom HTML | ⚠️ Best-effort |
| AppsFlyer | Custom HTML | ⚠️ Best-effort |
| Fiverr | Custom HTML | ⚠️ Best-effort |
| Lightricks | Custom HTML | ⚠️ Best-effort |
| Papaya Global | Custom HTML | ⚠️ Best-effort |
| JFrog | Custom HTML | ⚠️ Best-effort |

"Best-effort" means the parser uses the Comeet job UID regex (`XX.XXX` format) to find jobs in HTML. Works as long as the careers page exposes those UIDs in `<a href>` attributes. If a company redesigns their careers site or hides links behind JavaScript, the scraper silently returns 0 jobs and logs an error rather than mass-closing existing entries (safety guard).

**Manual checking required (weekly):**

| Company | Why skipped |
|---------|-------------|
| Wix, Google, Microsoft, Meta, Salesforce, Intuit, Apple, Nvidia | JS-rendered or Workday-based SPAs; need browser automation |
| Lemonade | Custom careers site, not Greenhouse-public |
| Snyk | JS-rendered SPA at snyk.io/careers |
| Rapyd, Gong, Tipalti | Likely use Greenhouse but their token isn't obvious; verify by trying `boards.greenhouse.io/<guess>` |

For these, set up LinkedIn job alerts as a fallback — LinkedIn scrapes most of them and emails you on new postings.

## Architecture

```
GitHub repo                                  Claude artifact
┌─────────────────────────────────────┐      ┌────────────────────┐
│ .github/workflows/scrape-daily.yml  │      │ job_tracker_       │
│   (cron, 07:00 UTC daily)           │      │ dashboard.jsx      │
│           │                          │      │                    │
│           ▼                          │      │  Reads jobs.json   │
│ scraper.py  ──► Greenhouse API      │      │  on Refresh        │
│              ──► Comeet HTML parse  │ ───► │                    │
│              ──► Amazon Jobs API    │ fetch│  Stores locally    │
│           │                          │ raw  │  in window.storage │
│           ▼                          │      │                    │
│ data/jobs.json  (committed back)     │      │  Cards, filters,   │
└─────────────────────────────────────┘      │  detail modal      │
                                              └────────────────────┘
```

## Setup

### 1. Create a GitHub repo

Public repo (so raw URL works without auth). Drop in:

```
job-tracker/
├── README.md
├── companies.json
├── scraper.py
├── requirements.txt
├── data/
│   └── jobs.json          ← starts as {"lastUpdated": null, "jobs": []}
└── .github/
    └── workflows/
        └── scrape-daily.yml
```

### 2. Enable workflow write permissions

Repo → Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save.

### 3. First run

Actions tab → "Scrape jobs daily" → "Run workflow" → manual trigger. Wait ~1 min. Check `data/jobs.json` got updated.

### 4. Wire up dashboard

In `data/jobs.json`, click "Raw" button, copy URL:
```
https://raw.githubusercontent.com/<you>/job-tracker/main/data/jobs.json
```
Open the dashboard artifact, gear icon, paste URL, Save, Refresh.

## Updating filters

In `companies.json` under `filters`:

- `titleMustMatch` — must contain "product"
- `seniorityIndicators` — at least one of: senior, sr, staff, principal, lead, group, director, vp, head, chief
- `excludeTitle` — excludes: marketing, designer, engineer, analyst, owner, ops, etc.
- `locationMustMatch` — Israeli cities OR "remote"
- `excludeLocation` — excludes "Remote - US", "Remote India", etc.

Edit and commit. Next run re-applies filters.

## Adding more companies

**If they use Greenhouse**: Visit `boards.greenhouse.io/<your-guess>`. If the page loads, add:
```json
{ "name": "CompanyName", "ats": "greenhouse", "token": "your-guess" }
```

**If they use Comeet (job URLs contain `XX.XXX` patterns)**: Add:
```json
{ "name": "CompanyName", "ats": "comeet_html", "url": "https://company.com/careers" }
```

**Custom HTML site**: You'll need to add a new fetcher function in `scraper.py` and register it in the `FETCHERS` dict at the top.

## Verifying unconfirmed Greenhouse tokens

Three companies (Rapyd, Gong, Tipalti) probably use Greenhouse but their slug isn't obvious. Quick way to verify:

1. Open `https://boards.greenhouse.io/<guess>` in browser
2. If the careers page loads → that slug works → update companies.json with `"ats": "greenhouse", "token": "<guess>"`
3. If 404 → try variations (e.g., `gonghire`, `rapydpayments`, `tipaltihq`)

Once a token works, the next workflow run will pick up that company's jobs automatically.

## Limitations

- **Comeet HTML parsing is fragile**: relies on the careers page exposing job links with the Comeet UID pattern. If a company changes their careers page design, the parser may silently return 0 jobs — logged in `fetchErrors`, not catastrophic.
- **Title/location parsing for Comeet jobs**: works well when title/location are in separate HTML elements (the common case). When everything's concatenated in one text node, the parser does its best using a regex for trailing "City, CC" patterns; titles with embedded acronyms (like "VP Product") can get split incorrectly.
- **No detailed description for Comeet jobs**: the scraper only grabs title + location from the listing page, not the full description (would require an extra HTTP call per job). The dashboard shows just the title/location/link for those.
