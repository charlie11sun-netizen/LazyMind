"""WritingBench-, ALCE- and FActScore-inspired paper evaluation; not official benchmark scores."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .autosurvey import Calls, fingerprint, read, save
from .client import Client
from .material_prepare import prepare, structure

PROMPTS = Path(__file__).with_name('material-prompts.json')
FACT_LABELS = {'supported', 'derived_supported', 'contradicted', 'unsupported_added',
               'source_conflict', 'evidence_unavailable', 'not_factual'}
CITATION_LABELS = {'supported', 'contradicted', 'insufficient_evidence', 'missing_citation'}
ELIGIBILITY = {'literature_explicit', 'literature_contextual', 'literature_uncited',
               'exclude_abstract', 'exclude_own_study', 'exclude_author_reasoning'}


def parse(raw):
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Evaluator must return a JSON object')
    return value


def rows(value, key, id_key, expected):
    result = value[key]
    if not isinstance(result, list) or sorted(row[id_key] for row in result) != sorted(expected):
        raise ValueError('Evaluator omitted, duplicated or invented IDs: ' + key)
    return result


def ratio(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def aggregate(extracted, verified):
    """Preserve the pilot's disclosed denominators, with unknown counts visible."""
    result = {}
    if 'facts' in verified:
        counts = Counter(row['verdict'] for row in verified['facts'])
        supported = counts['supported'] + counts['derived_supported']
        wrong = counts['contradicted'] + counts['unsupported_added']
        factual = sum(counts.values()) - counts['not_factual']
        result['factscore_inspired'] = {
            'counts': dict(counts), 'supported': supported, 'writer_error_or_unsupported': wrong,
            'verifiable_denominator': supported + wrong, 'verifiable_precision': ratio(supported, supported + wrong),
            'strict_support_fraction': ratio(supported, factual),
            'evidence_unavailable_fraction': ratio(counts['evidence_unavailable'], factual),
            'scope': 'Occurrence-weighted model judgments against provided sources, not official FActScore.'}
    if 'citation_units' in verified:
        counts = Counter(row['joint_verdict'] for row in verified['citation_units'])
        units = {row['unit_id']: row for row in extracted['citation_units']}
        links = Counter(supported_useful=0, irrelevant_or_contradicted=0, unknown=0)
        for row in verified['citation_units']:
            cited = set(units[row['unit_id']]['cited_ids'])
            useful = set(row['individually_sufficient_ids']) | set(row['necessary_source_ids'])
            irrelevant = set(row['irrelevant_source_ids'])
            if not (useful | irrelevant) <= cited or useful & irrelevant:
                raise ValueError('Citation usefulness has unbound or contradictory source IDs')
            for identifier in cited:
                if row['joint_verdict'] == 'supported' and identifier in useful:
                    links['supported_useful'] += 1
                elif row['joint_verdict'] == 'contradicted' or identifier in irrelevant:
                    links['irrelevant_or_contradicted'] += 1
                else:
                    links['unknown'] += 1
        n = len(verified['citation_units'])
        verifiable = n - counts['insufficient_evidence']
        useful, irrelevant = links['supported_useful'], links['irrelevant_or_contradicted']
        result['alce_inspired'] = {
            'statement_counts': dict(counts), 'statements': n, 'citation_counts': dict(links),
            'verifiable_recall': ratio(counts['supported'], verifiable),
            'verifiable_recall_denominator': verifiable, 'strict_recall_lower_bound': ratio(counts['supported'], n),
            'verifiable_precision': ratio(useful, useful + irrelevant),
            'strict_precision_lower_bound': ratio(useful, sum(links.values())),
            'unknown_citation_fraction': ratio(links['unknown'], sum(links.values())),
            'scope': 'Scientific-paper adaptation with abstention and GPT judgments; not official ALCE or citation F1.'}
    return result


class Evaluator:
    def __init__(self, data, calls, specification, jobs):
        self.data, self.calls, self.specification, self.jobs = data, calls, specification, jobs

    def call(self, name, task, payload, validator=None):
        prompt = self.specification['prompts'][task] + '\n\nEVALUATION DATA:\n' + json.dumps(payload, ensure_ascii=False)
        check = (lambda raw: validator(parse(raw))) if validator else None
        value = parse(self.calls.chat(prompt, 0, name, 'json', validator=check))
        save(self.calls.out / (name + '.json'), value)
        return value

    def parallel(self, jobs):
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            pending = {pool.submit(fn): key for key, fn in jobs.items()}
            return {pending[future]: future.result() for future in as_completed(pending)}

    def initial(self, metrics, bundle=None):
        context = {'task': self.data['task'], 'research_sources': self.data['sources']}

        def validate_rubric(value):
            criteria = rows(value, 'criteria', 'id', ['C1', 'C2', 'C3', 'C4', 'C5'])
            for criterion in criteria:
                if not criterion['name_zh'] or not criterion['description'] or not all(
                        criterion['anchors'].get(key) for key in ['1-2', '3-4', '5-6', '7-8', '9-10']):
                    raise ValueError('Writing rubric is missing descriptions or score anchors')

        def validate_ledger(value):
            identifiers = [group['id'] for group in value['evidence_groups']]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError('Evidence ledger contains duplicate group IDs')

        if bundle is not None:
            if bundle['context_sha256'] != fingerprint(context):
                raise ValueError('Shared rubric requires the same assignment and research sources')
            ledger, rubric = bundle['ledger'], bundle.get('rubric')
            if 'writingbench' in metrics:
                validate_rubric(rubric)
        else:
            jobs = {'ledger': lambda: self.call('01-input-evidence-ledger', 'ledger', context, validate_ledger)}
            if 'writingbench' in metrics:
                jobs['rubric'] = lambda: self.call('00-rubric-generation', 'rubric', context, validate_rubric)
            values = self.parallel(jobs)
            ledger, rubric = values['ledger'], values.get('rubric')
        validate_ledger(ledger)
        frozen = {'context_sha256': fingerprint(context), 'rubric': rubric, 'ledger': ledger}
        save(self.calls.out / 'rubric-frozen.json', frozen)
        return rubric, ledger

    def research_sources(self):
        overlap = set(self.data['sources']) & set(self.data.get('workflow_provenance', {}))
        if overlap:
            raise ValueError('Workflow provenance duplicates a research source ID')
        return {**self.data['sources'], **self.data.get('workflow_provenance', {})}

    def writing(self, rubric, ledger):
        jobs = {}
        for criterion in rubric['criteria']:
            def score(criterion=criterion):
                def validate(value, criterion=criterion):
                    if value['criterion_id'] != criterion['id'] or type(value['score']) is not int:
                        raise ValueError('Writing score has a wrong criterion or non-integer score')
                    if not 1 <= value['score'] <= 10 or not value['reason_zh']:
                        raise ValueError('Writing score must be 1..10 with a reason')
                payload = {'criterion': criterion, 'task': self.data['task'],
                           'research_sources': self.research_sources(), 'input_ledger': ledger,
                           'manuscript': self.data['manuscript'], 'literature_sources': self.data['literature'],
                           'layout_evidence': self.data.get('layout', {})}
                return self.call('writing-full-' + criterion['id'], 'writing', payload, validate)
            jobs[criterion['id']] = score
        jobs['coverage'] = lambda: self.call('material-coverage', 'coverage', {
            'input_ledger': ledger, 'manuscript': self.data['body']},
            lambda v: rows(v, 'groups', 'id', [g['id'] for g in ledger['evidence_groups']]))
        values = self.parallel(jobs)
        save(self.calls.out / 'writing-scores.json', values)
        return {'criteria': [values[c['id']] for c in rubric['criteria']],
                'mean_1_to_10': round(sum(values[c['id']]['score'] for c in rubric['criteria']) / 5, 2),
                'material_coverage': values['coverage']}

    def extract(self):
        jobs = {}
        for section in self.data['sections']:
            ps = [p for p in section['paragraphs'] if p['type'] == 'prose']
            if ps:
                jobs[section['id']] = lambda ps=ps, sid=section['id']: self.call(
                    'extract-' + sid, 'extract', {'paragraphs': ps},
                    lambda value, ps=ps: rows(value, 'paragraphs', 'paragraph_id', [p['id'] for p in ps]))
        values = self.parallel(jobs)
        by_id = {p['id']: p for p in self.data['paragraphs'] if p['type'] == 'prose'}
        facts, units, invalid, coverage = [], [], [], []
        for _sid, value in sorted(values.items()):
            for row in value['paragraphs']:
                paragraph = by_id[row['paragraph_id']]
                coverage.append(paragraph['id'])
                for key, output, id_key, prefix in [('facts', facts, 'fact_id', 'F'),
                                                    ('citation_units', units, 'unit_id', 'U')]:
                    for item in row.get(key, []):
                        if not item.get('quote') or item['quote'] not in paragraph['text']:
                            invalid.append({'paragraph_id': paragraph['id'], 'kind': key, 'item': item})
                            continue
                        item = dict(item)
                        if key == 'facts':
                            item['cited_ids'] = paragraph['cited_ids']
                        elif not set(item['cited_ids']) <= set(paragraph['cited_ids']):
                            raise ValueError('Extracted citation IDs are absent from the paragraph')
                        item.update({id_key: f'{prefix}{len(output) + 1:04}', 'paragraph_id': paragraph['id'],
                                     'section': paragraph['section']})
                        output.append(item)
        if sorted(coverage) != sorted(by_id):
            raise ValueError('Extraction did not cover every prose paragraph exactly once')
        result = {'facts': facts, 'citation_units': units, 'invalid_extraction_quotes': invalid,
                  'paragraph_coverage': {'expected': len(by_id), 'returned': len(coverage)}}
        save(self.calls.out / 'extracted.json', result)
        return result

    def eligible(self, extracted):
        candidates = extracted['citation_units']
        if not candidates:
            return [], {'units': []}
        paragraphs = {p['id']: p for p in self.data['paragraphs']}
        units = {unit['unit_id']: unit for unit in candidates}

        def validate(value):
            for row in rows(value, 'units', 'unit_id', units):
                paragraph = paragraphs[units[row['unit_id']]['paragraph_id']]
                if row['eligibility'] not in ELIGIBILITY or not set(row['cited_ids']) <= set(paragraph['cited_ids']):
                    raise ValueError('Invalid citation eligibility or paragraph source binding')
                if row['eligibility'] == 'literature_uncited' and row['cited_ids']:
                    raise ValueError('An uncited statement cannot contain bound citation IDs')

        result = self.call('citation-eligibility-all', 'citation_eligibility', {
            'candidates': candidates,
            'original_paragraphs': [paragraphs[p] for p in dict.fromkeys(u['paragraph_id'] for u in candidates)],
            'research_sources': self.research_sources()}, validate)
        eligible = [{**units[row['unit_id']], 'cited_ids': row['cited_ids'],
                     'citation_binding': row['eligibility'], 'binding_reason_zh': row.get('reason_zh', '')}
                    for row in result['units'] if not row['eligibility'].startswith('exclude_')]
        return eligible, result

    def verify(self, extracted, ledger, metrics):
        papers = {p['id']: p for p in self.data['literature']}
        jobs = {}

        def sources_for(items):
            ids = sorted({key for item in items for key in item['cited_ids']})
            return [papers[key] for key in ids if key in papers]

        for metric, key, width, prefix, task, labels, label_key in [
                ('factscore', 'facts', 30, 'fact-verify-', 'verify_facts', FACT_LABELS, 'verdict'),
                ('alce', 'citation_units', 12, 'citation-verify-', 'verify_citations',
                 CITATION_LABELS, 'joint_verdict')]:
            if metric not in metrics:
                continue
            for start in range(0, len(extracted[key]), width):
                batch = extracted[key][start:start + width]
                id_key, output_key = ('fact_id', 'facts') if key == 'facts' else ('unit_id', 'units')
                payload = ({'facts': batch, 'research_sources': self.research_sources(), 'input_ledger': ledger,
                            'cited_literature': sources_for(batch)} if key == 'facts'
                           else {'units': batch, 'sources': sources_for(batch)})

                name = prefix + str(start // width + 1).zfill(2)

                def job(batch=batch, id_key=id_key, output_key=output_key, payload=payload, task=task,
                        labels=labels, label_key=label_key, name=name):
                    def validate(value, output_key=output_key, id_key=id_key, batch=batch,
                                 labels=labels, label_key=label_key):
                        by_id = {item[id_key]: item for item in batch}
                        for row in rows(value, output_key, id_key, [item[id_key] for item in batch]):
                            if row[label_key] not in labels:
                                raise ValueError('Unknown verification label')
                            if output_key == 'units':
                                useful = set(row['individually_sufficient_ids']) | set(row['necessary_source_ids'])
                                irrelevant = set(row['irrelevant_source_ids'])
                                cited = set(by_id[row[id_key]]['cited_ids'])
                                if not (useful | irrelevant) <= cited or useful & irrelevant:
                                    raise ValueError('Citation usefulness has unbound or contradictory source IDs')
                    return self.call(name, task, payload, validate)
                jobs[prefix + str(start // width + 1).zfill(2)] = job
        values = self.parallel(jobs)
        result = {}
        if 'factscore' in metrics:
            result['facts'] = [row for name, value in sorted(values.items()) if name.startswith('fact-')
                               for row in value['facts']]
        if 'alce' in metrics:
            result['citation_units'] = [row for name, value in sorted(values.items()) if name.startswith('citation-')
                                        for row in value['units']]
        save(self.calls.out / 'verified.json', result)
        return result

    def audit(self, extracted, verified):
        papers = {paper['id']: paper.get('text', paper.get('abs', '')) for paper in self.data['literature']}
        sources = {**self.research_sources(), **papers}
        sources = {key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                   for key, value in sources.items()}
        facts = {fact['fact_id']: fact for fact in extracted['facts']}
        warnings = []
        for key, id_key in [('facts', 'fact_id'), ('citation_units', 'unit_id')]:
            units = facts if key == 'facts' else {u['unit_id']: u for u in extracted['citation_units']}
            for row in verified.get(key, []):
                unit = units[row[id_key]]
                allowed = set(unit['cited_ids'])
                if key == 'facts' and unit.get('scope') != 'literature':
                    allowed |= set(sources) - set(papers)
                for evidence in row.get('evidence', []):
                    source_id, quote = evidence.get('source_id'), evidence.get('quote')
                    if source_id not in allowed or not quote or quote not in sources.get(source_id, ''):
                        warnings.append({'id': row[id_key], 'evidence': evidence,
                                         'reason': 'Evidence is not an exact quote from an allowed supplied source'})
        save(self.calls.out / 'evidence-anchor-audit.json', {'warnings': warnings,
             'policy': 'Raw model labels retained; quote warnings are not silently repaired or hidden.'})
        return {'exact_quote_warnings': len(warnings), 'automatic_labels_are_not_human_confirmed': True}


def evaluate(args):
    data, specification = read(args.input), read(PROMPTS)
    if args.manuscript:
        data['manuscript'] = args.manuscript.read_text(encoding='utf-8-sig')
    if not data.get('manuscript') or not data.get('task') or not isinstance(data.get('sources'), dict):
        raise ValueError('Input requires a complete manuscript, assignment task and supplied sources')
    data.update(structure(data['manuscript'], data.get('reference')))
    data.setdefault('literature', [])
    identities = [paper['id'] for paper in data['literature']]
    if len(identities) != len(set(identities)):
        raise ValueError('Literature source IDs must identify one supplied record each')
    metrics = sorted(set(args.metrics))
    bundle = read(args.rubric) if args.rubric else None
    binding = {'input_sha256': fingerprint(data),
               'input_file_sha256': hashlib.sha256(args.input.read_bytes()).hexdigest(),
               'rubric_file_sha256': hashlib.sha256(args.rubric.read_bytes()).hexdigest() if args.rubric else None,
               'specification_sha256': fingerprint(specification),
               'rubric_sha256': fingerprint(bundle), 'metrics': metrics, 'official_benchmark': False,
               'requested_model': args.model, 'requested_model_id': args.model_id,
               'base_url': (args.base_url or os.environ.get('LAZYMIND_BASE_URL', 'http://127.0.0.1:8091')).rstrip('/')}
    api = None

    def factory():
        nonlocal api
        if api is None:
            api = Client(args.base_url, args.auth_file, persist_session=True)
        return api

    if args.resume:
        config = read(args.out / 'run.json')
        if config['binding'] != binding:
            raise ValueError('Resume requires the same materials, prompts, rubric, metrics, model and service')
    else:
        args.out.mkdir(parents=True, exist_ok=False)
        config = {'binding': binding}
    status = {'status': 'running', **binding, 'started_at_unix': time.time()}
    save(args.out / 'status.json', status)
    (args.out / 'score.json').unlink(missing_ok=True)
    try:
        if 'model' not in config:
            config['model'] = factory().model(args.model, args.model_id, args.out)
        save(args.out / 'run.json', config)
        save(args.out / 'input.json', data)
        save(args.out / 'specification.json', specification)
        calls = Calls(args.out, config['model'], factory, args.offline, args.jobs,
                      specification['prompts']['system'], parse)
        evaluator = Evaluator(data, calls, specification, args.jobs)
        rubric, ledger = evaluator.initial(metrics, bundle)
        result = {'official_benchmark': False, 'adaptation_version': specification['version'],
                  'judge': config['model'], 'method_sources': specification['methods'], 'posthoc_adjudication': False}
        if 'writingbench' in metrics:
            result['writingbench_inspired'] = evaluator.writing(rubric, ledger)
        if set(metrics) & {'factscore', 'alce'}:
            extracted = evaluator.extract()
            if 'alce' in metrics:
                eligible, eligibility = evaluator.eligible(extracted)
                save(args.out / 'citation-eligibility.json', eligibility)
                extracted = {**extracted, 'citation_units': eligible}
                save(args.out / 'extracted-final.json', extracted)
            verified = evaluator.verify(extracted, ledger, metrics)
            result.update(aggregate(extracted, verified))
            result['extraction'] = {'paragraphs': extracted['paragraph_coverage'], 'facts': len(extracted['facts']),
                                    'citation_units': len(extracted['citation_units']),
                                    'invalid_quotes_excluded': len(extracted['invalid_extraction_quotes'])}
            result['evidence_audit'] = evaluator.audit(extracted, verified)
        save(args.out / 'score.json', result)
        report = ('Material-based adapters; NOT official benchmark scores.\n'
                  'Model judgments against supplied evidence; no acceptance verdict.\n'
                  + json.dumps(result, ensure_ascii=False, indent=2))
        (args.out / 'report.txt').write_text(report, encoding='utf-8')
        status['status'] = 'succeeded'
        return result
    except Exception as error:
        status.update(status='failed', error_type=type(error).__name__, error=str(error))
        raise
    finally:
        status['elapsed_seconds'] = round(time.time() - status['started_at_unix'], 3)
        save(args.out / 'status.json', status)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('manuscript', type=Path, help='Complete Markdown including bibliography')
    prep.add_argument('--assignment', required=True, type=Path, help='Original writing assignment as text')
    prep.add_argument('--source', action='append', default=[], type=Path, help='Repeat for each supplied research file')
    prep.add_argument('--literature', type=Path, help='Cited abstracts or LazyMind current material pool')
    prep.add_argument('--references', type=Path, help='Optional numeric-label to literature-ID map')
    prep.add_argument('--provenance', type=Path, help='Optional actual workflow execution record; excluded from rubric')
    prep.add_argument('--out', required=True, type=Path)
    run = sub.add_parser('run')
    run.add_argument('input', type=Path)
    run.add_argument('--manuscript', type=Path, help='Supply complete manuscript for an existing evidence packet')
    run.add_argument('--metrics', nargs='+', choices=['writingbench', 'alce', 'factscore'],
                     default=['writingbench', 'alce', 'factscore'])
    run.add_argument('--rubric', type=Path, help='Reuse a frozen rubric bundle for the same assignment and sources')
    run.add_argument('--model', default='gpt-6-luna')
    run.add_argument('--model-id')
    run.add_argument('--base-url')
    run.add_argument('--auth-file', type=Path)
    run.add_argument('--jobs', type=int, default=3)
    run.add_argument('--out', required=True, type=Path)
    run.add_argument('--resume', action='store_true')
    run.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    if args.command == 'prepare':
        args.out.parent.mkdir(parents=True, exist_ok=True)
        save(args.out, prepare(args))
    else:
        if args.jobs < 1 or args.offline and not args.resume:
            parser.error('jobs must be positive; offline replay requires --resume')
        print(json.dumps(evaluate(args), ensure_ascii=False))


if __name__ == '__main__':
    main()
