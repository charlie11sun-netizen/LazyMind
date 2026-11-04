"""Core adapter regressions; no network, provider credentials or benchmark fixtures."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
import threading
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from examples.paperwritingbench import autosurvey, client, fetch_upstream


def test_prepare_keeps_adjacent_citations_caption_and_selected_scope(tmp_path):
    path = tmp_path / 'paper.md'
    path.write_text('Preface\n## Introduction\n<a id="intro"></a>\n'
                    'Evidence [\\[1\\]](#paper-ref-A)[\\[2\\]](#paper-ref-B). '
                    'See [overview](#fig).\n![Figure 1. Supplied caption.](figure.png)\n'
                    '## Method\nExcluded body.', encoding='utf-8')
    value = autosurvey.prepare(path, 'Topic', '## Introduction', '## Method')
    assert value['reference'] == {'1': 'A', '2': 'B'}
    assert 'Evidence [1][2]. See overview.' in value['survey']
    assert 'Figure 1. Supplied caption.' in value['survey']
    assert 'Excluded' not in value['survey'] and 'Preface' not in value['survey']
    assert value['citation_sequence_verified'] is True
    assert value['source_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_prepare_latex_preserves_bibliography_order_through_pandoc(tmp_path, monkeypatch):
    path = tmp_path / 'paper.tex'
    path.write_text('\\section{Introduction}\nResult\\cite{second,first}.\n'
                    '\\section{Method}\nExcluded.\n\\bibitem{first}\n\\bibitem{second}', encoding='utf-8')

    def pandoc(command, **kwargs):
        assert command[0] == 'pandoc'
        assert 'ResultAUTOSURVEYCITATIONTOKEN000000.' in kwargs['input']
        assert 'Excluded' not in kwargs['input']
        return SimpleNamespace(stdout='# Introduction\nResult AUTOSURVEYCITATIONTOKEN000000.')

    monkeypatch.setattr(autosurvey.subprocess, 'run', pandoc)
    value = autosurvey.prepare(path, 'Topic', r'\section{Introduction}', r'\section{Method}')
    assert value['reference'] == {'2': 'second', '1': 'first'}
    assert value['survey'] == '## Introduction\nResult [2;1].'
    assert value['citation_sequence_verified'] is True


def test_concurrent_refresh_persists_once_and_exact_cache_replays_offline(tmp_path, monkeypatch):
    for name in ['LAZYMIND_ACCESS_TOKEN', 'LAZYMIND_REFRESH_TOKEN']:
        monkeypatch.delenv(name, raising=False)
    auth = tmp_path / 'owner.json'
    auth.write_text(json.dumps({'access_token': 'old-session', 'refresh_token': 'old-refresh'}))
    records = tmp_path / 'records'
    records.mkdir()
    expired = threading.Barrier(3)
    refreshes = []

    def send(request, **kwargs):
        if request.full_url.endswith('/refresh'):
            refreshes.append(json.loads(request.data))
            return io.BytesIO(b'{"data":{"access_token":"new-session","refresh_token":"new-refresh"}}')
        if request.get_header('Authorization') == 'Bearer old-session':
            expired.wait(timeout=5)
            raise HTTPError(request.full_url, 401, 'expired', {}, None)
        assert request.get_header('Authorization') == 'Bearer new-session'
        return io.BytesIO(b'{"result":{"structuredContent":{"content":"Yes","finish_reason":"stop"}}}')

    monkeypatch.setattr(client.urllib.request, 'urlopen', send)
    api = client.Client('http://localhost:8091', auth, persist_session=True)
    calls = autosurvey.Calls(records, {'id': 'judge-id', 'name': 'judge'}, lambda: api, jobs=3)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(calls.chat, 'claim ' + str(i), 0, '__nli', i) for i in range(3)]
        assert [future.result(timeout=10) for future in futures] == ['Yes'] * 3
    assert refreshes == [{'refresh_token': 'old-refresh'}]
    assert autosurvey.read(auth)['data']['refresh_token'] == 'new-refresh'
    if os.name != 'nt':
        assert auth.stat().st_mode & 0o777 == 0o600
    retained = ''.join(p.read_text() for p in records.rglob('*.json'))
    assert all(secret not in retained for secret in ['old-session', 'old-refresh', 'new-session', 'new-refresh'])
    monkeypatch.setattr(client.urllib.request, 'urlopen', lambda *_a, **_kw: pytest.fail('Unexpected network call'))
    replay = autosurvey.Calls(records, calls.model, lambda: pytest.fail('Unexpected authentication'), offline=True)
    assert replay.chat('claim 0', 0, '__nli', 0) == 'Yes'
    with pytest.raises(ValueError, match='missing a completion'):
        replay.chat('unseen claim', 0, '__nli', 9)


def test_loader_verifies_pin_and_surfaces_hidden_upstream_worker_errors(tmp_path, monkeypatch):
    # A dependency fixture exercises AST selection and exception plumbing, not scoring arithmetic.
    files = {
        'src/prompt.py': 'CRITERIA_BASED_JUDGING_PROMPT = "criterion"\nNLI_PROMPT = "support"\n',
        'src/agents/judge.py': 'raise AssertionError("do not execute module initialization")\n'
                               'CRITERIA = {}\n'
                               'class Judge:\n'
                               '    def __init__(self, *args):\n'
                               '        thread = threading.Thread(target=self.fail)\n'
                               '        thread.start()\n'
                               '        thread.join()\n'
                               '    def fail(self):\n'
                               '        raise ValueError("worker interrupted")\n',
    }
    manifest = {'repository': 'fixture', 'commit': 'fixture', 'files': []}
    for name, source in files.items():
        path = tmp_path / 'upstream/autosurvey' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')
        manifest['files'].append({'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    autosurvey.save(tmp_path / 'autosurvey-manifest.json', manifest)
    monkeypatch.setattr(fetch_upstream, 'ROOT', tmp_path)
    calls = autosurvey.Calls(tmp_path, {'id': 'id', 'name': 'judge'}, lambda: None, offline=True)
    autosurvey.load_judge(calls, {})
    with pytest.raises(RuntimeError, match='worker interrupted'):
        calls.check()
    assert 'worker interrupted' in autosurvey.read(tmp_path / 'worker-errors.json')[0]
    (tmp_path / 'upstream/autosurvey/src/prompt.py').write_text('tampered', encoding='utf-8')
    with pytest.raises(ValueError, match='Restore pinned raters'):
        autosurvey.load_judge(calls, {})


def test_incomplete_completion_is_preserved_as_failure_not_cached(tmp_path):
    class Incomplete:
        def call_tool(self, *_args):
            return {'content': 'Partial answer', 'finish_reason': 'length'}

    calls = autosurvey.Calls(tmp_path, {'id': 'id', 'name': 'judge'}, Incomplete)
    with pytest.raises(ValueError, match='did not finish normally'):
        calls.chat('original prompt', 0, '__nli', 0)
    assert not list(tmp_path.rglob('completion.json'))
    status = autosurvey.read(next(tmp_path.rglob('attempt-01-status.json')))
    assert status['status'] == 'failed'


def test_evaluation_resumes_only_missing_calls_and_rejects_changed_input(tmp_path, monkeypatch):
    monkeypatch.setenv('LAZYMIND_ACCESS_TOKEN', 'fixture-session')
    path, out = tmp_path / 'input.json', tmp_path / 'evaluation'
    autosurvey.save(path, {'topic': 'Topic', 'survey': 'Selected survey.', 'reference': {}})
    scores, seen, interrupted = {'Coverage': 4, 'Structure': 3, 'Relevance': 5}, [], [True]

    def send(request, **kwargs):
        payload = json.loads(request.data)['params']
        if payload['name'] == 'model.list':
            result = {'items': [{'id': 'id', 'name': 'gpt-6-luna'}]}
        else:
            axis = payload['arguments']['messages'][0]['content']
            seen.append(axis)
            if axis == 'Structure' and interrupted[0]:
                raise HTTPError(request.full_url, 503, 'interrupted', {}, None)
            result = {'content': str(scores[axis]), 'finish_reason': 'stop'}
        return io.BytesIO(json.dumps({'result': {'structuredContent': result}}).encode())

    # Replace the external rater dependency, keeping the real CLI, client, caching and status handling.
    def rater(calls, papers):
        def batch(survey, topic, criteria):
            assert survey == 'Selected survey.' and topic == 'Topic' and papers == {}
            return [int(calls.chat(axis, 0, '__criteria_based_judging', i)) for i, axis in enumerate(criteria)]
        return SimpleNamespace(batch_criteria_based_judging=batch)

    monkeypatch.setattr(client.urllib.request, 'urlopen', send)
    monkeypatch.setattr(autosurvey, 'load_judge', rater)
    args = SimpleNamespace(input=path, out=out, abstracts=None, phase='content', model='gpt-6-luna',
                           model_id=None, base_url=None, auth_file=None, resume=False, offline=False, jobs=2)
    with pytest.raises(HTTPError):
        autosurvey.evaluate(args)
    assert autosurvey.read(out / 'status.json')['status'] == 'failed'
    assert not (out / 'score.json').exists()
    interrupted[0], args.resume = False, True
    result = autosurvey.evaluate(args)
    assert result['axes'] == scores and result['mean'] == 4
    assert seen == ['Coverage', 'Structure', 'Structure', 'Relevance']
    args.offline = True
    monkeypatch.setattr(client.urllib.request, 'urlopen', lambda *_a, **_kw: pytest.fail('Unexpected network call'))
    assert autosurvey.evaluate(args)['axes'] == scores
    autosurvey.save(path, {'topic': 'Changed topic', 'survey': 'Selected survey.', 'reference': {}})
    with pytest.raises(ValueError, match='Resume requires the same'):
        autosurvey.evaluate(args)
    assert autosurvey.read(out / 'score.json')['axes'] == scores
