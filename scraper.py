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
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
COMPANIES_FILE = ROOT / "companies.json"
DATA_FILE = ROOT / "data" / "jobs.json"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
HTTP_TIMEOUT = 30

COMEET_UID_RE = re.compile(r"([A-F0-9]{2}\.[A-F0-9]{3})", re.IGNORECASE)


# ============================================================
# Filtering
# ============================================================

def matches_role(title: str, filters: dict) -> bool:
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
    text = html_to_text(html_or_text)
    sections = {
        "companyDetails": "",
        "productDetails": "",
        "jobDescription": "",
        "requirements": "",
        "other": ""
    }

    patterns = [
        (r"(?im)^(about (the |our )?(company|us|team)|who we are|company overview)", "companyDetails"),
        (r"(?im)^(about (the )?(product|role)|product overview|the role)", "productDetails"),
        (r"(?im)^(what you('?ll| will) do|responsibilities|your role|job description|the job|day to day)", "jobDescription"),
        (r"(?im)^(requirements|qualifications|what you('?ll| will) bring|what we('?re| are) looking for|skills|must have|nice to have|preferred)", "requirements"),
    ]

    boundaries = []
    for pat, key in patterns:
        for m in re.finditer(pat, text):
            boundaries.append((m.start(), key))
    boundaries.sort()

    if not boundaries:
        sections["jobDescription"] = text.strip()
        return sections

    if boundaries[0][0] > 0:
        sections["companyDetails"] = text[:boundaries[0][0]].strip()

    for i, (start, key) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        chunk = text[start:end].strip()
        chunk_lines = chunk.split("\n", 1)
        chunk = chunk_lines[1].strip() if len(chunk_lines) > 1 else chunk
        sections[key] = (sections[key] + "\n\n" + chunk).strip() if sections[key] else chunk

    return sections


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</(p|div|li|h[1-6])>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'")
          .replace("&rsquo;", "'").replace("&lsquo;", "'")
          .replace("&rdquo;", '"').replace("&ldquo;", '"'))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ============================================================
# ATS fetchers
# ============================================================

def fetch_greenhouse(company: dict) -> list:
    token = company["token"]
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


def fetch_comeet_html(company: dict) -> list:
    """
    Generic HTML scraper for any careers page that uses Comeet job UIDs (XX.XXX format).
    Finds all <a> tags whose href contains a Comeet UID, extracts title + location from text.
    Tries best-effort split of concatenated text into title and location.
    """
    url = company["url"]
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    jobs = []
    seen_uids = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        m = COMEET_UID_RE.search(href)
        if not m:
            continue
        uid = m.group(1).upper()
        if uid in seen_uids:
            continue
        seen_uids.add(uid)

        # Get text from this anchor. Use separator to detect element boundaries.
        raw_text = link.get_text(separator=" | ", strip=True)
        # Often comes out like "Senior Product Manager | Product | Tel Aviv, IL"
        parts = [p.strip() for p in raw_text.split("|") if p.strip()]

        title, location = _split_title_location(parts, raw_text)

        full_url = href if href.startswith("http") else urljoin(url, href)

        jobs.append({
            "external_id": uid,
            "title": title,
            "location": location,
            "url": full_url,
            "updated_at": None,
            "raw_description": ""  # detail fetch is per-job and adds load; skip for now
        })

    return jobs


def _split_title_location(parts: list, raw_text: str) -> tuple:
    """
    Best-effort split of concatenated job title/team/location text.
    Strategy:
      1. If multiple parts (separator worked), title = first, location = last
      2. Otherwise, regex for 'City, Country' or 'Remote' suffix at end of string
    """
    if len(parts) >= 2:
        return parts[0], parts[-1]

    if not raw_text:
        return "Unknown", ""

    # Trailing location pattern: "City, CC" (1-2 word cities) or "Remote, REGION" or "Remote"
    # City words require at least one lowercase letter (so acronyms like "PM" aren't treated as cities)
    loc_match = re.search(
        r"^(.*?)\s+("
        r"Remote(?:\s*[-,]\s*[A-Za-z]{2,}(?:\s+[A-Za-z]{2,})*)?"
        r"|[A-Z][a-z][a-zA-Z\-]*(?:\s+[A-Z][a-z][a-zA-Z\-]*)?,\s*[A-Z]{2,3}"
        r")\s*$",
        raw_text
    )
    if loc_match:
        return loc_match.group(1).strip(), loc_match.group(2).strip()
    return raw_text.strip(), ""


def fetch_amazon(company: dict) -> list:
    """Amazon Jobs search filtered to Product Management in Israel + Remote."""
    url = "https://www.amazon.jobs/en/search.json"
    # Use list of tuples to allow repeated query params (country[]=ISR&country[]=Remote)
    params = [
        ("result_limit", "100"),
        ("sort", "recent"),
        ("job_function_id[]", "job_function_corporate_80"),  # Product Management
        ("country[]", "ISR"),
        ("country[]", "Remote"),
    ]
    try:
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json()
        jobs = []
        for j in data.get("jobs", []):
            jobs.append({
                "external_id": str(j.get("id_icims", j.get("id", ""))),
                "title": j.get("title", ""),
                "location": j.get("normalized_location", j.get("location", "")),
                "url": "https://www.amazon.jobs" + j.get("job_path", ""),
                "updated_at": j.get("posted_date"),
                "raw_description": (j.get("description", "") + "\n\n" + j.get("basic_qualifications", ""))
            })
        return jobs
    except Exception:
        return []


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "comeet_html": fetch_comeet_html,
    "amazon": fetch_amazon,
}


# ============================================================
# State management
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


# ============================================================
# Main
# ============================================================

def main():
    with open(COMPANIES_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    filters = cfg["filters"]
    companies = cfg["companies"]

    state = load_state()
    existing = {j["id"]: j for j in state.get("jobs", [])}
    today = datetime.now(timezone.utc).date().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

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

        identifier = c.get("token") or c.get("url", "?")
        print(f"Fetching {name} ({ats} / {identifier})...", flush=True)
        try:
            raw_jobs = fetcher(c)
        except Exception as e:
            fetch_errors.append(f"{name}: {type(e).__name__}: {e}")
            print(f"  ERROR: {e}", flush=True)
            continue

        if not raw_jobs:
            fetch_errors.append(f"{name}: 0 jobs returned (treating as fetch error, not closing existing)")
            print(f"  WARN: 0 jobs returned, skipping close detection for {name}", flush=True)
            continue

        company_ids = set()
        matched_count = 0
        for raw in raw_jobs:
            if not matches_role(raw["title"], filters):
                continue
            if not matches_location(raw["location"], filters):
                continue
            matched_count += 1

            job_id = make_id(name, raw["external_id"])
            company_ids.add(job_id)

            details = parse_description(raw.get("raw_description", ""))

            if job_id in existing:
                j = existing[job_id]
                if j["status"] == "closed":
                    j["status"] = "open"
                    j["closingDate"] = None
                j["title"] = raw["title"]
                j["location"] = raw["location"]
                j["url"] = raw["url"]
                if any(details.values()):
                    j["details"] = details
                j["rawDescription"] = html_to_text(raw.get("raw_description", ""))[:5000]
            else:
                publish_date = (raw.get("updated_at") or now_iso)[:10]
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

        print(f"  {name}: {len(raw_jobs)} total, {matched_count} match filters", flush=True)
        seen_ids_by_company[name] = company_ids
        time.sleep(0.5)

    # Detect closed jobs
    closed_count = 0
    for jid, j in existing.items():
        if j["status"] != "open":
            continue
        company = j["company"]
        if company not in seen_ids_by_company:
            continue
        if jid not in seen_ids_by_company[company]:
            j["status"] = "closed"
            j["closingDate"] = today
            closed_count += 1
            print(f"  - CLOSED: {j['title']} @ {company}", flush=True)

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
