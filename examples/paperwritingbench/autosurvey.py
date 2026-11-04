"""Pinned AutoSurvey author rules through LazyMind; private records and exact-prompt resume."""
import argparse
import ast
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import types

from .client import Client
from .fetch_upstream import settings, verify

AXES = ['Coverage', 'Structure', 'Relevance']


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalize_markdown(text):
    text = re.sub(r'(?m)^<a [^>]+></a>\s*$', '', text)
    text = re.sub(r'\[\\\[(\d+)\\\]\]\(#paper-ref-[^)]+\)', r'[\1]', text)
    text = re.sub(r'!\[([^\[\]\n]*)\]\([^\n)]+\)', r'\1', text)
    # Excluding brackets prevents a following ordinary link from eating [1][2].
    text = re.sub(r'\[([^\[\]\n]*)\]\([^\n)]+\)', r'\1', text)
    text = re.sub(r'\[([^\[\]\n]*)\]\{reference-type="ref" reference="[^"]+"\}', r'\1', text)
    text = re.sub(r'(?m)^(#+ [^\n]*?)\s*\{#[^}]+\}\s*$', r'\1', text)
    return text.strip()


def prepare(path, topic, start=None, end=None, references=None):
    source = path.read_text(encoding='utf-8-sig')
    selected = source[source.index(start):] if start else source
    if end:
        selected = selected[:selected.index(end)]
    if not selected.strip():
        raise ValueError('Selected survey is empty')
    if path.suffix.lower() == '.tex':
        keys = re.findall(r'\\bibitem\{([^}]+)\}', source)
        numbers = {key: str(i + 1) for i, key in enumerate(keys)}
        replacements, refs, original_numbers = {}, {}, []

        def citation(match):
            cited = [key.strip() for key in match[1].split(',')]
            for key in cited:
                refs[numbers[key]] = key
                original_numbers.append(numbers[key])
            token = 'AUTOSURVEYCITATIONTOKEN' + str(len(replacements)).zfill(6)
            if token in source:
                raise ValueError('Source collides with citation placeholder')
            replacements[token] = '[' + ';'.join(numbers[key] for key in cited) + ']'
            return token

        selected = re.sub(r'\\cite\{([^}]+)\}', citation, selected)
        selected = subprocess.run(['pandoc', '--from=latex', '--to=markdown', '--wrap=none'],
                                  input=selected, text=True, encoding='utf-8', capture_output=True,
                                  check=True).stdout
        for token, label in replacements.items():
            if selected.count(token) != 1:
                raise ValueError('Pandoc did not preserve a citation placeholder')
            selected = selected.replace(token, label)
        selected = re.sub(r'(?m)^#### ', '### ', selected)
        selected = re.sub(r'(?m)^# ', '## ', selected)
        survey = normalize_markdown(selected)
        actual_numbers = [n for group in re.findall(r'\[([\d;]+)\]', survey) for n in group.split(';')]
    else:
        pairs = re.findall(r'\[\\\[(\d+)\\\]\]\(#paper-ref-([^)]+)\)', selected)
        refs = {}
        for number, identifier in pairs:
            if number in refs and refs[number] != identifier:
                raise ValueError('One citation number maps to different papers: ' + number)
            refs[number] = identifier
        survey = normalize_markdown(selected)
        original_numbers = [number for number, _identifier in pairs]
        actual_numbers = re.findall(r'\[(\d+)\]', survey) if pairs else []
    if original_numbers != actual_numbers:
        raise ValueError('Ordered citation sequence changed during conversion')
    if references is not None:
        refs = references
    return {'topic': topic, 'survey': survey, 'reference': refs,
            'scope': {'start': start, 'end': end}, 'source': str(path.resolve()),
            'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'citation_sequence_verified': bool(original_numbers)}


def abstracts(path):
    value = read(path)
    if isinstance(value, dict):
        value = value.get('data', value)
    if isinstance(value, dict) and 'materials' in value:
        current = value['current']['papers']
        value = next(item['content'] for item in value['materials']
                     if item['id'] == current['id'] and item['version'] == current['version'])
    elif isinstance(value, dict) and 'papers' in value:
        value = value['papers']
    if isinstance(value, list):
        value = {paper['id']: paper for paper in value}
    return {key: {**paper, 'id': key, 'abs': paper.get('abs') or paper.get('text', '')}
            for key, paper in value.items()}


class Calls:
    def __init__(self, out, model, client_factory, offline=False, jobs=4):
        self.out, self.model, self.client_factory, self.offline = out, model, client_factory, offline
        self.client = None
        self.gate = threading.BoundedSemaphore(jobs)
        self.lock, self.key_locks, self.errors = threading.Lock(), {}, []

    def chat(self, prompt, temperature, phase, idx):
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        folder = self.out / 'calls' / f'{phase}-{idx}-{digest[:20]}'
        with self.lock:
            key_lock = self.key_locks.setdefault(folder.name, threading.Lock())
        with key_lock:
            folder.mkdir(parents=True, exist_ok=True)
            prompt_path, result_path = folder / 'upstream-prompt.json', folder / 'completion.json'
            if prompt_path.exists() and read(prompt_path)['prompt'] != prompt:
                raise ValueError('Cached prompt differs from requested prompt')
            if result_path.exists():
                if not prompt_path.exists():
                    raise ValueError('Cached completion has no original prompt')
                result = read(result_path)
            else:
                if self.offline:
                    raise ValueError('Offline replay is missing a completion: ' + folder.name)
                save(prompt_path, {'prompt': prompt, 'sha256': digest,
                                   'upstream_temperature_argument': temperature,
                                   'generation_policy': 'provider_defaults'})
                with self.lock:
                    if self.client is None:
                        self.client = self.client_factory()
                attempt = len(list(folder.glob('attempt-*-request.json'))) + 1
                record = folder / f'attempt-{attempt:02}'
                started = time.time()
                event = {'status': 'interrupted'}
                try:
                    with self.gate:
                        result = self.client.call_tool('model.chat', {
                            'model_id': self.model['id'], 'messages': [{'role': 'user', 'content': prompt}]}, record)
                    self.validate(result)
                    save(result_path, result)
                    event = {'status': 'succeeded', 'usage': result.get('usage')}
                except Exception as error:
                    event = {'status': 'failed', 'error_type': type(error).__name__, 'error': str(error)}
                    raise
                finally:
                    save(folder / f'attempt-{attempt:02}-status.json',
                         {**event, 'seconds': round(time.time() - started, 3)})
            self.validate(result)
            return result['content']

    @staticmethod
    def validate(result):
        if result.get('finish_reason') != 'stop' or not isinstance(result.get('content'), str):
            raise ValueError('Judge completion did not finish normally')
        if not result['content'].strip():
            raise ValueError('Judge returned an empty completion')

    def check(self):
        if self.errors:
            save(self.out / 'worker-errors.json', self.errors)
            raise RuntimeError('A judge worker failed; no final score accepted: ' + self.errors[0])


def load_judge(calls, papers):
    import numpy as np

    verify('autosurvey')
    _manifest, upstream = settings('autosurvey')
    scope = {'np': np, 're': re, 'tqdm': lambda values: values}

    class APIModel:
        def __init__(self, *_args):
            pass

        def chat(self, prompt, temperature=1):
            frame = inspect.currentframe().f_back
            return calls.chat(prompt, temperature, frame.f_code.co_name, frame.f_locals.get('idx', 'na'))

    class Worker(threading.Thread):
        def run(self):
            try:
                super().run()
            except Exception as error:
                with calls.lock:
                    calls.errors.append(f'{type(error).__name__}: {error}')

    class Database:
        def get_paper_info_from_ids(self, ids):
            return [papers[identifier] for identifier in ids]

    class TokenCounter:
        def num_tokens_from_string(self, _text):
            return 0  # Bookkeeping only; actual provider usage is saved with each response.

    scope.update(APIModel=APIModel, tokenCounter=TokenCounter, threading=types.SimpleNamespace(Thread=Worker))
    for filename, names in [('src/prompt.py', {'CRITERIA_BASED_JUDGING_PROMPT', 'NLI_PROMPT'}),
                            ('src/agents/judge.py', {'CRITERIA', 'Judge'})]:
        path = upstream / filename
        nodes = []
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if isinstance(node, ast.ClassDef) and node.name in names:
                nodes.append(node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in names:
                    nodes.append(node)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), scope)
    return scope['Judge'](calls.model['name'], '', '', Database())


def cited_claims(survey, judge):
    # Mirror only the extraction for preflight/audit; the original Judge computes the metrics.
    result = []
    for section in survey.split('## References')[0].split('###'):
        for sentence in re.findall(r'[^.!?]*\[[^\]]+\][^.!?]*[.!?]', section):
            indices = set()
            for group in re.findall(r'\[(.*?)\]', sentence):
                for token in group.split(';'):
                    number = judge.extract_num(token)
                    if number != '':
                        indices.add(number)
            if indices:
                result.append({'claim_with_citations': sentence, 'indices': sorted(indices)})
    return result


def evaluate(args):
    inp = read(args.input)
    if not all(isinstance(inp.get(key), str) and inp[key].strip() for key in ['topic', 'survey']):
        raise ValueError('Input requires nonempty topic and survey strings')
    refs = inp.get('reference', {})
    papers = abstracts(args.abstracts) if args.abstracts else {}
    papers = {identifier: papers[identifier] for identifier in set(refs.values())} if args.phase != 'content' else {}
    if args.phase != 'content':
        if not refs or not all(paper.get('title') and paper.get('abs', '').strip() for paper in papers.values()):
            raise ValueError('Citation scoring requires a real title and abstract for every reference')
    manifest, _directory = settings('autosurvey')
    binding = {'input_sha256': fingerprint(inp), 'abstracts_sha256': fingerprint(papers),
               'upstream_commit': manifest['commit'], 'phase': args.phase,
               'requested_model': args.model, 'requested_model_id': args.model_id,
               'base_url': (args.base_url or os.environ.get('LAZYMIND_BASE_URL', 'http://127.0.0.1:8091')).rstrip('/'),
               'generation_policy': 'provider_defaults'}
    existing = args.out / 'run.json'
    api = None

    def factory():
        nonlocal api
        if api is None:
            api = Client(args.base_url, args.auth_file, persist_session=True)
        return api
    if args.resume:
        config = read(existing)
        if config['binding'] != binding:
            raise ValueError('Resume requires the same input, abstracts, phase, model, service and upstream version')
    else:
        args.out.mkdir(parents=True, exist_ok=False)
        config = {'binding': binding}
    status = {'status': 'running', **binding, 'started_at_unix': time.time()}
    save(args.out / 'status.json', status)
    (args.out / 'score.json').unlink(missing_ok=True)
    try:
        if 'model' not in config:
            config['model'] = factory().model(args.model, args.model_id, args.out)
        save(existing, config)
        save(args.out / 'input.json', inp)
        save(args.out / 'abstracts.json', {'papers': papers})
        (args.out / 'survey.md').write_text(inp['survey'], encoding='utf-8')
        save(args.out / 'upstream-manifest.json', manifest)
        calls = Calls(args.out, config['model'], factory, args.offline, args.jobs)
        judge = load_judge(calls, papers)
        result = {'judge': config['model'], 'upstream_commit': manifest['commit'],
                  'scope': inp.get('scope', 'Provided survey text; not an official benchmark dataset run')}
        if args.phase != 'content':
            claims = cited_claims(inp['survey'], judge)
            if not claims or any(str(i) not in refs for claim in claims for i in claim['indices']):
                raise ValueError('Citation scoring requires cited sentences and mapped numeric references')
            save(args.out / 'citation-claims.json', {'claims': claims})
        if args.phase != 'citations':
            scores = judge.batch_criteria_based_judging(inp['survey'], inp['topic'], AXES)
            calls.check()
            if not all(isinstance(score, int) and 1 <= score <= 5 for score in scores) or len(scores) != len(AXES):
                raise ValueError('Content scores must be three integers from 1 to 5')
            result.update(axes=dict(zip(AXES, scores)), mean=sum(scores) / len(scores))
            save(args.out / 'content-score.json', {'axes': result['axes'], 'mean': result['mean']})
        if args.phase != 'content':
            recall, precision = judge.citation_quality(inp['survey'], refs)
            calls.check()
            if not all(0 <= score <= 1 for score in [recall, precision]):
                raise ValueError('Citation metrics must be finite numbers from 0 to 1')
            result.update(citation_recall=float(recall), citation_precision=float(precision),
                          cited_sentences=len(claims), citation_associations=sum(len(c['indices']) for c in claims))
        save(args.out / 'score.json', result)
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
    prep = sub.add_parser('prepare', help='Select and mechanically normalize Markdown or LaTeX survey text')
    prep.add_argument('manuscript', type=Path)
    prep.add_argument('--topic', required=True)
    prep.add_argument('--start', help='Exact heading marking the beginning of the selected text')
    prep.add_argument('--end', help='Exact heading excluded from the selected text')
    prep.add_argument('--references', type=Path, help='Optional JSON mapping numeric labels to abstract IDs')
    prep.add_argument('--out', required=True, type=Path)
    run = sub.add_parser('run', help='Evaluate using the unmodified author Judge and rubric')
    run.add_argument('input', type=Path)
    run.add_argument('--abstracts', type=Path, help='Abstract map or retained LazyMind material pool')
    run.add_argument('--phase', choices=['all', 'content', 'citations'], default='all')
    run.add_argument('--out', required=True, type=Path)
    run.add_argument('--model', default='gpt-6-luna')
    run.add_argument('--model-id')
    run.add_argument('--base-url')
    run.add_argument('--auth-file', type=Path)
    run.add_argument('--jobs', type=int, default=4)
    run.add_argument('--resume', action='store_true')
    run.add_argument('--offline', action='store_true', help='With --resume, require existing exact-prompt responses')
    args = parser.parse_args()
    if args.command == 'prepare':
        value = prepare(args.manuscript, args.topic, args.start, args.end,
                        read(args.references) if args.references else None)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        save(args.out, value)
    else:
        if args.jobs < 1 or args.offline and not args.resume:
            parser.error('jobs must be positive; offline replay requires --resume')
        print(json.dumps(evaluate(args), ensure_ascii=False))


if __name__ == '__main__':
    main()
