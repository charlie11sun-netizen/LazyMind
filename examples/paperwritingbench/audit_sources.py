"""Check native F2 material pools before drafting; exclusions are requests plus an audit, not filters."""
import argparse
from datetime import date
import json
from pathlib import Path
import re
import unicodedata


def audit(pool, manifest):
    pool = pool.get('data', pool)

    def key(text):
        return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())

    excluded = {key(title) for title in manifest['exclude_titles']}
    cutoff = date.fromisoformat(manifest['published_before']) if manifest.get('published_before') else None
    violations, seen = [], set()
    for item in pool['materials']:
        if item['kind'] != 'papers':
            continue
        for paper in item['content']:
            identity = (paper.get('id'), paper.get('title'))
            if identity in seen:
                continue
            seen.add(identity)
            if any(title and title in key(paper['title']) for title in excluded) or any(
                    re.search(r'(?<!\d)' + re.escape(identifier) + r'(?:v\d+)?(?!\d)', json.dumps(paper))
                    for identifier in manifest['exclude_arxiv']):
                violations.append({'paper': paper['title'], 'reason': 'excluded_target'})
            published = ((paper.get('metadata') or {}).get('published')
                         or (paper.get('extra') or {}).get('published') or '')
            if cutoff:
                if not published:
                    violations.append({'paper': paper['title'], 'reason': 'publication_date_unverified'})
                elif date.fromisoformat(published[:10]) > cutoff:
                    violations.append({'paper': paper['title'], 'reason': 'published_after_cutoff', 'date': published})
    return {'status': 'failed' if violations else 'passed', 'papers_checked': len(seen), 'violations': violations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pool', type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    args = parser.parse_args()
    result = audit(json.loads(args.pool.read_text()), json.loads(args.manifest.read_text()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
