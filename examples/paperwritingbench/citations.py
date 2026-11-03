"""Extract citations, resolve Semantic Scholar IDs, and run the pinned upstream F1 formula."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import time

from . import transport

ROOT = Path(__file__).resolve().parent


def upstream_node(path, kind, name):
    tree = ast.parse((ROOT / 'upstream' / path).read_text())
    return next(node for node in ast.walk(tree) if isinstance(node, kind) and node.name == name)


def upstream_constant(path, name):
    tree = ast.parse((ROOT / 'upstream' / path).read_text())
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))


def compute_metrics(reference, candidate, directory):
    # Compile only the official arithmetic method, avoiding unrelated imports and
    # the upstream module's import-time API-key print.
    node = upstream_node('autoraters/citation_f1.py', ast.FunctionDef, 'evaluate_from_data')
    scope = {'Dict': dict, 'os': os, 'json': json}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'upstream/autoraters/citation_f1.py', 'exec'), scope)
    return scope['evaluate_from_data'](SimpleNamespace(log_folder=str(directory)), reference, candidate)


def title_key(value):
    return ''.join(char for char in unicodedata.normalize('NFKC', value).casefold() if char.isalnum())


def reference_index(reference):
    index = {}
    for item in reference.get('citation_info', []):
        identifier = item.get('fetched_paper_id')
        if not identifier or identifier not in reference['all_ids']:
            continue
        for key in ('extracted_paper_title', 'fetched_paper_title'):
            title = item.get(key) or ''
            if title_key(title):
                index.setdefault(title_key(title), {})[identifier] = item.get('fetched_paper_title') or title
    return index


def lookup(title, directory, number):
    # The query, fields, limit and first-result identity rule match upstream.
    url = 'https://api.semanticscholar.org/graph/v1/paper/search?' + urllib.parse.urlencode(
        {'query': title, 'limit': 1, 'fields': 'paperId,title'})
    request = {'title': title, 'url': url, 'attempts': []}
    filename = directory / f'lookup-{number:03d}.json'
    headers = {'User-Agent': 'LazyMind-PaperWritingBench/1.0'}
    if os.environ.get('SEMANTIC_SCHOLAR_API_KEY'):
        headers['x-api-key'] = os.environ['SEMANTIC_SCHOLAR_API_KEY']
    try:
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=25) as response:
                    data = json.load(response)
                request['attempts'].append({'status': 200})
                request['response'] = data
                rows = data.get('data') or []
                if not rows:
                    return None
                return {'fetched_paper_id': rows[0]['paperId'], 'fetched_paper_title': rows[0]['title']}
            except urllib.error.HTTPError as error:
                request['attempts'].append({'status': error.code})
                if error.code not in (429, 502, 503, 504) or attempt == 2:
                    raise RuntimeError(
                        f'Semantic Scholar lookup unavailable (HTTP {error.code}); no score produced.') from error
                time.sleep(attempt + 1)
    finally:
        filename.write_text(json.dumps(request, ensure_ascii=False, indent=2))


def extract(pdf, model, directory):
    text = transport.load_paper(str(pdf))
    prompt = upstream_constant('utils/pdf_utils.py', 'papaer_text_to_reference_text_prompt_template')
    raw = transport.call_text(prompt.format(paper_text=text), model, return_json=False)['raw_response'].strip()
    (directory / 'extracted-references.txt').write_text(raw)
    # The upstream prompt illustrates the output with surrounding quotes.
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].strip()
    # Preserve upstream's numbered-reference segmentation.
    references = [part.strip().replace('\n', ' ').replace('  ', ' ') for part in re.split(r'\[\d+\]', raw)
                  if part.strip()]
    if not references or not re.match(r'^\[\d+\]', raw):
        raise ValueError('Reference extraction did not return numbered bibliography entries')
    rules = upstream_constant('utils/content_parsing_utils.py', 'extract_title_prompt_template')
    prompt = ('Apply the following bibliographic parsing rules separately to EVERY supplied citation.\n\n'
              + rules.format(citation_text='[one input citation]')
              + '\n\nFor this batch, return a JSON object mapping each input index to its title string. '
              'The title-only rule applies to each JSON value. Preserve every index. '
              'Do not add or suggest references.\nCITATIONS:\n'
              + json.dumps(dict(enumerate(references, 1)), ensure_ascii=False))
    titles = transport.call_text(prompt, model)['parsed_response']
    expected = {str(i) for i in range(1, len(references) + 1)}
    if set(titles) != expected or any(not isinstance(title, str) for title in titles.values()):
        raise ValueError('Title extraction omitted or changed citation indices')
    (directory / 'extracted-titles.json').write_text(json.dumps(titles, ensure_ascii=False, indent=2))
    return references, titles


def score(pdf, reference_path, model, directory, cache_path=None):
    reference = json.loads(reference_path.read_text())
    required = {'all_ids', 'p0_ids', 'p1_ids', 'citation_info'}
    if not required <= reference.keys():
        raise ValueError('Provide the dataset GT citation JSON with all_ids, p0_ids, p1_ids and citation_info')
    all_ids, p0, p1 = (set(reference[key]) for key in ('all_ids', 'p0_ids', 'p1_ids'))
    if not all_ids or p0 & p1 or p0 | p1 != all_ids:
        raise ValueError('GT citation IDs or P0/P1 partition are inconsistent')
    (directory / 'reference-source.json').write_text(json.dumps({
        'path': str(reference_path.resolve()), 'sha256': hashlib.sha256(reference_path.read_bytes()).hexdigest(),
        'priority_source': 'provided dataset annotations; not reclassified by the current judge'}, indent=2))
    references, titles = extract(pdf, model, directory)
    known = reference_index(reference)
    cache_path = Path(cache_path) if cache_path else directory / 'identity-cache.json'
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    entries, ids = [], set()
    for number, citation in enumerate(references, 1):
        title = titles[str(number)]
        matches = known.get(title_key(title), {})
        source, resolved = 'unresolved', None
        if len(matches) == 1:
            identifier, fetched_title = next(iter(matches.items()))
            resolved = {'fetched_paper_id': identifier, 'fetched_paper_title': fetched_title}
            source = 'dataset_identity_cache_exact_title'
        elif title_key(title) in cache:
            resolved, source = cache[title_key(title)], 'semantic_scholar_cache_exact_title'
        elif title.strip():
            resolved, source = lookup(title, directory, number), 'semantic_scholar_search'
            time.sleep(0.5)
            if resolved:
                cache[title_key(title)] = resolved
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
        if resolved:
            ids.add(resolved['fetched_paper_id'])
        entries.append({'citation_text': citation, 'extracted_paper_title': title,
                        'fetched_paper_id': resolved['fetched_paper_id'] if resolved else 'gen_NA',
                        'fetched_paper_title': resolved['fetched_paper_title'] if resolved else None,
                        'resolution_source': source, 'priority': 'P1',
                        'ground_truth_match': bool(resolved and resolved['fetched_paper_id'] in all_ids),
                        'ground_truth_priority': ('P0' if resolved['fetched_paper_id'] in p0 else 'P1')
                        if resolved and resolved['fetched_paper_id'] in all_ids else None})
        # Preserve completed resolutions even if a later external lookup fails.
        (directory / 'citation-matches.json').write_text(json.dumps(entries, ensure_ascii=False, indent=2))
    candidate = {'citation_info': entries, 'all_ids': sorted(ids), 'all_ids_count': len(ids)}
    metrics = compute_metrics(reference, candidate, directory)
    unresolved = [item for item in entries if item['fetched_paper_id'] == 'gen_NA']
    metrics['resolution'] = {
        'extracted_references': len(entries), 'resolved_references': len(entries) - len(unresolved),
        'unresolved_references': len(unresolved), 'complete': not unresolved,
        'precision_denominator': 'unique resolved Semantic Scholar IDs, following upstream',
        'unresolved': unresolved}
    metrics['missing_reference_ids'] = sorted(all_ids - ids)
    metrics['unmatched_candidate_ids'] = sorted(ids - all_ids)
    return metrics
