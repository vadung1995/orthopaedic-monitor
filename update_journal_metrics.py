#!/usr/bin/env python3
"""VU's LAB – conservative, source-auditable journal metrics refresh.

Automatically checks public journal landing pages for a clearly labelled JIF,
and downloads the official SCImago journal-ranking CSV when available to obtain
SJR best quartiles (not Scopus CiteScore quartiles).

Never estimates a JIF, never substitutes CiteScore for JIF, and never promotes
user-provided provisional watchlist figures to 'verified' without evidence.

The original journal_info.json remains untouched. Only the additive file
`data/journal_metrics.json` is written by this job. The dashboard prefers
verified values from that file when their metric years are not older.

Limitations: some publisher sites do not publish a machine-readable JIF; the
SCImago CSV download may block automation. Previously verified metrics are
preserved in either case. For full JCR coverage, an authorised Clarivate/JCR
export or licensed Journals API integration is necessary.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import io
import json
import os
import re
import time

ROOT=Path(__file__).resolve().parent
OUTPUT=ROOT/'data'/'journal_metrics.json'
MAX_PUBLISHER_CHECKS=int(os.getenv('VU_METRICS_MAX_PAGES','110'))
SJR_EXPORT='https://www.scimagojr.com/journalrank.php?out=xls&year={year}&type=j'
USER_AGENT='VusLabResearchMonitor/3.0 (personal noncommercial bibliometric audit)'


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else fallback
    except (json.JSONDecodeError, UnicodeDecodeError):
        return fallback


def key(name):
    return re.sub(r'[^a-z0-9]+',' ',str(name or '').casefold().replace('&',' and ')).strip()


def get(url, limit=5_000_000):
    req=Request(url,headers={'User-Agent':USER_AGENT,'Accept':'text/html,text/csv,application/octet-stream;q=0.9,*/*;q=0.5'})
    with urlopen(req,timeout=12) as resp:
        return resp.read(limit+1)[:limit],resp.geturl(),resp.headers.get('Content-Type','')


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title=[]; self.heading=[]; self.parts=[]; self.in_title=False; self.in_h1=False; self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag=='title':self.in_title=True
        if tag=='h1':self.in_h1=True
        if tag in ('script','style','noscript'): self.skip+=1
    def handle_endtag(self,tag):
        if tag=='title':self.in_title=False
        if tag=='h1':self.in_h1=False
        if tag in ('script','style','noscript') and self.skip: self.skip-=1
    def handle_data(self,data):
        if self.in_title:self.title.append(data)
        if self.in_h1:self.heading.append(data)
        if not self.skip:self.parts.append(data)

# Parse only clearly labelled, year-specific *2-year* JIF. This deliberately
# ignores a 5-year JIF, CiteScore, SJR, and unlabelled values.
JIF_FORWARD = [
    re.compile(r"(?<!5-year )(?<!5 year )\bJournal Impact Factor\s*[:：-]?\s*(\d{1,3}(?:\.\d{1,2})?)\s*\(\s*(20\d{2})\s*\)", re.I),
    re.compile(r"(?<!5-year )(?<!5 year )\bJournal Impact Factor\s*[:：-]?\s*(\d{1,3}(?:\.\d{1,2})?)\s*[,;]\s*(20\d{2})", re.I),
    re.compile(r"\b(20\d{2})\s+(?:Journal )?Impact Factor\s*[:：-]?\s*(\d{1,3}(?:\.\d{1,2})?)\b", re.I),
    re.compile(r"\b(?:Journal )?Impact Factor\s*[:：-]?\s*(\d{1,3}(?:\.\d{1,2})?)\s+(?:\bfor\s+)?(20\d{2})\b", re.I),
]
JCR_YEAR = re.compile(r"(?:Journal Citation Reports|JCR)\s*(?:\bdata\s*\b)?(?:for\s*)?(20\d{2})", re.I)
JIF_REVERSED = re.compile(r"(?<![.\d])\b(\d{1,3}(?:\.\d{1,2})?)\s+Impact Factor\b", re.I)


def journal_identity(page, expected_title):
    expected=key(expected_title)
    # Compare full journal name against the document's own title or main heading.
    # Avoid checking random unrelated publisher pages or linked search results.
    raw_title=' '.join(page.title)
    raw_heading=' '.join(page.heading)
    if not expected or len(expected)<4:return False
    # Document title is usually 'Home | Journal name | Publisher', while
    # heading is often the exact journal name. Match title *segments* for
    # short names (Nature, Injury, Bone) to avoid journal-name collisions.
    candidates=[key(raw_heading)]+[key(part) for part in re.split(r'[|:–—]',raw_title)]
    def same(a,b):
        return a==b or a.removeprefix('the ')==b.removeprefix('the ')
    if any(same(c,expected) for c in candidates if c):return True
    # Some pages append 'Home Page' / 'Journal Homepage' but still name
    # the full journal; allow a strict suffix only for longer titles.
    if len(expected)>=14:
        return any(same(c.removeprefix('home page ').removeprefix('journal homepage '), expected) for c in candidates if c)
    return False


def extract_jif_diagnostic(source_html, expected_title, latest_year):
    p=PageText()
    try:p.feed(source_html)
    except Exception:return None,'invalid_html'
    if not journal_identity(p,expected_title):return None,'journal_title_not_confirmed'
    text=re.sub(r'\s+',' ',unescape(' '.join(p.parts)))
    found=defaultdict(set)
    for i,rx in enumerate(JIF_FORWARD):
        for m in rx.finditer(text):
            # Pattern 3 is year-first; other expressions are value-first.
            num,year=(m.group(2),m.group(1)) if i==2 else (m.group(1),m.group(2))
            try:value=float(num);yr=int(year)
            except (ValueError,TypeError):continue
            if 0<value<300 and 2015<=yr<=latest_year:found[yr].add(value)
    # Elsevier journal homepage: '4.4 Impact Factor ... Journal Citation Reports 2025'
    # Require an explicit local JCR year near the value, not a year somewhere
    # unrelated in the page body.
    for m in JIF_REVERSED.finditer(text):
        segment=text[m.end():m.end()+500]
        yearmatch=JCR_YEAR.search(segment)
        if not yearmatch:continue
        try:value=float(m.group(1));yr=int(yearmatch.group(1))
        except (ValueError,TypeError):continue
        if 0<value<300 and 2015<=yr<=latest_year:found[yr].add(value)
    if not found:return None,'no_year_labeled_2yr_jif_found'
    yr=max(found)
    if len(found[yr])!=1:return None,'conflicting_values_for_same_year'
    return (next(iter(found[yr])),yr),'verified_on_publisher_page'


def extract_jif(source_html, expected_title, latest_year):
    result,_=extract_jif_diagnostic(source_html,expected_title,latest_year)
    return result


def combined_registry():
    original=read_json(ROOT/'journals.json',{}).get('journals',[])
    additions=read_json(ROOT/'journals_additions.json',{}).get('journals',[])
    info1=read_json(ROOT/'journal_info.json',{}).get('journals',{})
    info2=read_json(ROOT/'journal_info_additions.json',{}).get('journals',{})
    registry={}
    for entry in original+additions:
        if entry.get('name'):
            registry.setdefault(key(entry['name']),{'name':entry['name'],'url':entry.get('url',''),'details':{}})
    for src in (info1,info2):
        for name,details in src.items():
            k=key(name)
            if k not in registry:continue
            registry[k]['details'].update(details or {})
    return registry


def supported_url(url):
    try:
        u=urlparse(url)
        return u.scheme=='https' and bool(u.netloc) and not u.username and not u.password
    except Exception:return False


def latest_years(now):
    # JCR 2026 released 2025 JIF in June 2026; SJR generally follows in spring.
    return now.year-(1 if now.month>=6 else 2),now.year-(1 if now.month>=5 else 2)


def candidate_urls(j):
    d=j['details']
    urls=[d.get('journal_url'),j.get('url')]
    # Several Nature Portfolio titles publish metrics under /journal-impact
    # or /journal-information. Trying both is more reliable than homepage only.
    for url in list(urls):
        if not supported_url(url):continue
        parsed=urlparse(url)
        if parsed.netloc.endswith('nature.com'):
            stem=parsed.path.rstrip('/').split('/')[1:2]
            if stem and stem[0] not in {'articles','subjects','search'}:
                urls.extend([f'https://www.nature.com/{stem[0]}/journal-impact',f'https://www.nature.com/{stem[0]}/journal-information'])
    return list(dict.fromkeys(u for u in urls if supported_url(u)))


def pick_publisher_urls(registry,jif_year):
    entries=[]
    for journal in registry.values():
        urls=candidate_urls(journal)
        if not urls:continue
        entries.append((journal['name'],urls))
    entries.sort(key=lambda a:key(a[0]))
    if MAX_PUBLISHER_CHECKS>=len(entries):return entries
    # Rotate by *day*, not by week: daily scheduled jobs cover the complete
    # journal roster over successive runs, including >250 titles.
    day=datetime.now(timezone.utc).date().toordinal()
    start=(day*MAX_PUBLISHER_CHECKS)%len(entries)
    return [entries[(start+i)%len(entries)] for i in range(MAX_PUBLISHER_CHECKS)]


def best_add(data,name,metric,value,year,source):
    d=data.setdefault(name,{})
    oldyr=int(d.get(metric+'_year') or 0)
    if year<oldyr:return False
    if year==oldyr and d.get(metric+'_status')=='verified' and d.get(metric)==value:return False
    d.update({metric:value,metric+'_year':year,metric+'_status':'verified',metric+'_source':source})
    return True


def update_jif(records,registry,jif_year):
    checked=0;updated=0
    selected=pick_publisher_urls(registry,jif_year)
    diagnostics={}
    def one(pair):
        name,urls=pair
        failures=[]
        for url in urls:
            try:
                payload,final,content_type=get(url,limit=1_500_000)
                if 'pdf' in content_type.lower():
                    failures.append('PDF: '+url);continue
                result,reason=extract_jif_diagnostic(payload.decode('utf-8','replace'),name,jif_year)
                if result:return name,(result,final),'verified'
                failures.append(reason+': '+url)
            except (OSError,ValueError,HTTPError,URLError,TimeoutError) as exc:
                failures.append(type(exc).__name__+': '+url)
        return name,None,' | '.join(failures)[:900] or 'No accepted journal URL'
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs=[pool.submit(one,entry) for entry in selected]
        for fut in as_completed(futs):
            name,result,reason=fut.result();checked+=1
            diagnostics[name]=reason
            if result:
                (value,year),url=result
                if best_add(records,name,'impact_factor',value,year,url):updated+=1
    return checked,updated,diagnostics


def normalize_header(s):
    return key(s).replace(' ','_')


def load_sjr_csv(contents):
    if len(contents)>10_000_000:
        raise ValueError('CSV too large to process safely')
    for encoding in ('utf-8-sig','latin-1'):
        try:text=contents.decode(encoding)
        except UnicodeDecodeError:continue
        if text.lstrip().startswith('<'):
            raise ValueError('HTML/captcha served instead of SCImago CSV')
        header=text.splitlines()[0] if text.splitlines() else ''
        delim=';' if header.count(';')>header.count(',') else ','
        r=csv.DictReader(io.StringIO(text,newline=''),delimiter=delim)
        cols={normalize_header(x):x for x in (r.fieldnames or [])}
        name_col=next((cols[c] for c in ('title','journal_title','source_title') if c in cols),None)
        q_col=next((cols[c] for c in ('sjr_best_quartile','best_quartile','quartile') if c in cols),None)
        if not name_col or not q_col:raise ValueError('Missing SCImago Title / SJR Best Quartile columns')
        data={}
        count=0
        for row in r:
            count+=1
            n=key(row.get(name_col))
            q=re.search(r'\bQ[1-4]\b',str(row.get(q_col,'')).upper())
            if n and q:data.setdefault(n,q.group(0))
        if count<1000:raise ValueError('Unexpectedly small SCImago export; not using it')
        return data,count
    raise ValueError('Unable to decode CSV')


def update_sjr(records,registry,sjr_year):
    # Official site provides CSV exports for personal research. If blocked,
    # use a user-downloaded export at metric_sources/scimagojr_YYYY.csv.
    src=ROOT/'metric_sources'/f'scimagojr_{sjr_year}.csv'
    content=None;source=''
    if src.exists():
        content=src.read_bytes();source=f'SCImago {sjr_year} downloaded CSV (user provided)'
    else:
        url=SJR_EXPORT.format(year=sjr_year)
        try:
            content,final,_=get(url,limit=10_000_001)
            source=final
        except (HTTPError,URLError,OSError,TimeoutError,ValueError) as ex:
            print('SCImago download unavailable:',type(ex).__name__)
    if content is None:return 0,0
    try:ranking,count=load_sjr_csv(content)
    except (ValueError,csv.Error) as e:
        print('SCImago CSV not usable:',e);return 0,0
    changes=0
    for k,j in registry.items():
        q=ranking.get(k)
        if q and best_add(records,j['name'],'sjr_quartile',q,sjr_year,source):changes+=1
    return count,changes


def update_publisher_evidence_import(records,registry,maxyear):
    """Import a short, human-audited public publisher evidence list.

    Add only numbers independently checked on the exact journal homepage.
    The source URL and year are preserved so the displayed value is auditable.
    """
    path=ROOT/'metric_sources'/'publisher_verified.csv'
    if not path.exists():return 0
    count=0
    with path.open(encoding='utf-8-sig',newline='') as f:
        for r in csv.DictReader(f):
            name=(r.get('journal') or '').strip();k=key(name)
            if k not in registry:continue
            try:value=float(r['jif']);year=int(r['year'])
            except (ValueError,TypeError,KeyError):continue
            source=(r.get('source_url') or '').strip()
            if not (0<value<300 and 2015<=year<=maxyear and supported_url(source)):continue
            if best_add(records,registry[k]['name'],'impact_factor',value,year,source):count+=1
    return count


def update_jcr_import(records,registry,maxyear):
    path=ROOT/'metric_sources'/'jcr_verified.csv'
    if not path.exists():return 0
    rows=0
    with path.open(encoding='utf-8-sig',newline='') as f:
        for r in csv.DictReader(f):
            name=(r.get('journal') or '').strip();k=key(name)
            if k not in registry:continue
            try:value=float(r['jif']);year=int(r['year'])
            except (ValueError,TypeError,KeyError):continue
            source=(r.get('source_url') or '').strip()
            if not (0<value<200 and 2015<=year<=maxyear and source.startswith('https://')):continue
            if best_add(records,registry[k]['name'],'impact_factor',value,year,source):rows+=1
    return rows


def main():
    now=datetime.now(timezone.utc);jif_year,sjr_year=latest_years(now)
    registry=combined_registry()
    if not registry:raise SystemExit('No journal list found; aborting without overwriting data')
    old=read_json(OUTPUT,{})
    records=old.get('journals',{}) if isinstance(old.get('journals',{}),dict) else {}
    # Prune nothing: verified records may remain useful if a website is blocked later.
    try: sjr_records,sjr_updated=update_sjr(records,registry,sjr_year)
    except Exception as e:print('SCImago update failed, retaining old metrics:',type(e).__name__,e);sjr_records=sjr_updated=0
    diagnostics={}
    try: checked,jif_updated,diagnostics=update_jif(records,registry,jif_year)
    except Exception as e:print('Publisher update failed, retaining old metrics:',type(e).__name__,e);checked=jif_updated=0
    try: publisher_seeded=update_publisher_evidence_import(records,registry,jif_year)
    except Exception as e:print('Publisher evidence import failed:',type(e).__name__,e);publisher_seeded=0
    try: imported=update_jcr_import(records,registry,jif_year)
    except Exception as e:print('Manual official JCR CSV update failed:',type(e).__name__,e);imported=0
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    result={'notes':'Verified-only overlay. No approximated IF; SJR quartiles are not Scopus CiteScore quartiles.',
            'checked_at_utc':now.isoformat(timespec='seconds'),
            'latest_expected_jif_year':jif_year,'latest_expected_sjr_year':sjr_year,
            'checks':{'public_journal_pages':checked,'publisher_jif_updated':jif_updated,'scimago_csv_rows':sjr_records,'sjr_quartiles_updated':sjr_updated,'jcr_import_updated':imported,'publisher_evidence_seeded':publisher_seeded},
            'journals':records}
    OUTPUT.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    coverage=[]
    for j in sorted(registry.values(),key=lambda item:key(item['name'])):
        name=j['name']; verified=records.get(name,{})
        jif=verified.get('impact_factor')
        y=verified.get('impact_factor_year')
        base=j.get('details') or {}
        has_existing=bool(base.get('impact_factor')) and base.get('impact_factor_status')!='provisional'
        status='verified_by_refresh' if jif else ('existing_metadata' if has_existing else 'not_verified')
        sjr=verified.get('sjr_quartile')
        has_existing_q=bool(base.get('sjr_quartile')) and base.get('sjr_quartile_status')!='provisional'
        qstatus='verified_by_refresh' if sjr else ('existing_metadata' if has_existing_q else 'not_verified')
        coverage.append({'journal':name,'status':status,'jif':jif,'year':y,'source':verified.get('impact_factor_source',''),
                         'sjr_status':qstatus,'sjr_q':sjr,'sjr_year':verified.get('sjr_quartile_year'),
                         'sjr_source':verified.get('sjr_quartile_source',''),
                         'last_attempt':diagnostics.get(name,'Not checked in this scheduled batch')})
    report={'checked_at_utc':now.isoformat(timespec='seconds'),
            'journals_total':len(coverage),
            'with_verified_refresh_jif':sum(x['status']=='verified_by_refresh' for x in coverage),
            'with_existing_jif_not_in_refresh':sum(x['status']=='existing_metadata' for x in coverage),
            'still_unverified':sum(x['status']=='not_verified' for x in coverage),
            'sjr_verified_by_refresh':sum(x['sjr_status']=='verified_by_refresh' for x in coverage),
            'sjr_still_unverified':sum(x['sjr_status']=='not_verified' for x in coverage),
            'checked_this_run':len(diagnostics),'journal_attempts':diagnostics,
            'explanation':'No website or generic citations metric can substitute for a JCR JIF. Missing may also mean publisher access is blocked.'}
    (OUTPUT.parent/'metrics_audit.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    with (OUTPUT.parent/'metrics_coverage.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['journal','status','jif','year','source','sjr_status','sjr_q','sjr_year','sjr_source','last_attempt'])
        writer.writeheader();writer.writerows(coverage)
    print('Metrics refresh:',len(registry),'journals;',result['checks'])

if __name__=='__main__':main()
