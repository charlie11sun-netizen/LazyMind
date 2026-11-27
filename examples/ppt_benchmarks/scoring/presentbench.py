"""PresentBench's Alphabet checklist and aggregation with a Codex judge."""

import re
import sys

from .codex import image, text
from .runner import Check, digest, pages, save


SECTIONS = ('material_independent', 'material_dependent')


def boxed_verdict(answer):
    matches = re.findall(r'\\boxed\{([^}]+)\}', answer)
    values = [re.sub(r'\\[a-zA-Z]+\s*', '', value).replace('{', '').replace('}', '')
              .strip().strip('.').strip().lower() for value in matches]
    if values:
        verdict = values[0] if values[0] in ('yes', 'no') else values[-1]
        if verdict in ('yes', 'no'):
            return verdict
    raise ValueError('No valid PresentBench boxed verdict')


def prepare(checkout, data, pdf, material, output):
    import pymupdf

    sys.path.insert(0, str(checkout))
    from scoring import compute_scores
    from utils.judge_utils import load_judge_prompt

    economics = data / 'economics'
    case = economics / 'Alphabet_Investor_Relations/2024q1/generation_task/judge_prompt.json'
    common = economics / 'common_judge_prompt.json'
    weights = economics / 'judge_weights.yaml'
    lists = load_judge_prompt(str(case), str(common), str(pdf))
    results = {section: {} for section in SECTIONS}
    checks = {section: [] for section in SECTIONS}
    for section, groups in zip(SECTIONS, lists, strict=True):
        for group, prompts in enumerate(groups, 1):
            results[section][str(group)] = {}
            for index, prompt in enumerate(prompts, 1):
                identifier = f'{group}.{index}'
                if callable(prompt):
                    results[section][str(group)][identifier] = prompt(str(pdf))
                else:
                    checks[section].append((str(group), identifier, Check(f'presentbench.{section}.{identifier}',
                                                                        [text(prompt)], validate=boxed_verdict)))
    if not any(checks.values()):
        raise ValueError('PresentBench checklist has no model-judged items')
    evidence = {}
    for label, source in (('deck', pdf), ('material', material)):
        images = pages(source, output / f'presentbench-{label}', dpi=100, ext='jpg', renderer='pymupdf')
        evidence[label] = []
        with pymupdf.open(source) as document:
            for index, (page, item) in enumerate(zip(document, images, strict=True), 1):
                heading = 'Slide' if label == 'deck' else 'Source material page'
                evidence[label].extend((text(f'{heading} {index}\n{page.get_text()}'), image(item)))
    fingerprint = {path.name: digest(path) for path in (case, common, weights)}
    fingerprint['resolved_checks'] = {section: [check.inputs[0]['text'] for _, _, check in entries]
                                      for section, entries in checks.items()}
    return (checks, evidence, results, compute_scores, weights), fingerprint


def run(codex, progress, prepared, output):
    checks, evidence, results, compute_scores, weights = prepared
    for section, entries in checks.items():
        documents = evidence['deck']
        if section == 'material_dependent':
            documents = documents + evidence['material']
        progress.group(codex, f'presentbench.{section}', documents, [check for _, _, check in entries])
        for group, identifier, check in entries:
            answer = progress.answer(check.name)
            results[section][group][identifier] = {'answer': boxed_verdict(answer), 'explanation': answer}
    verdicts = output / 'presentbench-verdicts.json'
    save(verdicts, results)
    return compute_scores(str(verdicts), str(weights))
