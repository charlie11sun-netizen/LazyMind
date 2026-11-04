"""Prepare manuscript paragraphs and explicit evidence for the material-based adapters."""
import hashlib
import json
import re

from .autosurvey import abstracts


def structure(manuscript, references=None):
    # Remove the bibliography only from fact extraction; WritingBench receives the complete manuscript.
    body = re.sub(r'<a id="paper-references-start"></a>[\s\S]*?<a id="paper-references-end"></a>',
                  '', manuscript)
    lines, skipping, reference_level = [], False, 0
    for line in body.splitlines():
        heading = re.match(r'^(#{1,6})\s+(.+)', line)
        if heading and skipping and len(heading[1]) <= reference_level:
            skipping = False
        if heading and heading[2].strip().lower() in {'references', 'bibliography', '参考文献'}:
            skipping, reference_level = True, len(heading[1])
        if not skipping:
            lines.append(line)
    body = '\n'.join(lines).strip()
    single_title = len(re.findall(r'(?m)^# ', body)) <= 1
    main_level = 2 if single_title and re.search(r'(?m)^## ', body) else 1
    sections, paragraphs, block = [], [], []
    current, title = None, 'Preamble'

    def flush():
        nonlocal current
        text = '\n'.join(block).strip()
        block.clear()
        if not text or text.startswith(('<a ', '<!--')):
            return
        if current is None:
            current = {'id': f'S{len(sections) + 1:02}', 'title': title, 'paragraphs': []}
            sections.append(current)
        identifiers = re.findall(r'#paper-ref-([A-Za-z0-9_.:-]+)', text)
        for group in re.findall(r'\[(\d+(?:;\d+)*)\]', text):
            identifiers += [(references or {})[number] for number in group.split(';')
                            if number in (references or {})]
        paragraph = {'id': f'P{len(paragraphs) + 1:03}', 'section': title, 'section_id': current['id'],
                     'type': 'figure_caption' if text.startswith('![') else 'prose', 'text': text,
                     'cited_ids': list(dict.fromkeys(identifiers))}
        paragraphs.append(paragraph)
        current['paragraphs'].append(paragraph)

    for line in body.splitlines():
        heading = re.match(r'^(#{1,6})\s+(.+)', line)
        if heading:
            flush()
            title = heading[2].strip()
            if len(heading[1]) == main_level:
                current = {'id': f'S{len(sections) + 1:02}', 'title': title, 'paragraphs': []}
                sections.append(current)
        elif not line.strip():
            flush()
        else:
            block.append(line)
    flush()
    return {'body': body, 'paragraphs': paragraphs, 'sections': sections}


def prepare(args):
    manuscript = args.manuscript.read_text(encoding='utf-8-sig')
    sources, checksums = {}, {}
    for path in args.source:
        if path.name in sources:
            raise ValueError('Duplicate source filename: ' + path.name)
        sources[path.name] = path.read_text(encoding='utf-8-sig')
        checksums[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    literature = list(abstracts(args.literature).values()) if args.literature else []
    literature = [{**paper, 'text': paper['abs']} for paper in literature]
    references = json.loads(args.references.read_text(encoding='utf-8-sig')) if args.references else {}
    data = {'task': {'request': args.assignment.read_text(encoding='utf-8-sig')},
            'sources': sources, 'literature': literature, 'manuscript': manuscript,
            'reference': references,
            'layout': {'embedded_markdown_images': len(re.findall(r'!\[[^\n]*\]\(', manuscript)),
                       'markdown_table_rows': len(re.findall(r'(?m)^\s*\|[^\n]+\|\s*$', manuscript)),
                       'visual_aesthetics_scored': False},
            'source_manifest': {'manuscript_sha256': hashlib.sha256(args.manuscript.read_bytes()).hexdigest(),
                                'sources_sha256': checksums}, **structure(manuscript, references)}
    if args.provenance:
        data['workflow_provenance'] = {args.provenance.name: args.provenance.read_text(encoding='utf-8-sig')}
    return data
