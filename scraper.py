"""
Job Tracker Scraper
Fetches product roles from each company's ATS, filters to Senior PM and above
in Israel + Remote, and diffs against existing state to detect new/closed jobs.

Usage:
    python scraper.py

Reads:
    companies.json - target companies + filter config
    data/jobs.json - current state (or empty if first run)

Writes:
    data/jobs.json - updated state
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
COMPANIES_FILE = ROOT / "companies.json"
DATA_FILE = ROOT / "data" / "jobs.json"
USER_AGENT = "Mozilla/5.0 (compatible; JobTrackerBot/1.0)"
HTTP_TIMEOUT = 30

# ============================================================
# Filtering
# ============================================================

def matches_role(title: str, filters: dict) -> bool:
    """Check if a job title matches our 'Senior PM and above' criteria."""
    if not title:
        return False
    t = title.lower()
    if not all(needle.lower() in t for needle in filters["titleMustMatch"]):
        return False
    if any(bad.lower() in t for bad in filters["excludeTitle"]):
        return False
    if not any(ind.lower() in t for ind in filters["seniorityIndicators"]):
        return False
    return True


def matches_location(location: str, filters: dict) -> bool:
    """Check if a job location is Israel or Remote (and not US-only remote)."""
    if not location:
        return False
    loc = location.lower()
    if any(bad.lower() in loc for bad in filters["excludeLocation"]):
        return False
    return any(good.lower() in loc for good in filters["locationMustMatch"])


# ============================================================
# Description parsing (heuristic)
# ============================================================

def parse_description(html_or_text: str) -> dict:
    """Heuristically split a job description into our 5 categories."""
    text = html_to_text(html_or_text)
    sections = {
        "companyDetails": "",
        "productDetails": "",
        "jobDescription": "",
        "requirements": "",
        "other": ""
    }

    # Section header keywords to look for
    patterns = [
        (r"(?im)^(about (the |our )?(company|us|team)|who we are|company overview)", "companyDetails"),
        (r"(?im)^(about (the )?(product|role)|product overview|the role)", "productDetails"),
        (r"(?im)^(what you('?ll| will) do|responsibilities|your role|job description|what we offer you|the job|day to day)", "jobDescription"),
        (r"(?im)^(requirements|qualifications|what you('?ll| will) bring|what we('?re| are) looking for|skills|must have|nice to have|preferred)", "requirements"),
    ]

    # Find all section boundaries
    boundaries = []
    for pat, key in patterns:
        for m in re.finditer(pat, text):
            boundaries.append((m.start(), key))
    boundaries.sort()

    if not boundaries:
        # Couldn't parse - dump everything into jobDescription
        sections["jobDescription"] = text.strip()
        return sections

    # Anything before the first boundary goes to companyDetails (often the intro)
    if boundaries[0][0] > 0:
        sections["companyDetails"] = text[:boundaries[0][0]].strip()

    # Walk boundaries
    for i, (start, key) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        chunk = text[start:end].strip()
        # Skip the header line itself for cleanliness
        chunk_lines = chunk.split("\n", 1)
        chunk = chunk_lines[1].strip() if len(chunk_lines) > 1 else chunk
        sections[key] = (sections[key] + "\n\n" + chunk).strip() if sections[key] else chunk

    return sections


def html_to_text(s: str) -> str:
    """Strip HTML tags, decode entities, normalize whitespace."""
    if not s:
        return ""
    # Replace common block tags with newlines
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</(p|div|li|h[1-6])>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.IGNORECASE)
    # Strip remaining tags
    s = re.sub(r"<[^>]+>", "", s)
    # Decode common entities
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'")
          .replace("&rsquo;", "'").replace("&lsquo;", "'")
          .replace("&rdquo;", '"').replace("&ldquo;", '"'))
    # Collapse whitespace
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ============================================================
# ATS fetchers
# ============================================================

def fetch_greenhouse(token: str) -> list:
    """Fetch jobs from a Greenhouse board. Returns list of normalized jobs."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "external_id": str(j.get("id")),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "updated_at": j.get("updated_at"),
            "raw_description": j.get("content", "")
        })
    return jobs


def fetch_lever(token: str) -> list:
    """Fetch jobs from a Lever board."""
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data:
        location = ((j.get("categories") or {}).get("location") or "")
        jobs.append({
            "external_id": j.get("id", ""),
            "title": j.get("text", ""),
            "location": location,
            "url": j.get("hostedUrl", ""),
            "updated_at": ms_to_iso(j.get("createdAt")),
            "raw_description": (j.get("descriptionPlain", "") or j.get("description", ""))
        })
    return jobs


def fetch_comeet(token: str) -> list:
    """Fetch jobs from a Comeet careers page. Comeet exposes a JSON feed at /careers-api/2.0/company/{uid}/positions."""
    # Comeet's URL structure can vary; this is a best-effort fetch from their public JSON.
    # Try the well-known pattern first.
    candidates = [
        f"https://www.comeet.com/careers-api/2.0/company/{token}/positions",
        f"https://www.{token}.com/careers/positions.json"
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                continue
            data = r.json()
            positions = data if isinstance(data, list) else data.get("positions", [])
            if not positions:
                continue
            jobs = []
            for p in positions:
                jobs.append({
                    "external_id": str(p.get("uid", p.get("id", ""))),
                    "title": p.get("name", p.get("title", "")),
                    "location": format_comeet_location(p),
                    "url": p.get("url_active", p.get("url", "")),
                    "updated_at": p.get("time_updated"),
                    "raw_description": (p.get("description", "") or "") + "\n\n" + (p.get("requirements", "") or "")
                })
            return jobs
        except Exception:
            continue
    return []


def format_comeet_location(p: dict) -> str:
    parts = []
    if p.get("location"):
        loc = p["location"]
        if isinstance(loc, dict):
            for k in ("city", "country"):
                if loc.get(k):
                    parts.append(str(loc[k]))
        else:
            parts.append(str(loc))
    return ", ".join(parts)


def fetch_amazon(_token: str) -> list:
    """Fetch from Amazon Jobs search API filtered to Product Management in Israel + Remote."""
    url = "https://www.amazon.jobs/en/search.json"
    params = {
        "result_limit": 100,
        "sort": "recent",
        "job_function_id[]": "job_function_corporate_80",  # Product Management
        "country[]": "ISR",
        "country[]": "Remote",
    }
    try:
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json()
        jobs = []
        for j in data.get("jobs", []):
            jobs.append({
                "external_id": str(j.get("id_icims", j.get("id"))),
                "title": j.get("title", ""),
                "location": j.get("normalized_location", j.get("location", "")),
                "url": "https://www.amazon.jobs" + j.get("job_path", ""),
                "updated_at": j.get("posted_date"),
                "raw_description": (j.get("description", "") + "\n\n" + j.get("basic_qualifications", ""))
            })
        return jobs
    except Exception:
        return []


def ms_to_iso(ms):
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "comeet": fetch_comeet,
    "amazon": fetch_amazon,
}


# ============================================================
# Main
# ============================================================

def load_state() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"lastUpdated": None, "jobs": []}


def save_state(state: dict):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def make_id(company: str, external_id: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "-", company.lower())
    return f"{safe}-{external_id}"


def main():
    with open(COMPANIES_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    filters = cfg["filters"]
    companies = cfg["companies"]

    state = load_state()
    existing = {j["id"]: j for j in state.get("jobs", [])}
    today = datetime.now(timezone.utc).date().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Collect IDs we see this run, per company (so we know which jobs closed)
    seen_ids_by_company = {}
    new_count = 0
    fetch_errors = []

    for c in companies:
        name = c["name"]
        ats = c["ats"]
        if ats == "skip":
            continue
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            fetch_errors.append(f"{name}: no fetcher for ats={ats}")
            continue

        print(f"Fetching {name} ({ats}/{c['token']})...", flush=True)
        try:
            raw_jobs = fetcher(c["token"])
        except Exception as e:
            fetch_errors.append(f"{name}: {e}")
            print(f"  ERROR: {e}", flush=True)
            continue

        # If a company returns zero jobs from the API, treat as fetch error
        # (don't close out every existing job for that company!)
        if not raw_jobs:
            fetch_errors.append(f"{name}: 0 jobs returned (treating as fetch error, not closing existing)")
            print(f"  WARN: 0 jobs returned, skipping close detection", flush=True)
            continue

        company_ids = set()
        for raw in raw_jobs:
            if not matches_role(raw["title"], filters):
                continue
            if not matches_location(raw["location"], filters):
                continue

            job_id = make_id(name, raw["external_id"])
            company_ids.add(job_id)

            details = parse_description(raw.get("raw_description", ""))

            if job_id in existing:
                # Already tracked - update mutable fields if still open
                j = existing[job_id]
                if j["status"] == "closed":
                    # Job reopened (rare but possible)
                    j["status"] = "open"
                    j["closingDate"] = None
                j["title"] = raw["title"]
                j["location"] = raw["location"]
                j["url"] = raw["url"]
                j["details"] = details
                j["rawDescription"] = html_to_text(raw.get("raw_description", ""))[:5000]
            else:
                # New job
                publish_date = (raw.get("updated_at") or now_iso)[:10]
                # Don't trust a publish_date in the far past for a job we're seeing for first time;
                # publishDate is "when it could be applied to" so we use the API's date or today.
                existing[job_id] = {
                    "id": job_id,
                    "company": name,
                    "title": raw["title"],
                    "location": raw["location"],
                    "url": raw["url"],
                    "publishDate": publish_date,
                    "discoveredDate": today,
                    "closingDate": None,
                    "status": "open",
                    "details": details,
                    "rawDescription": html_to_text(raw.get("raw_description", ""))[:5000]
                }
                new_count += 1
                print(f"  + NEW: {raw['title']} | {raw['location']}", flush=True)

        seen_ids_by_company[name] = company_ids
        time.sleep(0.5)  # be polite

    # Detect closed jobs: anything in existing that belongs to a successfully-fetched
    # company but wasn't seen this run
    closed_count = 0
    for jid, j in existing.items():
        if j["status"] != "open":
            continue
        company = j["company"]
        if company not in seen_ids_by_company:
            # Company wasn't fetched successfully - don't close
            continue
        if jid not in seen_ids_by_company[company]:
            j["status"] = "closed"
            j["closingDate"] = today
            closed_count += 1
            print(f"  - CLOSED: {j['title']} @ {company}", flush=True)

    # Save
    state["lastUpdated"] = now_iso
    state["jobs"] = list(existing.values())
    state["fetchErrors"] = fetch_errors
    save_state(state)

    open_count = sum(1 for j in state["jobs"] if j["status"] == "open")
    print("\n=== Summary ===")
    print(f"  New: {new_count}")
    print(f"  Closed: {closed_count}")
    print(f"  Open total: {open_count}")
    print(f"  Tracked total: {len(state['jobs'])}")
    if fetch_errors:
        print(f"  Errors: {len(fetch_errors)}")
        for e in fetch_errors:
            print(f"    - {e}")


if __name__ == "__main__":
    main()
