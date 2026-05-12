"""
Job Tracker Scraper.
Sources: Greenhouse API + LinkedIn aggregated public search.
Filters: Senior PM+ in Israel/Remote.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
COMPANIES_FILE = ROOT / "companies.json"
DATA_FILE = ROOT / "data" / "jobs.json"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
HTTP_TIMEOUT = 30


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
# Description parsing
# ============================================================

def parse_description(html_or_text: str) -> dict:
    text = html_to_text(html_or_text)
    sections = {"companyDetails": "", "productDetails": "", "jobDescription": "",
                "requirements": "", "other": ""}
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
        lines = chunk.split("\n", 1)
        chunk = lines[1].strip() if len(lines) > 1 else chunk
        sections[key] = (sections[key] + "\n\n" + chunk).strip() if sections[key] else chunk
    return sections


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</(p|div|li|h[1-6])>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
          .replace("&rsquo;", "'").replace("&lsquo;", "'")
          .replace("&rdquo;", '"').replace("&ldquo;", '"'))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ============================================================
# Greenhouse
# ============================================================

def fetch_greenhouse(company: dict, _ctx=None) -> list:
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


# ============================================================
# LinkedIn aggregator
# ============================================================

LINKEDIN_HEADERS_VARIANTS = [
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    },
    {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Ch-Ua": '"Chromium";v="120", "Not?A_Brand";v="8"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    },
]

LINKEDIN_ENDPOINTS = [
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
    "https://www.linkedin.com/jobs/search",
]


def linkedin_search(target_names: list, errors: list) -> dict:
    """Aggregate LinkedIn search. Returns dict of jobs by matched company name."""
    keyword_variants = [
        "Senior Product Manager",
        "Director Product",
        "VP Product",
        "Head of Product",
        "Group Product Manager",
        "Lead Product Manager",
    ]

    jobs_by_company = {match_name.lower(): [] for _, match_name in target_names}
    seen_ids = set()
    total_fetched = 0
    diagnostics_shown = False

    for endpoint in LINKEDIN_ENDPOINTS:
        for headers in LINKEDIN_HEADERS_VARIANTS:
            endpoint_total = 0
            for kw in keyword_variants:
                for start in (0, 25, 50):
                    params = {
                        "keywords": kw,
                        "location": "Israel",
                        "geoId": "101620260",
                        "f_TPR": "r2592000",
                        "start": str(start),
                    }
                    try:
                        r = requests.get(endpoint, params=params, headers=headers, timeout=HTTP_TIMEOUT)
                    except requests.RequestException as e:
                        errors.append(f"LinkedIn {endpoint} '{kw}': {type(e).__name__}: {e}")
                        break

                    body_size = len(r.content)
                    job_links = []
                    entity_cards = []
                    if "html" in r.headers.get("Content-Type", ""):
                        soup = BeautifulSoup(r.content, "html.parser")
                        # LinkedIn changed URL format from /jobs/view/{id} to /jobs/view/{slug-ending-in-id}.
                        # Find any /jobs/view/ link, AND data-entity-urn cards as a more reliable signal.
                        job_links = soup.find_all("a", href=re.compile(r"/jobs/view/"))
                        entity_cards = soup.find_all(attrs={"data-entity-urn": re.compile(r"jobPosting")})

                    # Diagnostics on first request only
                    if not diagnostics_shown:
                        print(f"  LinkedIn diag: endpoint={endpoint.split('/')[-1]} ua=...{headers['User-Agent'][-30:]}", flush=True)
                        print(f"  LinkedIn diag: HTTP {r.status_code}, {body_size} bytes, content-type={r.headers.get('Content-Type', 'none')[:50]}", flush=True)
                        print(f"  LinkedIn diag: found {len(entity_cards)} jobPosting cards, {len(job_links)} /jobs/view/ links", flush=True)
                        if len(entity_cards) == 0 and len(job_links) == 0:
                            sample = r.text[:600].replace("\n", " ").replace("\r", "")
                            print(f"  LinkedIn diag: response sample (first 600 chars):\n    {sample}", flush=True)
                        diagnostics_shown = True

                    if r.status_code in (429, 999):
                        errors.append(f"LinkedIn {endpoint} '{kw}': HTTP {r.status_code} (blocked)")
                        break
                    if r.status_code != 200:
                        errors.append(f"LinkedIn {endpoint} '{kw}': HTTP {r.status_code}")
                        break

                    # Prefer entity_cards (have data-entity-urn = direct job ID), fall back to job_links
                    items_to_parse = entity_cards if entity_cards else job_links

                    page_count = 0
                    for item in items_to_parse:
                        parsed = _parse_linkedin_item(item)
                        if not parsed:
                            continue
                        jid = parsed["external_id"]
                        if jid in seen_ids:
                            continue
                        seen_ids.add(jid)
                        page_count += 1
                        endpoint_total += 1
                        total_fetched += 1

                        cname = parsed["_company"].lower()
                        for target_name, match_name in target_names:
                            mn = match_name.lower()
                            if mn in cname or cname in mn:
                                jobs_by_company[mn].append({
                                    "external_id": parsed["external_id"],
                                    "title": parsed["title"],
                                    "location": parsed["location"],
                                    "url": parsed["url"],
                                    "updated_at": parsed["updated_at"],
                                    "raw_description": "",
                                })
                                break

                    if page_count == 0:
                        break  # no more results for this keyword
                    time.sleep(2)
                time.sleep(1)

            if endpoint_total > 0:
                # This endpoint+headers combination worked, we're done
                print(f"  LinkedIn: using endpoint={endpoint.split('/')[-1]}", flush=True)
                print(f"  LinkedIn: scanned {total_fetched} unique jobs total", flush=True)
                return jobs_by_company
            else:
                print(f"  LinkedIn: endpoint={endpoint.split('/')[-1]} ua-variant returned 0 jobs, trying next", flush=True)
                # reset diagnostics so we see the next attempt
                diagnostics_shown = False

    # All endpoints/UAs failed
    print(f"  LinkedIn: ALL endpoint+UA combinations returned 0 jobs", flush=True)
    return jobs_by_company


def _parse_linkedin_item(item) -> dict:
    """Parse a job from either a card (with data-entity-urn) or a link (with /jobs/view/ href)."""
    job_id = None

    # Path 1: it's a card with data-entity-urn
    urn = item.get("data-entity-urn") if hasattr(item, "get") else None
    if urn:
        m = re.search(r"jobPosting:(\d+)", urn)
        if m:
            job_id = m.group(1)

    # Find the /jobs/view/ link (either inside the card, or item itself)
    link = None
    if item.name == "a" and "/jobs/view/" in item.get("href", ""):
        link = item
    else:
        try:
            link = item.find("a", href=re.compile(r"/jobs/view/"))
        except Exception:
            link = None

    # Path 2: extract ID from URL if not from urn
    if not job_id and link:
        href = link.get("href", "")
        # LinkedIn slug ends with "-{digits}" usually
        m = re.search(r"-(\d{8,})(?:[/?]|$)", href) or re.search(r"(\d{8,})", href)
        if m:
            job_id = m.group(1)

    if not job_id:
        return None

    # URL
    url = ""
    if link:
        url = link.get("href", "").split("?")[0]

    # Title - try clean h3 first, fall back to aria-label
    title = ""
    for sel in [".base-search-card__title", "h3.base-search-card__title", "h3"]:
        try:
            el = item.select_one(sel)
            if el:
                title = el.get_text(strip=True)
                if title:
                    break
        except Exception:
            continue
    if not title and link and link.get("aria-label"):
        # aria-label like "Senior PM at CompanyName" — strip the "at X" suffix
        al = link["aria-label"].strip()
        title = re.sub(r"\s+at\s+.+$", "", al, flags=re.IGNORECASE) or al

    # Company
    company = ""
    for sel in [".base-search-card__subtitle a", ".base-search-card__subtitle", "h4 a", "h4",
                "[class*=subtitle]", "[class*=company-name]"]:
        try:
            el = item.select_one(sel)
            if el:
                company = el.get_text(strip=True)
                if company:
                    break
        except Exception:
            continue

    # Location
    location = ""
    for sel in [".job-search-card__location", "[class*=location]"]:
        try:
            el = item.select_one(sel)
            if el:
                location = el.get_text(strip=True)
                if location:
                    break
        except Exception:
            continue

    # Date
    updated_at = None
    try:
        time_el = item.find("time")
        if time_el and time_el.get("datetime"):
            updated_at = time_el["datetime"]
    except Exception:
        pass

    if not (title and company):
        return None

    return {
        "external_id": f"li-{job_id}",
        "title": title,
        "_company": company,
        "location": location,
        "url": url,
        "updated_at": updated_at,
    }


def fetch_linkedin(company: dict, ctx: dict) -> list:
    cache = ctx.get("linkedin_results", {})
    match_name = (company.get("linkedin_name") or company["name"]).lower()
    return cache.get(match_name, [])


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "linkedin": fetch_linkedin,
}


# ============================================================
# State + main
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

    seen_ids_by_company = {}
    new_count = 0
    fetch_errors = []
    ctx = {}

    linkedin_companies = [c for c in companies if c["ats"] == "linkedin"]
    if linkedin_companies:
        target_names = [(c["name"], c.get("linkedin_name") or c["name"]) for c in linkedin_companies]
        print(f"Fetching LinkedIn aggregator for {len(target_names)} companies...", flush=True)
        try:
            ctx["linkedin_results"] = linkedin_search(target_names, fetch_errors)
        except Exception as e:
            fetch_errors.append(f"LinkedIn aggregator: {type(e).__name__}: {e}")
            ctx["linkedin_results"] = {}

    for c in companies:
        name = c["name"]
        ats = c["ats"]
        if ats == "skip":
            continue
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            fetch_errors.append(f"{name}: no fetcher for ats={ats}")
            continue

        identifier = c.get("token") or c.get("linkedin_name") or "?"
        print(f"Processing {name} ({ats} / {identifier})...", flush=True)
        try:
            raw_jobs = fetcher(c, ctx)
        except Exception as e:
            fetch_errors.append(f"{name}: {type(e).__name__}: {e}")
            print(f"  ERROR: {e}", flush=True)
            continue

        if not raw_jobs and ats != "linkedin":
            fetch_errors.append(f"{name}: 0 jobs returned (fetch likely failed, not closing)")
            print(f"  WARN: 0 jobs returned, skipping close detection", flush=True)
            continue

        company_ids = set()
        matched = 0
        for raw in raw_jobs:
            if not matches_role(raw["title"], filters):
                continue
            if not matches_location(raw["location"], filters):
                continue
            matched += 1

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
                    "id": job_id, "company": name, "title": raw["title"],
                    "location": raw["location"], "url": raw["url"],
                    "publishDate": publish_date, "discoveredDate": today,
                    "closingDate": None, "status": "open",
                    "details": details,
                    "rawDescription": html_to_text(raw.get("raw_description", ""))[:5000]
                }
                new_count += 1
                print(f"  + NEW: {raw['title']} | {raw['location']}", flush=True)

        print(f"  {name}: {len(raw_jobs)} raw, {matched} match filters", flush=True)
        seen_ids_by_company[name] = company_ids
        if ats != "linkedin":
            time.sleep(0.5)

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
