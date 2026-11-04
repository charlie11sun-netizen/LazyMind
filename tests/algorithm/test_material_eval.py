"""Core material-adapter flows with controlled model responses and no external services."""
from collections import Counter
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from examples.paperwritingbench import autosurvey, client, material_eval as ev
from examples.paperwritingbench.material_prepare import prepare, structure


def test_preparation_keeps_full_bibliography_and_extracts_appendix_prose(tmp_path):
    manuscript = tmp_path / 'paper.md'
    manuscript.write_text('# Title\n## Introduction\nA result [1].\n## References\n[1] Bibliographic entry.\n'
                          '## Appendix\nAn appendix claim.', encoding='utf-8')
    assignment, source, refs = tmp_path / 'assignment.txt', tmp_path / 'log.md', tmp_path / 'refs.json'
    assignment.write_text('Write from supplied evidence.', encoding='utf-8')
    source.write_text('A result.', encoding='utf-8')
    autosurvey.save(refs, {'1': 'L1'})
    args = SimpleNamespace(manuscript=manuscript, assignment=assignment, source=[source], literature=None,
                           references=refs, provenance=None)
    value = prepare(args)
    assert 'Bibliographic entry.' in value['manuscript']
    assert 'Bibliographic entry.' not in value['body']
    assert [p['text'] for p in value['paragraphs']] == ['A result [1].', 'An appendix claim.']
    assert value['paragraphs'][0]['cited_ids'] == ['L1']
    assert value['reference'] == {'1': 'L1'}
    assert structure(value['manuscript'], value['reference'])['paragraphs'] == value['paragraphs']
    assert value['sources'] == {'log.md': 'A result.'}
    top_level = structure('# Introduction\nA.\n## Detail\nB.\n# Methods\nC.')
    assert [s['title'] for s in top_level['sections']] == ['Introduction', 'Methods']
    native = structure('## Introduction\nA result.\n<a id="paper-references-start"></a>\n'
                       '**参考文献**\nA bibliographic entry.\n<a id="paper-references-end"></a>\n'
                       '## Appendix\nAn appendix claim.')
    assert [p['text'] for p in native['paragraphs']] == ['A result.', 'An appendix claim.']


def fixture_input():
    return {'task': {'request': 'Write a paper from provided evidence.'},
            'sources': {'log.md': 'Provided accuracy is 0.8.'},
            'literature': [{'id': 'L1', 'title': 'A prior study', 'text': 'A prior finding.'}],
            'manuscript': '## Introduction\nProvided accuracy is 0.8. '
                          'A prior finding [\\[1\\]](#paper-ref-L1). Another external claim needs a reference.\n'
                          '## References\n[1] A prior study.'}


@pytest.mark.parametrize('interrupted', [False, True])
def test_all_adapters_resume_and_reuse_candidate_blind_rubric(tmp_path, monkeypatch, interrupted):
    monkeypatch.setenv('LAZYMIND_ACCESS_TOKEN', 'fixture-session')
    specification = autosurvey.read(ev.PROMPTS)
    tasks = {text: name for name, text in specification['prompts'].items()}
    path, out, seen = tmp_path / 'input.json', tmp_path / 'run', []
    packet = fixture_input()
    if interrupted:
        packet['manuscript'] = packet['manuscript'].replace('[\\[1\\]](#paper-ref-L1)', '[1]')
        packet['reference'] = {'1': 'L1'}
    autosurvey.save(path, packet)
    fail = [interrupted]

    def judge(task, payload):
        seen.append(task)
        if task in {'rubric', 'ledger'}:
            assert set(payload) == {'task', 'research_sources'}
            assert 'manuscript' not in payload
        if task == 'rubric':
            return {'criteria': [{'id': 'C' + str(i), 'name_zh': 'Criterion', 'description': 'Description',
                                  'anchors': {key: 'Anchor' for key in ['1-2', '3-4', '5-6', '7-8', '9-10']}}
                                 for i in range(1, 6)]}
        if task == 'ledger':
            return {'evidence_groups': [{'id': 'E1'}], 'input_gaps': [], 'input_conflicts': []}
        if task == 'writing':
            assert '## References\n[1] A prior study.' in payload['manuscript']
            return {'criterion_id': payload['criterion']['id'], 'score': 8, 'reason_zh': 'Clear.',
                    'strengths_zh': [], 'writer_issues': [], 'input_limitations_not_charged': []}
        if task == 'coverage':
            return {'groups': [{'id': 'E1', 'status': 'covered'}], 'input_conflict_handling': []}
        if task == 'extract':
            assert len(payload['paragraphs']) == 1
            paragraph = payload['paragraphs'][0]
            return {'paragraphs': [{'paragraph_id': paragraph['id'], 'facts': [
                {'statement': 'Provided accuracy is 0.8.', 'quote': 'Provided accuracy is 0.8.',
                 'scope': 'own_research', 'numerical': True},
                {'statement': 'A prior finding.', 'quote': 'A prior finding',
                 'scope': 'literature', 'numerical': False}],
                'citation_units': [{'claim': 'A prior finding.', 'quote': 'A prior finding',
                                    'cited_ids': ['L1']},
                                   {'claim': 'Another external claim needs a reference.',
                                    'quote': 'Another external claim needs a reference.', 'cited_ids': []}]}]}
        if task == 'citation_eligibility':
            return {'units': [{'unit_id': item['unit_id'], 'eligibility': 'literature_explicit'
                               if item['cited_ids'] else 'literature_uncited', 'cited_ids': item['cited_ids']}
                              for item in payload['candidates']]}
        if task == 'verify_facts':
            return {'facts': [{'fact_id': item['fact_id'], 'verdict': 'supported' if i == 0 else 'evidence_unavailable',
                               'evidence': [{'source_id': 'log.md', 'quote': 'Provided accuracy is 0.8.'}]
                               if i == 0 else []} for i, item in enumerate(payload['facts'])]}
        if task == 'verify_citations':
            if fail[0]:
                raise HTTPError('http://localhost', 503, 'interrupted', {}, None)
            return {'units': [{'unit_id': item['unit_id'], 'joint_verdict': 'supported'
                               if item['cited_ids'] else 'missing_citation', 'evidence': [],
                               'individually_sufficient_ids': item['cited_ids'], 'necessary_source_ids': [],
                               'irrelevant_source_ids': []} for item in payload['units']]}
        raise AssertionError(task)

    def send(request, **kwargs):
        params = json.loads(request.data)['params']
        if params['name'] == 'model.list':
            value = {'items': [{'id': 'id', 'name': 'gpt-6-luna'}]}
        else:
            messages = params['arguments']['messages']
            assert messages[0] == {'role': 'system', 'content': specification['prompts']['system']}
            task, payload = messages[1]['content'].split('\n\nEVALUATION DATA:\n', 1)
            value = {'content': json.dumps(judge(tasks[task], json.loads(payload))), 'finish_reason': 'stop'}
        return io.BytesIO(json.dumps({'result': {'structuredContent': value}}).encode())

    monkeypatch.setattr(client.urllib.request, 'urlopen', send)
    args = SimpleNamespace(input=path, out=out, manuscript=None, rubric=None,
                           metrics=['writingbench', 'alce', 'factscore'], model='gpt-6-luna', model_id=None,
                           base_url=None, auth_file=None, jobs=3, resume=False, offline=False)
    if interrupted:
        with pytest.raises(HTTPError):
            ev.evaluate(args)
        assert autosurvey.read(out / 'status.json')['status'] == 'failed'
        assert not (out / 'score.json').exists()
        fail[0], args.resume = False, True
    result = ev.evaluate(args)
    assert result['official_benchmark'] is False and result['posthoc_adjudication'] is False
    assert result['writingbench_inspired']['mean_1_to_10'] == 8
    assert result['factscore_inspired']['verifiable_precision'] == 1
    assert result['factscore_inspired']['strict_support_fraction'] == 0.5
    assert result['alce_inspired']['strict_recall_lower_bound'] == 0.5
    assert result['alce_inspired']['citation_counts']['supported_useful'] == 1
    assert result['extraction']['paragraphs'] == {'expected': 1, 'returned': 1}
    assert Counter(seen)['writing'] == 5 and Counter(seen)['rubric'] == 1
    calls_before = list(seen)
    args.resume, args.offline = True, True
    assert ev.evaluate(args)['factscore_inspired'] == result['factscore_inspired']
    assert seen == calls_before
    args.resume, args.offline, args.out = False, False, tmp_path / 'second-candidate'
    args.rubric, args.metrics = out / 'rubric-frozen.json', ['writingbench']
    changed = fixture_input()
    changed['manuscript'] = changed['manuscript'].replace('Provided accuracy is', 'The reported accuracy is')
    autosurvey.save(path, changed)
    ev.evaluate(args)
    assert Counter(seen)['rubric'] == 1 and Counter(seen)['ledger'] == 1
    assert Counter(seen)['writing'] == 10
    args.out = tmp_path / 'different-assignment'
    changed['task'] = {'request': 'A different assignment.'}
    autosurvey.save(path, changed)
    with pytest.raises(ValueError, match='Shared rubric requires the same'):
        ev.evaluate(args)


def test_unknown_evidence_is_not_reported_as_perfect_accuracy():
    extracted = {'citation_units': [{'unit_id': 'U1', 'cited_ids': ['L1']}]}
    verified = {'facts': [{'fact_id': 'F1', 'verdict': 'evidence_unavailable'}], 'citation_units': [
        {'unit_id': 'U1', 'joint_verdict': 'insufficient_evidence', 'individually_sufficient_ids': [],
         'necessary_source_ids': [], 'irrelevant_source_ids': []}]}
    result = ev.aggregate(extracted, verified)
    assert result['factscore_inspired']['verifiable_precision'] is None
    assert result['factscore_inspired']['counts'] == {'evidence_unavailable': 1}
    assert result['alce_inspired']['verifiable_recall'] is None
    assert result['alce_inspired']['unknown_citation_fraction'] == 1
    assert result['alce_inspired']['strict_recall_lower_bound'] == 0
    with pytest.raises(ValueError, match='omitted, duplicated or invented'):
        ev.rows({'facts': [{'fact_id': 'F1'}, {'fact_id': 'F1'}]}, 'facts', 'fact_id', ['F1'])


def test_invalid_json_shape_is_preserved_as_failure_and_not_cached(tmp_path):
    class API:
        def call_tool(self, *_args):
            return {'content': '[]', 'finish_reason': 'stop'}

    calls = autosurvey.Calls(
        tmp_path, {'id': 'id', 'name': 'judge'}, API, system='Evaluator', content_validator=ev.parse)
    with pytest.raises(ValueError, match='JSON object'):
        calls.chat('Evaluate.', 0, 'test', 'json')
    assert not list(tmp_path.rglob('completion.json'))
    assert autosurvey.read(next(tmp_path.rglob('attempt-01-status.json')))['status'] == 'failed'
