"""Package an extracted benchmark sample without reading the reference paper or GT citations."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re


def tables_from_log(log):
    tables, title = [], ''
    lines, index = log.splitlines(), 0

    def cells(text):
        return [cell.strip().replace('**', '').replace(r'\|', '|')
                for cell in re.split(r'(?<!\\)\|', text.strip().strip('|'))]

    number = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?%?')
    while index < len(lines):
        line = lines[index]
        if line.startswith('**Table Data:'):
            title = line.strip('*').removeprefix('Table Data:').strip()
        if not (line.startswith('|') and index + 1 < len(lines)
                and re.fullmatch(r'[|\s:\-]+', lines[index + 1])):
            index += 1
            continue
        columns, raw = cells(line), []
        index += 2
        while index < len(lines) and lines[index].startswith('|'):
            row = cells(lines[index])
            if len(row) != len(columns):
                raise ValueError('Inconsistent result table width: ' + title)
            raw.append(row)
            index += 1
        if not raw:
            raise ValueError('Empty result table: ' + title)
        types, units = [], []
        for column in zip(*raw):
            present = [cell for cell in column if cell not in ('', '-', 'N/A')]
            numeric = bool(present) and all(number.fullmatch(cell) for cell in present)
            types.append('number' if numeric else 'string')
            units.append('%' if numeric and all(cell.endswith('%') for cell in present) else None)
        rows = [[None if cell in ('', '-', 'N/A') else float(cell.rstrip('%')) if kind == 'number' else cell
                 for cell, kind in zip(row, types)] for row in raw]
        tables.append({'id': f'table{len(tables) + 1}', 'description': title or f'Table {len(tables) + 1}',
                       'table': {'columns': columns, 'column_types': types, 'units': units, 'rows': rows}})
    if not tables:
        raise ValueError('No Markdown result tables found; provide structured results instead of inventing them')
    return {'tables': tables}


def prepare(materials, *, idea='idea_sparse.md', topic=None, exclude_titles=(), exclude_arxiv=(), cutoff=None):
    paths = [materials / idea, materials / 'experimental_log.md', materials / 'figures/info.json']
    research, log = (path.read_text(encoding='utf-8-sig') for path in paths[:2])
    captions = json.loads(paths[2].read_text())
    images = [materials / 'figures' / item['name'] for item in captions]
    for path in images:
        if path.parent.resolve() != (materials / 'figures').resolve() or not path.is_file():
            raise ValueError('Missing or invalid supplied figure: ' + path.name)
    request = ('Write a complete English research paper using the supplied idea, existing experiment log, '
               'result tables and figures. Import results at F3; interpret supplied values and reuse figures at F4. '
               'Do not run new experiments or generate simulated data.')
    if exclude_titles or exclude_arxiv:
        request += (' Exclude these target papers and all versions/mirrors from reading, review and citations: '
                    + json.dumps({'titles': list(exclude_titles), 'arxiv_ids': list(exclude_arxiv)}, ensure_ascii=False))
    if cutoff:
        date.fromisoformat(cutoff)
        request += f' Use literature published on or before {cutoff}; do not treat later work as prior art.'
    notes = '\n\n'.join(f'## Source: {path.name}\nSHA256: {hashlib.sha256(path.read_bytes()).hexdigest()}\n\n{text}'
                        for path, text in zip(paths[:2], (research, log)))
    notes += '\n\n' + '\n\n'.join(f'## {item["name"]}\n\n{item["caption"]}' for item in captions)
    data = tables_from_log(log)
    value = {'topic': topic or research.strip().splitlines()[0].lstrip('# ').strip(),
             'request': request, 'question': research, 'writing_materials': notes,
             'constraints': {'paper_language': 'en', 'paper_type': 'conference', 'citation_style': 'IEEE'},
             'data': data, 'analysis_mode': 'interpret_provided',
             'data_context': {'description': 'Externally supplied aggregate results; not reproduced by LazyMind.'},
             'study_design': {'description': 'Do not infer missing sample counts, variance or significance.'},
             'experiment': {'executed': False, 'plan': 'Import existing benchmark results.',
                            'results': data, 'report': log},
             'result_meta': {'execution_status': 'not_run', 'data_origin': 'user_supplied', 'method': 'empirical',
                             'reason': 'Provided benchmark results; LazyMind has not reproduced the experiments.',
                             'assumptions': [], 'metrics': [], 'seed': None}}
    manifest = {'ground_truth_supplied_to_writer': False, 'exclude_titles': list(exclude_titles),
                'exclude_arxiv': list(exclude_arxiv), 'published_before': cutoff,
                'workflow_request': ('Use the paper-writing workflow and bind the exact attached filename '
                                     'to paper_input. Preserve the supplied constraints and materials. ' + request),
                'sources': [{'path': str(path.relative_to(materials)),
                             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths + images],
                'upload_with_json': [str(path.relative_to(materials)) for path in images]}
    return value, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('materials', type=Path, help='Extracted sample raw_materials directory')
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--idea', choices=['idea_sparse.md', 'idea_dense.md'], default='idea_sparse.md')
    parser.add_argument('--topic')
    parser.add_argument('--exclude-title', action='append', default=[])
    parser.add_argument('--exclude-arxiv', action='append', default=[])
    parser.add_argument('--published-before', type=date.fromisoformat)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('Use a new output path to preserve earlier inputs')
    value, manifest = prepare(args.materials, idea=args.idea, topic=args.topic,
                              exclude_titles=args.exclude_title, exclude_arxiv=args.exclude_arxiv,
                              cutoff=args.published_before.isoformat() if args.published_before else None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    args.out.with_suffix('.manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    launch = manifest['workflow_request'].replace('the exact attached filename', args.out.name)
    args.out.with_suffix('.request.txt').write_text(launch + '\n')
    print(json.dumps({'input': str(args.out), 'tables': len(value['data']['tables']),
                      'upload_with_json': manifest['upload_with_json']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
