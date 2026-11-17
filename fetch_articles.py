#!/usr/bin/env python3
"""VU's LAB daily orthopaedic article collector (Europe PMC; stdlib only).

Preserves the 21-day retrieval window, 90-day retention and conservative
orthopaedic relevance filter from the existing collector. Adds publication
metadata, exclusion of Editorial/Letter/Correspondence and related non-research
items, and Technique Note categorisation. Existing stored articles are
re-screened on every successful run so older excluded papers disappear.
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

# Metadata exclusion has precedence over every included article type. In
# Europe PMC, pubTypeList.pubType can contain several labels. "Journal Article"
# alone is not proof of original research; a record tagged both Journal Article
# and Letter must be rejected.
BLOCKED_TYPE = re.compile(
    r"(?:^|\W)(?:editorial|letters?(?: to the editor)?|correspondence|"
    r"commentary|comment|reply|response to|author response|"
    r"corrigendum|erratum|correction|retraction|expression of concern|"
    r"news item|obituary)(?:$|\W)", re.I,
)
BLOCKED_TITLE = re.compile(
    r"^\s*(?:editorial\b|letters?\s+to\s+the\s+editor\b|letter\s*[:—–-]|to\s+the\s+editor\b|dear\s+editor\b|"
    r"correspondence(?:\s*[:—–-]|\s+(?:on|about|regarding)\b)|commentary\s*[:—–-]|comment\s+on\b|"
    r"reply\s+to\b|response\s+to\b|author(?:'s)?\s+reply\b|"
    r"corrigendum\b|erratum\b|correction\s+(?:to|of)\b|"
    r"retraction\b|expression\s+of\s+concern\b)", re.I,
)
TECHNIQUE_TYPE = re.compile(
    r"(?:technical\s+note|technique\s+note|surgical\s+technique|"
    r"operative\s+technique|technical\s+report|technique\s+article|"
    r"procedural\s+guide|step[- ]by[- ]step|tips\s+and\s+tricks|"
    r"how\s+(?:we|i)\s+do\s+it|how[- ]to\b|video\s+technique)", re.I,
)
TECHNIQUE_TITLE = re.compile(
    r"(?:\btechnical\s+note\b|\btechnique\s+note\b|"
    r"\bsurgical\s+technique\b|\boperative\s+technique\b|"
    r"\btechnical\s+report\b|\bstep[- ]by[- ]step\b|"
    r"\btips\s+and\s+tricks\b|\bhow\s+we\s+do\s+it\b|"
    r"\bprocedure\s+guide\b|\boperative\s+video\b|"
    r"\bvideo\s+technique\b)", re.I,
)
REVIEW_TYPE = re.compile(r"systematic review|meta.analysis|scoping review|umbrella review|narrative review|literature review|\breview\b", re.I)
CASE_TYPE = re.compile(r"case report|case series|case study", re.I)
ORIGINAL_TYPE = re.compile(r"randomized|randomised|cohort study|cross.sectional|case.control|clinical trial|observational study|prospective study|retrospective study|original research|research article|journal article", re.I)


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
    phrase = normalize(phrase)
    if not phrase:
        return False
    pattern = r"(?<!\w)" + r"[\W_]+".join(map(re.escape, phrase.split())) + r"(?!\w)"
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
    return sum(bool(p.search(text)) for p in patterns)


def is_relevant(title: str, abstract: str) -> bool:
    title, abstract = clean_html(title), clean_html(abstract)
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


def as_type_strings(value):
    """Flatten various Europe PMC pubTypeList representations safely."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [x for item in value for x in as_type_strings(item)]
    if isinstance(value, dict):
        for key in ("pubType", "publicationType", "pub_type", "type", "$", "value"):
            if key in value:
                return as_type_strings(value[key])
    return []


def get_publication_types(raw: dict) -> list[str]:
    names = []
    for field in ("pubTypeList", "publicationTypes", "publication_types", "publication_type", "pub_type", "article_type", "paper_type"):
        names.extend(as_type_strings(raw.get(field)))
    # Preserve case for display, but deduplicate case insensitively.
    return list(dict((x.casefold(), x) for x in names).values())


def is_excluded(title: str, publication_types) -> bool:
    return bool(BLOCKED_TITLE.search(clean_html(title)) or
                BLOCKED_TYPE.search(" | ".join(as_type_strings(publication_types))))


def classify_type(title: str, publication_types) -> str:
    """Always let specific technique/review/case metadata beat Journal Article."""
    types = " ".join(as_type_strings(publication_types))
    if is_excluded(title, publication_types):
        return "exclude"
    if TECHNIQUE_TYPE.search(types) or TECHNIQUE_TITLE.search(title):
        return "technique"
    combined = types + " " + title
    if REVIEW_TYPE.search(combined):
        return "review"
    if CASE_TYPE.search(combined):
        return "case"
    if ORIGINAL_TYPE.search(combined):
        return "original"
    return "other"


def api_get(query: str, cursor: str) -> dict:
    params = urlencode({"query": query, "format": "json", "resultType": "core",
                        "pageSize": PAGE_SIZE, "cursorMark": cursor})
    req = Request(API + "?" + params, headers={
        "User-Agent": "OrthopaedicResearchMonitor/2.1 (personal noncommercial literature monitoring)"})
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


def article_from_record(raw: dict, chosen: dict, keywords: list[str]) -> dict | None:
    title = clean_html(raw.get("title", ""))
    actual_journal = raw.get("journalTitle", "") or (raw.get("journalInfo") or {}).get("journal", {}).get("title", "")
    if not title or (actual_journal and not title_matches_journal(actual_journal, chosen["name"])):
        return None
    types = get_publication_types(raw)
    if is_excluded(title, types):
        return None
    abstract = clean_html(raw.get("abstractText", ""))
    if not specialist_journal(chosen) and not is_relevant(title, abstract):
        return None
    doi, pmid = (raw.get("doi") or "").strip(), (raw.get("pmid") or "").strip()
    source, identifier = (raw.get("source") or "").strip(), str(raw.get("id", "")).strip()
    if not (doi or pmid or (source and identifier)):
        return None
    key = "doi:" + doi.lower() if doi else "pmid:" + pmid if pmid else source + ":" + identifier
    published = parse_date(raw.get("firstPublicationDate") or raw.get("firstIndexDate") or "")
    raw_text = title + " " + abstract
    matches = [k for k in keywords if k.casefold() not in AMBIGUOUS_KEYWORDS and contains_phrase(raw_text, k)]
    return {
        "id": key, "title": title, "journal": chosen["name"],
        "journal_list": chosen["list"], "authors": raw.get("authorString", "") or "",
        "published_date": published, "doi": doi, "pmid": pmid,
        "url": ("https://doi.org/" + doi if doi else
                "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/" if pmid else
                "https://europepmc.org/article/" + source + "/" + identifier),
        "abstract": abstract,
        "open_access": str(raw.get("isOpenAccess") or "").lower() == "y",
        "publication_types": types,
        "paper_type": classify_type(title, types),
        "categories": (["Digital Orthopaedics & Surgical Technology"] if matches else ["Other / Unclassified"]),
        "matched_keywords": matches,
    }


def fetch_journal(journal: dict, keywords: list[str], from_date: str, through_date: str):
    name = journal["name"]
    query = (f'JOURNAL:"{name.replace(chr(34), "")}" AND '
             f'FIRST_PDATE:[{from_date} TO {through_date}] AND (SRC:MED OR SRC:PMC)')
    articles, cursor, total, pages = [], "*", 0, 0
    while pages < MAX_PAGES_PER_JOURNAL:
        data = api_get(query, cursor)
        page = (data.get("resultList") or {}).get("result", [])
        total = int(data.get("hitCount", 0))
        pages += 1
        for raw in page:
            a = article_from_record(raw, journal, keywords)
            if a:
                articles.append(a)
        next_cursor = data.get("nextCursorMark", "")
        if not page or not next_cursor or next_cursor == cursor or pages * PAGE_SIZE >= total:
            break
        cursor = next_cursor
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

    # Same 90-day retention and re-screening of old articles as prior version.
    by_id, removed_old, removed_type = {}, 0, 0
    for a in existing:
        if not a.get("id") or a.get("published_date", "") < str(cutoff):
            continue
        chosen = lookup.get(normalize(a.get("journal", "")))
        if not chosen or (not specialist_journal(chosen) and not is_relevant(a.get("title", ""), a.get("abstract", ""))):
            removed_old += 1
            continue
        types = a.get("publication_types") or get_publication_types(a)
        if is_excluded(a.get("title", ""), types):
            removed_type += 1
            continue
        a["publication_types"] = types
        a["paper_type"] = classify_type(a.get("title", ""), types)
        text = (a.get("title", "") or "") + " " + (a.get("abstract", "") or "")
        matches = [k for k in keywords if k.casefold() not in AMBIGUOUS_KEYWORDS and contains_phrase(text, k)]
        a["matched_keywords"] = matches
        a["categories"] = ["Digital Orthopaedics & Surgical Technology"] if matches else ["Other / Unclassified"]
        by_id[a["id"]] = a

    errors, report, successes = {}, {}, 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_journal, j, keywords, str(start), str(today)): j["name"] for j in journals}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                _, articles, info = fut.result()
                successes += 1
                report[name] = info
                for a in articles:
                    if a["published_date"] >= str(cutoff):
                        by_id[a["id"]] = a
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
        "previous_articles_removed_by_type": removed_type,
        "type_counts": {t: sum(a.get("paper_type") == t for a in result) for t in ("technique", "review", "original", "case", "other")},
        "search_date_range": [str(start), str(today)],
        "per_journal": report, "errors": errors,
        "important_note": "Rule-based screening can miss relevant publications and retain false positives. Europe PMC does not cover all journals or online-first articles.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Finished: {successes}/{len(journals)} journals; {len(result)} articles; {removed_old} off-topic and {removed_type} excluded old articles; {len(errors)} errors")


if __name__ == "__main__":
    main()
