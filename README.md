# Job Tracker

A small system that scrapes product roles (Senior PM and above, Israel + Remote) from a list of target companies daily, tracks which jobs are open/closed, and surfaces it all in a Claude artifact dashboard.

## Architecture

```
GitHub repo                                  Claude artifact
┌─────────────────────────────────────┐      ┌────────────────────┐
│ .github/workflows/scrape-daily.yml  │      │ job_tracker_       │
│   (cron, runs at 07:00 UTC)         │      │ dashboard.jsx      │
│           │                          │      │                    │
│           ▼                          │      │  Reads jobs.json   │
│ scraper.py  ──► fetch ATS APIs      │      │  on Refresh        │
│   (Greenhouse, Lever, Comeet,        │      │                    │
│    Amazon)                           │ ───► │  Stores locally    │
│           │                          │ fetch│  in window.storage │
│           ▼                          │ raw  │                    │
│ data/jobs.json  (committed back)     │      │  Cards, filters,   │
└─────────────────────────────────────┘      │  detail modal      │
                                              └────────────────────┘
```

## Setup

### 1. Create a GitHub repo

Create a new **public** repo (private also works but needs auth on the raw URL). Drop these files in:

```
job-tracker/
├── README.md
├── companies.json
├── scraper.py
├── requirements.txt
├── data/
│   └── jobs.json          ← create this empty, see below
└── .github/
    └── workflows/
        └── scrape-daily.yml
```

For the initial `data/jobs.json`:

```json
{ "lastUpdated": null, "jobs": [] }
```

### 2. Verify company tokens

`companies.json` lists target companies with their ATS provider and slug. Before first run, **verify each token** — they're best-guesses based on common naming. To check:

- Greenhouse: visit `https://boards.greenhouse.io/{token}` — if the careers page loads, the token is right.
- Lever: `https://jobs.lever.co/{token}` — same idea.
- Comeet: trickier; some companies use custom domains. Check the careers page URL.

If a token is wrong, the scraper logs an error for that company but keeps going.

### 3. Test locally (optional)

```bash
pip install -r requirements.txt
python scraper.py
```

You should see fetch logs for each company and a summary at the end. `data/jobs.json` will be populated.

### 4. Enable GitHub Actions

Push everything to GitHub. Go to repo → Settings → Actions → General → Workflow permissions → set to "**Read and write permissions**". This lets the workflow commit `jobs.json` back to the repo.

Then go to the **Actions** tab and run "Scrape jobs daily" manually once via "Run workflow" to confirm it works. After that it runs daily at 07:00 UTC automatically.

### 5. Hook up the dashboard

Open `job_tracker_dashboard.jsx` as a Claude artifact. Click the Settings gear and paste your raw GitHub URL:

```
https://raw.githubusercontent.com/{your-username}/job-tracker/main/data/jobs.json
```

Save → click Refresh → done.

## How it tracks "open" vs "closed"

- Every job pulled from an ATS that matches the title + location filters gets stored with `status: open`.
- If on the next run a job that was previously open is **no longer returned by the API**, it's marked `closed` with today's date.
- **Safety guard:** if a company returns 0 jobs (likely a fetch error, not actually empty), the scraper does NOT close that company's existing jobs. Errors are logged in `state.fetchErrors`.

## How it categorizes job details

For each job's description, `parse_description()` does a heuristic split looking for common section headers ("About us", "Responsibilities", "Requirements", etc.) and routes content into the five fields the dashboard expects:
- Company details
- Product details
- Job description
- Requirements
- Other

If the heuristic can't find any section headers (some job posts are unstructured), the whole description lands in "Job description".

## Filters

In `companies.json` under `filters`:

- **`titleMustMatch`**: title must contain "product"
- **`seniorityIndicators`**: title must contain one of: senior, sr., staff, principal, lead, group, director, vp, head, chief
- **`excludeTitle`**: excludes Product Marketing, Designer, Engineer, Analyst, Owner, Ops, etc.
- **`locationMustMatch`**: Israel cities OR "remote"
- **`excludeLocation`**: excludes "Remote - US", "Remote India", etc.

Tweak these to your taste. After tweaking, rerun the scraper — the next run will reapply filters on the full ATS response.

## Limitations

- **Skipped companies**: Wix, Google, Microsoft, Meta, Salesforce, Intuit, Apple, Nvidia all use JS-rendered career pages or Workday tenants. They need browser automation (Playwright in CI) to scrape reliably — added complexity that this MVP skips. Amazon is supported via their `amazon.jobs` JSON.
- **Publish date for jobs already on the board**: For jobs we discover on the first run, we use the ATS's reported `updated_at` (Greenhouse) or `createdAt` (Lever). For Comeet that field isn't always reliable. So "publish date" is a best-effort, not always exact.
- **Description parsing**: heuristic, not perfect. Some posts will land mostly in "Job description" and that's fine.

## Adding more companies

Append to `companies.json`. If they use Greenhouse/Lever/Comeet, just add the entry. If they use something custom, add a new `fetch_*()` function in `scraper.py` and register it in `FETCHERS`.
