"""Score one deck using both paper rubrics and its own dataset rubric."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

from ..cases import DATASET_CASES, case_files
from . import papers, presentbench, slidesgen
from .codex import Codex, EFFORT, MODEL
from .runner import Progress, digest, lock, read, render_pdf, save, slide_count, totals


def json_digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare(args):
    import pymupdf

    output = args.out
    prompt, material = case_files(DATASET_CASES[args.dataset], args.cases)
    pptx = args.pptx.resolve()
    count = slide_count(pptx)
    source = {
        'dataset': args.dataset, 'task': DATASET_CASES[args.dataset],
        'pptx_sha256': digest(pptx), 'pages': count,
        'case_prompt_sha256': digest(prompt), 'case_material_sha256': digest(material),
    }
    manifest = output / 'artifact.json'
    previous = read(manifest) if manifest.is_file() else None
    if previous and any(previous[key] != value for key, value in source.items()):
        raise ValueError('Output directory belongs to a different deck or dataset')
    if args.pdf:
        pdf = args.pdf.resolve()
    else:
        rendered = output / 'rendered'
        rendered.mkdir(exist_ok=True)
        pdf = rendered / f'{pptx.stem}.pdf'
        if not pdf.is_file() or previous is None:
            pdf = render_pdf(pptx, rendered)
    with pymupdf.open(pdf) as document:
        if len(document) != count:
            raise ValueError(f'The PDF must contain {count} pages to match the PPTX')
    source['pdf_sha256'] = digest(pdf)
    if previous and previous != source:
        raise ValueError('Output directory belongs to a different PDF render')
    save(manifest, source)

    templates = papers.load_prompts(args.deeppresenter)
    prepared_papers = papers.prepare(pdf, output, args.dataset, args.presenter_python)
    local = {'deeppresenter_constraints': prepared_papers[2], 'deepslides_balance': prepared_papers[3]}
    if args.dataset == 'presentbench':
        prepared_dataset, rubric = presentbench.prepare(args.dataset_code, args.presentbench_data, pdf,
                                                        material, output)
        local['presentbench_deterministic_checks'] = prepared_dataset[2]
    else:
        prepared_dataset, rubric = slidesgen.prepare(args.dataset_code, pdf, output)
        local['slidesgen_editability'] = slidesgen.editability_evidence(pptx)
        if args.aesthetics_model:
            local['slidesgen_aesthetics'] = slidesgen.aesthetics(args.dataset_code, prepared_dataset[0],
                                                                output, args.aesthetics_model)
    save(output / 'preparation.json', local)
    fingerprints = {**source, 'rubrics_sha256': json_digest([templates, papers.DEEPSLIDES, rubric]),
                    'deterministic_sha256': json_digest({k: v for k, v in local.items()
                                                         if k != 'slidesgen_aesthetics'})}
    return source, fingerprints, local, prepared_papers, prepared_dataset, templates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', choices=DATASET_CASES, required=True)
    parser.add_argument('--pptx', type=Path, required=True)
    parser.add_argument('--pdf', type=Path, help='Existing PDF of the input deck. Default: render with LibreOffice.')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--deeppresenter', type=Path, required=True)
    parser.add_argument('--presenter-python', type=Path, default=Path(sys.executable),
                        help='Python with the pinned DeepPresenter checkout installed')
    parser.add_argument('--dataset-code', type=Path, required=True)
    parser.add_argument('--presentbench-data', type=Path)
    parser.add_argument('--aesthetics-model', type=Path, help='Local SlidesGen-Bench layout detector weights')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.dataset == 'presentbench' and not args.presentbench_data:
        parser.error('--presentbench-data is required with PresentBench')
    if args.dataset == 'presentbench' and args.aesthetics_model:
        parser.error('--aesthetics-model applies to SlidesGen-Bench')
    os.umask(0o077)
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    with lock(args.out):
        source, fingerprints, local, prepared_papers, prepared_dataset, templates = prepare(args)
        if args.prepare_only:
            print(args.out / 'preparation.json')
            return
        progress = Progress(args.out, fingerprints)
        results = args.out / 'scores.json'
        if results.is_file() and progress.path.is_file():
            if args.dataset == 'slidesgen' and args.aesthetics_model:
                previous = read(results)
                previous['dataset_scores']['aesthetics'] = local['slidesgen_aesthetics']
                save(results, previous)
            print(results)
            return
        with Codex(args.out / 'codex') as codex:
            paper_scores = papers.run(codex, progress, prepared_papers, templates)
            if args.dataset == 'presentbench':
                dataset_scores = presentbench.run(codex, progress, prepared_dataset, args.out)
            else:
                dataset_scores = {**slidesgen.run(codex, progress, prepared_dataset),
                                  'editability': local['slidesgen_editability'],
                                  'aesthetics': local.get('slidesgen_aesthetics')}
        save(results, {
            'artifact': source, 'judge': {'model': MODEL, 'effort': EFFORT, 'auth': 'chatgpt'},
            'paper_scores': paper_scores, 'dataset_scores': dataset_scores,
            'usage': {'scoring': progress.data['usage'],
                      'warmup': totals([item['usage'] for item in progress.data['warmups']])},
        })
        print(results)


if __name__ == '__main__':
    main()
