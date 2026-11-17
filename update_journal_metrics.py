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
MAX_PUBLISHER_CHECKS=int(os.getenv('VU_METRICS_MAX_PAGES','60'))
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
    with urlopen(req,timeout=14) as resp:
        return resp.read(limit+1)[:limit],resp.geturl(),resp.headers.get('Content-Type','')


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title=[]; self.parts=[]; self.in_title=False; self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag=='title':self.in_title=True
        if tag in ('script','style','noscript'): self.skip+=1
    def handle_endtag(self,tag):
        if tag=='title':self.in_title=False
        if tag in ('script','style','noscript') and self.skip: self.skip-=1
    def handle_data(self,data):
        if self.in_title:self.title.append(data)
        if not self.skip:self.parts.append(data)

# Separate 2-year JIF from 5-year JIF and other indicators.
PATTERNS=[
 re.compile(r'(?<!5.year )\bJournal Impact Factor\s*[:：-]?\s*(\d{1,3}(?:\.\d{1,2})?)\s*\(\s*(20\d{2})\s*\)',re.I),
 re.compile(r'\b(20\d{2})\s+Journal Impact Factor\s*[:：-]?\s*(\d{1,3}(?:\.\d{1,2})?)\b',re.I),
 re.compile(r'\b(20\d{2})\s+Impact Factor\s*[:：-]?\s*(\d{1,3}(?:\.\d{1,2})?)\b',re.I),
]


def extract_jif(source_html, expected_title, latest_year):
    p=PageText()
    try:p.feed(source_html)
    except Exception:return None
    page_title=key(' '.join(p.title)); expected=key(expected_title)
    # Require a journal-specific page, not a publisher home/search page
    if len(expected)<8 or expected not in page_title:
        return None
    text=re.sub(r'\s+',' ',unescape(' '.join(p.parts)))
    found=defaultdict(set)
    for i,rx in enumerate(PATTERNS):
        for m in rx.finditer(text):
            num,year=(m.group(1),m.group(2)) if i==0 else (m.group(2),m.group(1))
            try:val=float(num); yr=int(year)
            except ValueError:continue
            if 0 < val < 200 and 2015<=yr<=latest_year:
                found[yr].add(val)
    if not found:return None
    year=max(found)
    # Multiple different JIF values for the same year: likely unrelated titles
    if len(found[year])!=1:return None
    return list(found[year])[0],year


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


def pick_publisher_urls(registry,jif_year):
    # Keep the job light: cap checks, rotate every weekly run, prioritise
    # known new additions, missing or stale JIF values, and journal pages.
    entries=[]
    for journal in registry.values():
        d=journal['details']; url=d.get('journal_url') or journal['url']
        if not supported_url(url):continue
        # Skip very generic publisher pages and DOI redirects.
        if urlparse(url).path in ('','/') and not d.get('journal_url'):continue
        entries.append((journal['name'],url))
    entries.sort(key=lambda a:key(a[0]))
    if len(entries)<=MAX_PUBLISHER_CHECKS:return entries
    week=datetime.now(timezone.utc).isocalendar().week
    start=((week-1)*MAX_PUBLISHER_CHECKS)%len(entries)
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
    def one(pair):
        name,url=pair
        try:
            payload,final,content_type=get(url,limit=1_500_000)
            if 'pdf' in content_type.lower():return name,None
            result=extract_jif(payload.decode('utf-8','replace'),name,jif_year)
            return name,(result,final) if result else None
        except (OSError,ValueError,HTTPError,URLError,TimeoutError):return name,None
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs=[pool.submit(one,entry) for entry in selected]
        for fut in as_completed(futs):
            name,result=fut.result();checked+=1
            if result:
                (value,year),url=result
                if best_add(records,name,'impact_factor',value,year,url): updated+=1
    return checked,updated


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
    try: checked,jif_updated=update_jif(records,registry,jif_year)
    except Exception as e:print('Publisher update failed, retaining old metrics:',type(e).__name__,e);checked=jif_updated=0
    try: imported=update_jcr_import(records,registry,jif_year)
    except Exception as e:print('Manual official JCR CSV update failed:',type(e).__name__,e);imported=0
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    result={'notes':'Verified-only overlay. No approximated IF; SJR quartiles are not Scopus CiteScore quartiles.',
            'checked_at_utc':now.isoformat(timespec='seconds'),
            'latest_expected_jif_year':jif_year,'latest_expected_sjr_year':sjr_year,
            'checks':{'public_journal_pages':checked,'publisher_jif_updated':jif_updated,'scimago_csv_rows':sjr_records,'sjr_quartiles_updated':sjr_updated,'jcr_import_updated':imported},
            'journals':records}
    OUTPUT.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('Metrics refresh:',len(registry),'journals;',result['checks'])

if __name__=='__main__':main()
