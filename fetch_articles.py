#!/usr/bin/env python3
"""Fetch newly published articles from journals.json using the free Europe PMC API.

Designed for scheduled GitHub Actions. Python 3.10+; standard library only.
Data source limitation: Europe PMC does not cover every journal or online-first article.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
# Check the preceding three weeks each day to catch delayed indexing.
LOOKBACK_DAYS = 21
# Retain the previous 90 days on the site for manageable file size.
RETENTION_DAYS = 90
PAGE_SIZE = 100
MAX_PAGES_PER_JOURNAL = 12

# Broad orthopaedic terms for screening journals on list B. The user's more
# specialized keywords are used in addition to these terms, not replaced.
ORTHOPAEDIC_TERMS = [
    "orthop", "orthopaed", "musculoskeletal", "bone", "joint", "cartilage",
    "fracture", "dislocation", "osteotomy", "osteosynthesis", "osteoarthritis",
    "arthroplasty", "arthroscopy", "tendon", "ligament", "menisc", "knee",
    "hip", "acetabul", "femoral", "tibial", "humer", "pelvi", "spine",
    "spinal", "vertebr", "lumbar", "cervical", "shoulder", "rotator cuff",
    "wrist", "ankle", "foot", "hand surgery", "scoliosis", "osteoporos",
    "osteonecros", "nonunion", "non-union", "implant", "prosthes",
    "amput", "limb salvage", "osteosarcoma", "bone tumor", "bone tumour",
    "glenoid", "scapul", "radius", "ulna", "calcane", "reconstruction",
    "sports injur", "acl", "pcl", "mcl", "achilles", "biomechanic",
    "skeletal", "sarcoma", "hip fracture", "knee replacement",
]
# Very short phrases often have unrelated meanings; require contextual match.
AMBIGUOUS_KEYWORDS = {"ar", "mr", "vr", "xr", "psi", "slm", "sls", "ebm", "fdm", "sla", "cad", "cam", "ai", "vsp"}


def read_json(path: Path, fallback):
    if not path.exists():
        return fallback
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()


def contains_phrase(text: str, phrase: str) -> bool:
    """Avoid substring hits for acronyms (e.g. AR in 'heart')."""
    p = phrase.casefold().strip()
    if not p:
        return False
    return bool(re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", text.casefold()))


def load_keywords() -> list[str]:
    path = ROOT / "keywords.txt"
    if not path.exists():
        raise FileNotFoundError("keywords.txt was not found beside fetch_articles.py")
    lines = path.read_text(encoding="utf-8").splitlines()
    return list(dict.fromkeys(x.strip() for x in lines if x.strip() and not x.lstrip().startswith("#")))


def clean_html(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", text or ""))).strip()


def api_get(query: str, cursor: str) -> dict:
    params = urlencode({
        "query": query, "format": "json", "resultType": "core",
        "pageSize": PAGE_SIZE, "cursorMark": cursor,
    })
    request = Request(API + "?" + params,
        headers={"User-Agent": "OrthopaedicResearchMonitor/1.0 (personal noncommercial monitoring)"})
    last_error = None
    for attempt in range(4):
        try:
            with urlopen(request, timeout=40) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, ValueError) as e:
            last_error = e
            if attempt < 3:
                time.sleep(2 ** attempt + 0.25)
    raise RuntimeError(f"Europe PMC request failed: {last_error}")


def parse_date(raw: str) -> str:
    # Europe PMC may supply a date as YYYY, YYYY-MM, or YYYY-MM-DD.
    raw = (raw or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return raw + "-01"
    if re.fullmatch(r"\d{4}", raw):
        return raw + "-01-01"
    return ""


def title_matches_journal(actual: str, chosen: str) -> bool:
    # Purposefully conservative: this limits cross-journal false positives.
    a, b = normalize(actual), normalize(chosen)
    if a == b:
        return True
    return a.removeprefix("the ") == b.removeprefix("the ")


def article_from_record(raw: dict, chosen_journal: dict, keywords: list[str]) -> dict | None:
    title = clean_html(raw.get("title", ""))
    actual_journal = raw.get("journalTitle", "") or (raw.get("journalInfo") or {}).get("journal", {}).get("title", "")
    if not title or (actual_journal and not title_matches_journal(actual_journal, chosen_journal["name"])):
        return None
    abstract = clean_html(raw.get("abstractText", ""))
    text = " ".join([title, abstract]).casefold()
    matches = [k for k in keywords if contains_phrase(text, k)]
    # Context disambiguation: short abbreviations alone cannot classify a paper.
    meaningful_matches = [k for k in matches if k.casefold() not in AMBIGUOUS_KEYWORDS]
    ortho_match = any(t in text for t in ORTHOPAEDIC_TERMS)
    if chosen_journal["collection"] == "keywords" and not (ortho_match or meaningful_matches):
        return None
    doi = (raw.get("doi") or "").strip()
    pmid = (raw.get("pmid") or "").strip()
    source = raw.get("source", "")
    identifier = str(raw.get("id", ""))
    key = "doi:" + doi.lower() if doi else "pmid:" + pmid if pmid else source + ":" + identifier
    if not key.strip(":"):
        return None
    published = parse_date(raw.get("firstPublicationDate", "") or raw.get("firstIndexDate", ""))
    return {
        "id": key,
        "title": title,
        "journal": chosen_journal["name"],
        "journal_list": chosen_journal["list"],
        "authors": raw.get("authorString", "") or "",
        "published_date": published,
        "doi": doi,
        "pmid": pmid,
        "url": "https://doi.org/" + doi if doi else
               ("https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/" if pmid else
                "https://europepmc.org/article/" + source + "/" + identifier),
        "abstract": abstract,
        "open_access": (raw.get("isOpenAccess") or "").lower() == "y",
        "categories": ["Digital Orthopaedics & Surgical Technology"] if meaningful_matches else ["Other / Unclassified"],
        "matched_keywords": meaningful_matches,
    }


def fetch_journal(journal: dict, keywords: list[str], from_date: str, through_date: str):
    name = journal["name"]
    # Quote-escape to create a valid Europe PMC journal field query.
    safe_name = name.replace('"', "")
    query = f'JOURNAL:"{safe_name}" AND FIRST_PDATE:[{from_date} TO {through_date}] AND (SRC:MED OR SRC:PMC)'
    articles = []
    cursor = "*"
    total = 0
    pages = 0
    while pages < MAX_PAGES_PER_JOURNAL:
        data = api_get(query, cursor)
        page = (data.get("resultList") or {}).get("result", [])
        total = int(data.get("hitCount", 0))
        pages += 1
        for raw in page:
            article = article_from_record(raw, journal, keywords)
            if article:
                articles.append(article)
        next_cursor = data.get("nextCursorMark", "")
        if not page or not next_cursor or next_cursor == cursor or pages * PAGE_SIZE >= total:
            break
        cursor = next_cursor
    return name, articles, {"returned": total, "accepted": len(articles), "pages_checked": pages,
                             "possibly_truncated": total > pages * PAGE_SIZE}


def main():
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    cutoff = today - timedelta(days=RETENTION_DAYS)
    journals = read_json(ROOT / "journals.json", {}).get("journals", [])
    if not journals:
        raise ValueError("journals.json has no journals")
    keywords = load_keywords()
    output = ROOT / "data"
    output.mkdir(parents=True, exist_ok=True)
    existing = read_json(output / "articles.json", [])
    if not isinstance(existing, list):
        raise ValueError("data/articles.json must contain a JSON array")
    by_id = {a["id"]: a for a in existing if a.get("id") and a.get("published_date", "") >= str(cutoff)}
    errors = {}
    report = {}
    successes = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_journal, j, keywords, str(start), str(today)): j["name"] for j in journals}
        for f in as_completed(futures):
            name = futures[f]
            try:
                _, articles, info = f.result()
                successes += 1
                report[name] = info
                for a in articles:
                    if a["published_date"] >= str(cutoff):
                        by_id[a["id"]] = a
            except Exception as exc:
                errors[name] = str(exc)
                print(f"ERROR [{name}]: {exc}", flush=True)
    if not successes:
        raise RuntimeError("All Europe PMC searches failed. Preserving previous file.")
    result = sorted(by_id.values(), key=lambda a: (a["published_date"], a["title"]), reverse=True)
    (output / "articles.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "run_report.json").write_text(json.dumps({
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "journals_requested": len(journals),
        "journals_succeeded": successes,
        "journals_failed": len(errors),
        "total_articles_stored": len(result),
        "search_date_range": [str(start), str(today)],
        "per_journal": report,
        "errors": errors,
        "important_note": "Europe PMC coverage is incomplete; zero results do not prove no new articles.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Finished: {successes}/{len(journals)} journal searches; {len(result)} articles; {len(errors)} errors.")
    if errors:
        print("Review data/run_report.json for incomplete journal coverage and errors.")


if __name__ == "__main__":
    main()
