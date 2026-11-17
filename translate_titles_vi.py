#!/usr/bin/env python3
"""VU's LAB — local, no-API-key English to Vietnamese paper-title translations.

Sources: Helsinki-NLP/opus-mt-en-vi, Apache 2.0 licensed MarianMT model.
Reads:  data/articles.json
Writes: data/title_vi.json  (compatible with existing VU's LAB v5 index.html)
Optional manual corrections: data/title_vi_overrides.json (English title -> Vietnamese title)
Does NOT modify fetch_articles.py, site layout, bookmarks or journal data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from html import unescape
import json
import os
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / 'data' / 'articles.json'
CACHE = ROOT / 'data' / 'title_vi.json'
OVERRIDES = ROOT / 'data' / 'title_vi_overrides.json'
MODEL_NAME = 'Helsinki-NLP/opus-mt-en-vi'
DEFAULT_LIMIT = 100
BATCH_SIZE = 8

# Apply only to words the offline model LEFT in English; it is NOT a dictionary
# that blindly replaces already-translated clinical Vietnamese phrases.
UNTRANSLATED_CLINICAL_TERMS = [
    (r'(?i)\btotal hip arthroplasty\b', 'thay khớp háng toàn phần'),
    (r'(?i)\btotal knee arthroplasty\b', 'thay khớp gối toàn phần'),
    (r'(?i)\brevision arthroplasty\b', 'phẫu thuật thay lại khớp'),
    (r'(?i)\bperiprosthetic joint infection\b', 'nhiễm khuẩn quanh khớp nhân tạo'),
    (r'(?i)\bmeniscal root\b', 'rễ sụn chêm'),
    (r'(?i)\banterior cruciate ligament\b', 'dây chằng chéo trước'),
    (r'(?i)\bposterior cruciate ligament\b', 'dây chằng chéo sau'),
    (r'(?i)\bfemoral neck\b', 'cổ xương đùi'),
    (r'(?i)\bacetabulum\b', 'ổ cối'),
    (r'(?i)\bosteonecrosis\b', 'hoại tử xương'),
    (r'(?i)\bosteoporosis\b', 'loãng xương'),
    (r'(?i)\bosteoarthritis\b', 'thoái hóa khớp'),
    (r'(?i)\bnonunion\b', 'khớp giả'),
    (r'(?i)\bmalunion\b', 'liền xương lệch trục'),
    (r'(?i)\bsystematic review\b', 'tổng quan hệ thống'),
    (r'(?i)\bmeta-analysis\b', 'phân tích gộp'),
]


def load_json(path: Path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def clean(s: str) -> str:
    """Keep mathematical text while removing stray HTML entity syntax."""
    s = unescape(str(s or '')).strip()
    # Europe PMC titles should already be plain text; avoid injecting HTML.
    s = re.sub(r'<\s*/?\s*(?:sup|sub)\s*>', '', s, flags=re.I)
    return re.sub(r'\s+', ' ', s)


def pending_items(articles, cache, overrides, limit):
    result, seen = [], set()
    for article in articles:
        if not isinstance(article, dict):
            continue
        ident, title = str(article.get('id') or '').strip(), clean(article.get('title') or '')
        if not ident or not title or ident in seen:
            continue
        seen.add(ident)
        saved = cache.get(ident)
        if isinstance(saved, dict) and saved.get('en') == title and saved.get('vi'):
            if title not in overrides or saved['vi'] == overrides[title]:
                continue
        result.append((ident, title))
        if len(result) >= limit:
            break
    return result


def polished_translation(text):
    s = clean(text).strip(' \"“”')
    for pat, replacement in UNTRANSLATED_CLINICAL_TERMS:
        s = re.sub(pat, replacement, s)
    return re.sub(r'\s+([,:;.])', r'\1', s)


def plausible(original, translation):
    if not isinstance(translation, str) or not (8 <= len(translation.strip()) <= 750):
        return False
    if not re.search(r'[À-ỹ]', translation):
        return False
    if re.search(r'<\s*\w+', translation):
        return False
    if clean(original).casefold() == clean(translation).casefold():
        return False
    return True


def atomic_save(records):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {'schema': 1, 'updated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
               'translations': records}
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.json.tmp',
                                     dir=CACHE.parent, delete=False) as f:
        temp_path = Path(f.name)
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write('\n')
    temp_path.replace(CACHE)


def make_translator():
    try:
        import torch
        from transformers import MarianMTModel, MarianTokenizer
    except ImportError as exc:
        raise RuntimeError('Dependencies missing: install torch, transformers and sentencepiece') from exc
    print(f'Loading free, open-source translation model: {MODEL_NAME}', flush=True)
    tok = MarianTokenizer.from_pretrained(MODEL_NAME)
    model = MarianMTModel.from_pretrained(MODEL_NAME)
    model.eval()
    torch.set_num_threads(min(4, os.cpu_count() or 2))
    codes = set(getattr(tok, 'supported_language_codes', []) or [])
    target_code = next((x for x in ('>>vie<<', '>>vi<<') if x in codes), '')
    if target_code:
        print('Applying model target-language code:', target_code, flush=True)

    def translate_batch(items):
        titles = [(target_code + ' ' if target_code else '') + text for text in items]
        inp = tok(titles, return_tensors='pt', truncation=True, padding=True, max_length=256)
        with torch.no_grad():
            generated = model.generate(**inp, num_beams=4, max_new_tokens=256, do_sample=False)
        return tok.batch_decode(generated, skip_special_tokens=True)

    return translate_batch


def main():
    if not INPUT.is_file():
        print('ERROR: missing data/articles.json. Run the daily article collection first.', file=sys.stderr)
        return 1
    articles = load_json(INPUT, [])
    if not isinstance(articles, list):
        print('ERROR: invalid data/articles.json; expected an array.', file=sys.stderr)
        return 1
    prior = load_json(CACHE, {})
    records = prior.get('translations', {}) if isinstance(prior, dict) else {}
    if not isinstance(records, dict):
        records = {}
    overrides = load_json(OVERRIDES, {})
    if not isinstance(overrides, dict):
        overrides = {}
    overrides = {clean(k): clean(v) for k, v in overrides.items() if clean(k) and clean(v)}
    try:
        limit = max(1, min(1000, int(os.getenv('VI_MAX_NEW', str(DEFAULT_LIMIT)))))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    pending = pending_items(articles, records, overrides, limit)
    print(f'Titles in feed: {len(articles)}; cached: {len(records)}; selected: {len(pending)}', flush=True)
    if not pending:
        print('Up to date. No API key, no translation fee.', flush=True)
        return 0

    # Allow approved corrections to be applied even if the model is unavailable.
    to_translate = []
    saved = 0
    for ident, title in pending:
        if title in overrides:
            candidate = overrides[title]
            if plausible(title, candidate):
                records[ident] = {'en': title, 'vi': candidate}
                saved += 1
                continue
        to_translate.append((ident, title))
    if saved:
        atomic_save(records)
        print(f'Applied {saved} human-reviewed corrections.', flush=True)
    if not to_translate:
        return 0
    translate_batch = make_translator()
    generated = 0
    problems = 0
    for i in range(0, len(to_translate), BATCH_SIZE):
        block = to_translate[i:i+BATCH_SIZE]
        try:
            outputs = translate_batch([t for _id, t in block])
            if len(outputs) != len(block):
                raise ValueError('Model produced the wrong number of translations')
        except Exception as exc:
            print(f'ERROR: batch {i//BATCH_SIZE+1}: {type(exc).__name__}: {str(exc)[:180]}', file=sys.stderr)
            if generated:
                atomic_save(records)
            return 1
        for (ident, title), rough in zip(block, outputs):
            proposed = polished_translation(rough)
            if plausible(title, proposed):
                records[ident] = {'en': title, 'vi': proposed}
                generated += 1
            else:
                problems += 1
                print(f'WARNING: translation rejected; paper ID={ident[:50]}', flush=True)
        atomic_save(records)  # Save each batch; old translations preserved.
        print(f'Progress: {generated} generated / {len(to_translate)} selected; {problems} rejected', flush=True)
    print('Completed. Vietnamese titles stored in data/title_vi.json. Website code unchanged.', flush=True)
    return 0  # Rejected titles stay English; do not publish unsafe guesses.


if __name__ == '__main__':
    raise SystemExit(main())
