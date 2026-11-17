#!/usr/bin/env python3
"""Daily Orthopaedic Research Monitor. Free Europe PMC API; standard library only.

Journals in A that are clearly orthopaedic accept every article. All other
journals (including broad journals accidentally assigned to A) must pass the
orthopaedic relevance screen. A saved article is screened again on each run.
This rules-based screen is deliberately conservative and is NOT a substitute
for manual review or complete bibliographic coverage.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
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
LOOKBACK_DAYS = 21
RETENTION_DAYS = 90
PAGE_SIZE = 100
MAX_PAGES_PER_JOURNAL = 12

# Only these unmistakably specialist journal-title features qualify for
# unconditional inclusion, and ONLY when the journal is on list A.
CORE_JOURNAL_TITLE = re.compile(
    r"orthop(?:a)?edic|arthroplast|arthroscop|\bthe knee\b|"
    r"\bknee surgery\b|\bhip international\b|\bfoot\s*(?:&|and)\s*ankle\b|"
    r"\bfoot and ankle\b|\bshoulder\s*(?:&|and)\s*elbow\b|"
    r"\bhand surgery\b|\bhand therapy\b|\bbone\s*(?:&|and)\s*joint\b|"
    r"\bjoint bone spine\b|\bmusculoskeletal\b|\bosteoporos|"
    r"\bbone and mineral\b|\btrauma and limb reconstruction\b|"
    r"\bhip preservation\b|\bhss journal\b",
    re.IGNORECASE,
)
CORE_EXACT = {"bone", "acta orthopaedica"}

# High-specificity orthopaedic conditions and interventions. Matching a word
# fragment such as 'hip' in 'scholarship' is explicitly NOT allowed.
STRONG_ORTHO = [
    r"\borthop(?:a)?edic\b", r"\bmusculoskeletal\b",
    r"\barthroplast(?:y|ies)\b", r"\barthroscop(?:y|ic)\b",
    r"\bosteoarthrit(?:is|ic)\b", r"\bosteonecros(?:is|es)\b",
    r"\bosteotom(?:y|ies)\b", r"\bosteosynthesis\b",
    r"\bmenisc(?:us|al|ectomy|i)\b", r"\brotator cuff\b",
    r"\b(?:anterior|posterior) cruciate ligament\b", r"\bACL (?:tear|injury|rupture|reconstruction|repair)\b",
    r"\bPCL (?:tear|injury|rupture|reconstruction|repair)\b",
    r"\b(?:total|partial|revision) (?:hip|knee|shoulder|ankle) (?:replacement|arthroplasty)\b",
    r"\bhip fracture\b", r"\bfemoral neck fracture\b", r"\btibial plateau fracture\b",
    r"\bvertebral compression fracture\b", r"\bspinal fusion\b",
    r"\b(?:acetabular|pelvic ring|intertrochanteric|subtrochanteric) fracture\b",
    r"\b(?:fracture|bone) fixation\b", r"\bfracture (?:healing|nonunion|non-union|management|reduction)\b",
    r"\b(?:bone|joint) infection\b", r"\bosteomyelitis\b",
    r"\b(?:bone|soft tissue) sarcoma\b", r"\bosteosarcoma\b",
    r"\b(?:knee|hip|shoulder|elbow|ankle|foot|hand|wrist) surgery\b",
    r"\b(?:knee|hip|shoulder|elbow|ankle|foot|hand|wrist) (?:pain|osteoarthritis|instability|injury|dislocation|replacement)\b",
    r"\b(?:scoliosis|kyphosis|spondylolisthesis|spondylosis)\b",
    r"\b(?:total knee arthroplasty|total hip arthroplasty|unicompartmental knee arthroplasty)\b",
    r"\b(?:prosthetic joint infection|periprosthetic fracture|revision arthroplasty)\b",
    r"\b(?:Achilles tendon|patellar tendon) (?:rupture|repair|injury)\b",
    r"\b(?:bone|joint) reconstruction\b", r"\borthotic(?:s)?\b",
    r"\b(?:gait|limb) (?:analysis|alignment|reconstruction)\b",
]

# Anatomy alone is NOT enough; must co-occur with an orthopaedic clinical,
# rehabilitation, imaging, or biomechanical context (see is_relevant).
ANATOMY = [
    r"\bfem(?:ur|oral)\b", r"\btibi(?:a|al)\b", r"\bfibul(?:a|ar)\b",
    r"\bhumer(?:us|al)\b", r"\bacetabul(?:um|ar)\b", r"\bscapul(?:a|ar)\b",
    r"\bcalcane(?:us|al)\b", r"\bpatell(?:a|ar)\b", r"\bmenisc(?:us|al|i)\b",
    r"\b(?:tarsal|metatarsal|metacarpal|carpal|phalangeal)\b",
    r"\b(?:hip|knee|shoulder|elbow|ankle|wrist|foot|hand)\b",
    r"\b(?:spine|spinal|vertebr(?:a|ae|al)|lumbar|cervical spine|sacral)\b",
    r"\b(?:pelvis|pelvic|joint|bone|cartilage|tendon|ligament)\b",
]
CONTEXT = [
    r"\b(?:fracture|dislocation|subluxation|nonunion|non-union|malunion)\b",
    r"\b(?:fixation|osteotomy|osteosynthesis|arthroscopy|arthroplasty|replacement)\b",
    r"\b(?:implant|prosthes(?:is|es|tic)|osteoporosis|osteopenia|degenerative)\b",
    r"\b(?:tendon|ligament|cartilage) (?:repair|rupture|tear|injury)\b",
    r"\b(?:sports injury|sports medicine|bone healing|gait analysis)\b",
    r"\b(?:biomechanic(?:s|al)|orthosis|orthotic|rehabilitation|range of motion)\b",
    r"\b(?:magnetic resonance imaging|computed tomography|radiograph(?:y|ic)?|MRI|CT)\b",
    r"\b(?:surgical planning|preoperative planning|surgical navigation|patient-specific implant)\b",
    r"\b(?:3D printing|three-dimensional printing|additive manufacturing|robot-assisted surgery)\b",
    r"\b(?:surgery|surgical|operative|reconstruction|reconstructive|treatment|outcomes)\b",
]

# Non-musculoskeletal specialties commonly generate false hits. Only reject
# if no clearly orthopaedic phrase appears in the title.
OFF_TOPIC_TITLE = [
    r"\b(?:penile|urethral|prostat(?:e|ic)|testicular|vaginal|obstetric|uterine|ovarian)\b",
    r"\b(?:dental|tooth|teeth|mandibular|periodontal|maxillofacial)\b",
    r"\b(?:coronary|cardiac|myocardial|pulmonary|hepatic|renal)\b",
    r"\b(?:ophthalmic|retinal|ocular|cataract)\b",
    r"\b(?:breast cancer|gastric cancer|colorectal cancer|pancreatic cancer)\b",
    r"\b(?:skin flap|pressure ulcer|pressure injur(?:y|ies)|pressure sore|spinal cord injury)\b",
]

COMPILED_STRONG = [re.compile(p, re.I) for p in STRONG_ORTHO]
COMPILED_ANATOMY = [re.compile(p, re.I) for p in ANATOMY]
COMPILED_CONTEXT = [re.compile(p, re.I) for p in CONTEXT]
COMPILED_OFFTOPIC = [re.compile(p, re.I) for p in OFF_TOPIC_TITLE]
AMBIGUOUS_KEYWORDS = {"ar", "mr", "vr", "xr", "psi", "slm", "sls", "ebm", "fdm", "sla", "cad", "cam", "ai", "vsp", "tka", "tha", "uka"}


def read_json(path: Path, fallback):
    if not path.exists():
        return fallback
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", value or ""))).strip()


def contains_phrase(text: str, phrase: str) -> bool:
    # Keep exact word boundaries; unlike substring search, "hip" is not in
    # "scholarship", and "bone" is not in "backbone".
    phrase = normalize(phrase)
    if not phrase:
        return False
    tokens = phrase.split()
    pattern = r"(?<!\w)" + r"[\W_]+".join(map(re.escape, tokens)) + r"(?!\w)"
    return bool(re.search(pattern, text, re.I))


def load_keywords() -> list[str]:
    path = ROOT / "keywords.txt"
    if not path.exists():
        raise FileNotFoundError("keywords.txt was not found")
    return list(dict.fromkeys(s.strip() for s in path.read_text(encoding="utf-8").splitlines()
                              if s.strip() and not s.lstrip().startswith("#")))


def specialist_journal(journal: dict) -> bool:
    if journal.get("list") != "A":
        return False
    name = normalize(journal.get("name", ""))
    return name in CORE_EXACT or bool(CORE_JOURNAL_TITLE.search(name))


def count_matches(patterns, text: str) -> int:
    return sum(bool(pattern.search(text)) for pattern in patterns)


def is_relevant(title: str, abstract: str) -> bool:
    """Require specialist concepts OR anatomy plus a meaningful clinical link.

    A purely generic keyword or a body part alone cannot admit a paper from
    multidisciplinary journals. The explicit title test reduces topic drift.
    """
    title = clean_html(title)
    abstract = clean_html(abstract)
    full = title + ". " + abstract
    strong_title = count_matches(COMPILED_STRONG, title)
    if count_matches(COMPILED_OFFTOPIC, title) and not strong_title:
        return False
    if strong_title:
        return True
    strong_full = count_matches(COMPILED_STRONG, full)
    anatomy_title = count_matches(COMPILED_ANATOMY, title)
    anatomy_full = count_matches(COMPILED_ANATOMY, full)
    context_title = count_matches(COMPILED_CONTEXT, title)
    context_full = count_matches(COMPILED_CONTEXT, full)
    if strong_full and (anatomy_title or context_title or anatomy_full >= 2):
        return True
    if anatomy_title and context_full and (context_title or anatomy_full >= 2):
        return True
    if anatomy_full >= 2 and context_full >= 2 and (anatomy_title or context_title):
        return True
    return False


def api_get(query: str, cursor: str) -> dict:
    params = urlencode({"query": query, "format": "json", "resultType": "core",
                        "pageSize": PAGE_SIZE, "cursorMark": cursor})
    req = Request(API + "?" + params, headers={
        "User-Agent": "OrthopaedicResearchMonitor/2.0 (personal noncommercial literature monitoring)"})
    err = None
    for attempt in range(4):
        try:
            with urlopen(req, timeout=40) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            err = exc
            if attempt < 3:
                time.sleep(2 ** attempt + .25)
    raise RuntimeError(f"Europe PMC request failed: {err}")


def parse_date(raw: str) -> str:
    raw = (raw or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return raw + "-01"
    if re.fullmatch(r"\d{4}", raw):
        return raw + "-01-01"
    return ""


def title_matches_journal(actual: str, chosen: str) -> bool:
    a, b = normalize(actual), normalize(chosen)
    return a == b or a.removeprefix("the ") == b.removeprefix("the ")


def extract_publication_types(raw: dict) -> list[str]:
    # Europe PMC core API typically provides pubTypeList.pubType.
    value = (raw.get("pubTypeList") or {}).get("pubType", [])
    if isinstance(value, str):
        value = [value]
    extra = raw.get("pubType", [])
    if isinstance(extra, str):
        extra = [extra]
    return list(dict.fromkeys(str(v).strip() for v in [*value, *extra] if v))


def classify_article(types: list[str], title: str, abstract: str) -> tuple[str, str]:
    """Return paper class, confidence. Never silently call an editorial original research."""
    combined = " ".join(types).casefold()
    heading = title.casefold()
    if "case reports" in combined or "case report" in combined or re.search(r"\bcase report\b", heading):
        return "Case report", "metadata" if "case report" in combined else "title"
    if "meta-analysis" in combined or re.search(r"\bmeta-analysis\b", heading):
        return "Systematic review / Meta-analysis", "metadata" if "meta-analysis" in combined else "title"
    if "systematic review" in combined or re.search(r"\bsystematic review\b", heading):
        return "Systematic review", "metadata" if "systematic review" in combined else "title"
    if any(w in combined for w in ["review", "literature review"]) or re.search(r"\b(scoping|narrative) review\b", heading):
        return "Review", "metadata" if "review" in combined else "title"
    if any(w in combined for w in ["editorial", "letter", "comment", "news", "guideline", "protocol", "retraction", "correction", "published erratum"]):
        return "Other", "metadata"
    if re.search(r"\b(editorial|corrigendum|erratum|letter to the editor|study protocol)\b", heading):
        return "Other", "title"
    if any(w in combined for w in ["clinical trial", "randomized controlled trial", "observational study", "comparative study", "multicenter study"]):
        return "Original research", "metadata"
    if "journal article" in combined and abstract:
        return "Original research", "inferred"
    return "Unclassified", "unknown"


def article_from_record(raw: dict, chosen: dict, keywords: list[str]) -> dict | None:
    title = clean_html(raw.get("title", ""))
    actual_journal = raw.get("journalTitle", "") or (raw.get("journalInfo") or {}).get("journal", {}).get("title", "")
    if not title or (actual_journal and not title_matches_journal(actual_journal, chosen["name"])):
        return None
    abstract = clean_html(raw.get("abstractText", ""))
    if not specialist_journal(chosen) and not is_relevant(title, abstract):
        return None
    doi = (raw.get("doi") or "").strip()
    pmid = (raw.get("pmid") or "").strip()
    source = (raw.get("source") or "").strip()
    identifier = str(raw.get("id", "")).strip()
    if not (doi or pmid or (source and identifier)):
        return None
    key = "doi:" + doi.lower() if doi else "pmid:" + pmid if pmid else source + ":" + identifier
    published = parse_date(raw.get("firstPublicationDate") or raw.get("firstIndexDate") or "")
    raw_text = title + " " + abstract
    matches = [k for k in keywords if k.casefold() not in AMBIGUOUS_KEYWORDS and contains_phrase(raw_text, k)]
    pub_types = extract_publication_types(raw)
    paper_type, paper_type_confidence = classify_article(pub_types, title, abstract)
    return {
        "publication_types": pub_types, "paper_type": paper_type, "paper_type_confidence": paper_type_confidence,
        "id": key, "title": title, "journal": chosen["name"],
        "journal_list": chosen["list"], "authors": raw.get("authorString", "") or "",
        "published_date": published, "doi": doi, "pmid": pmid,
        "url": ("https://doi.org/" + doi if doi else
                "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/" if pmid else
                "https://europepmc.org/article/" + source + "/" + identifier),
        "abstract": abstract, "open_access": str(raw.get("isOpenAccess") or "").lower() == "y",
        "categories": (["Digital Orthopaedics & Surgical Technology"] if matches else ["Other / Unclassified"]),
        "matched_keywords": matches,
    }


def fetch_journal(journal: dict, keywords: list[str], from_date: str, through_date: str):
    name = journal["name"]
    safe_name = name.replace('"', "")
    query = f'JOURNAL:"{safe_name}" AND FIRST_PDATE:[{from_date} TO {through_date}] AND (SRC:MED OR SRC:PMC)'
    articles = []
    cursor = "*"
    total = pages = 0
    while pages < MAX_PAGES_PER_JOURNAL:
        data = api_get(query, cursor)
        page = (data.get("resultList") or {}).get("result", [])
        total = int(data.get("hitCount", 0))
        pages += 1
        for raw in page:
            a = article_from_record(raw, journal, keywords)
            if a:
                articles.append(a)
        new_cursor = data.get("nextCursorMark", "")
        if not page or not new_cursor or new_cursor == cursor or pages * PAGE_SIZE >= total:
            break
        cursor = new_cursor
    return name, articles, {
        "returned": total, "accepted": len(articles), "pages_checked": pages,
        "possibly_truncated": total > pages * PAGE_SIZE,
        "automatic_full_journal": specialist_journal(journal),
    }


def main():
    today = datetime.now(timezone.utc).date()
    start, cutoff = today - timedelta(days=LOOKBACK_DAYS), today - timedelta(days=RETENTION_DAYS)
    journals = read_json(ROOT / "journals.json", {}).get("journals", [])
    if not journals:
        raise ValueError("journals.json has no journals")
    keywords = load_keywords()
    lookup = {normalize(j["name"]): j for j in journals}
    out = ROOT / "data"
    out.mkdir(parents=True, exist_ok=True)
    existing = read_json(out / "articles.json", [])
    if not isinstance(existing, list):
        raise ValueError("data/articles.json must contain a JSON array")

    # CRITICAL: rescreen OLD content, not only newly indexed papers. Otherwise
    # yesterday's false positives remain visible for up to 90 days.
    by_id = {}
    removed_old = 0
    for a in existing:
        if not a.get("id") or a.get("published_date", "") < str(cutoff):
            continue
        chosen = lookup.get(normalize(a.get("journal", "")))
        if not chosen or (not specialist_journal(chosen) and not is_relevant(a.get("title", ""), a.get("abstract", ""))):
            removed_old += 1
            continue
        text = (a.get("title", "") or "") + " " + (a.get("abstract", "") or "")
        match = [k for k in keywords if k.casefold() not in AMBIGUOUS_KEYWORDS and contains_phrase(text, k)]
        a["matched_keywords"] = match
        if not a.get("paper_type"):
            a["paper_type"], a["paper_type_confidence"] = classify_article(a.get("publication_types", []), a.get("title", ""), a.get("abstract", ""))
        a["categories"] = ["Digital Orthopaedics & Surgical Technology"] if match else ["Other / Unclassified"]
        by_id[a["id"]] = a

    errors = {}
    report = {}
    successes = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_journal, j, keywords, str(start), str(today)): j["name"] for j in journals}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                _, articles, info = fut.result()
                successes += 1
                report[name] = info
                for article in articles:
                    if article["published_date"] >= str(cutoff):
                        by_id[article["id"]] = article
            except Exception as exc:
                errors[name] = str(exc)
                print(f"ERROR [{name}]: {exc}", flush=True)
    if not successes:
        raise RuntimeError("All Europe PMC searches failed; preserving existing articles.json")
    result = sorted(by_id.values(), key=lambda a: (a.get("published_date", ""), a.get("title", "")), reverse=True)
    (out / "articles.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "run_report.json").write_text(json.dumps({
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "journals_requested": len(journals), "journals_succeeded": successes,
        "journals_failed": len(errors), "total_articles_stored": len(result),
        "previous_articles_removed_by_filter": removed_old,
        "search_date_range": [str(start), str(today)],
        "per_journal": report, "errors": errors,
        "important_note": "Rule-based screening can miss relevant publications and retain false positives. Europe PMC does not cover all journals or online-first articles.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Finished: {successes}/{len(journals)} journals; {len(result)} articles; {removed_old} older off-topic articles removed; {len(errors)} errors")

if __name__ == "__main__":
    main()
