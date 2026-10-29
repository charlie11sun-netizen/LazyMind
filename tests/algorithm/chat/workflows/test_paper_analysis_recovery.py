"""Core native-response and line-repair contracts, without live model calls."""
import json
from contextlib import nullcontext

import lazyllm
import pytest

from lazymind.document_tools.paper.analysis import trace
from lazymind.document_tools.paper.plotting import model
from lazymind.document_tools.paper.plotting.code_response import (
    normalize_response, parse_code_response, repair_response,
)


def test_local_repair_applies_ranges_against_the_same_source():
    source = 'def analyze():\n    score = 1\n    cost = 2\n    return score, cost\n'
    patched = repair_response(dict(replacements=[
        dict(start_line=3, end_line=3, code='    cost = 4'),
        dict(start_line=2, end_line=2, code='    score = 3'),
    ]), source)
    namespace = {}
    exec(patched, namespace)
    assert namespace['analyze']() == (3, 4)
    assert patched.splitlines()[0] == source.splitlines()[0]
    assert patched.splitlines()[-1] == source.splitlines()[-1]


def test_overlapping_ranges_are_not_applied():
    with pytest.raises(ValueError, match='重叠'):
        repair_response(dict(replacements=[dict(start_line=1, end_line=2, code='pass'),
                                           dict(start_line=2, end_line=2, code='pass')]), 'a\nb\n')


def test_code_is_read_from_native_body_without_merging_reasoning():
    code = 'def draw(data, statistics, spec):\n    return figure'
    response = normalize_response(dict(content=f'```python\n{code}\n```',
                                       reasoning_content='Unfinished draft code'))
    assert parse_code_response(response.body)['code'] == code
    assert response.reasoning == 'Unfinished draft code'
    with pytest.raises(ValueError, match='未返回代码'):
        parse_code_response(normalize_response(dict(reasoning_content=code)).body)


# Each case covers a distinct transport regression; avoid provider/format cross-products.
@pytest.mark.parametrize('source,stream,field,kind,interrupted', [
    ('minimax', False, 'reasoning_content', 'plan', False),
    ('minimax', True, 'reasoning_content', 'code', False),
    ('openai', True, 'reasoning', 'code', False),
    ('openai', True, 'reasoning_details', 'markdown', False),
    ('openai', False, 'reasoning_details', 'plan', False),
    ('openai', True, 'reasoning', 'markdown', True),
])
def test_native_provider_channels_through_sdk_and_f4(
        monkeypatch, tmp_path, source, stream, field, kind, interrupted):
    import requests
    from lazyllm.module.llms.onlinemodule.base import ModelCallError
    from lazyllm.module.llms.onlinemodule.chat import dynamic_chat_config

    stage, method, body, expected = {
        'plan': ('plan_generation', 'call_model', '{"questions":["Means?"],"outline":"Means"}',
                 dict(questions=['Means?'], outline='Means')),
        'code': ('figure_code_generation', 'call_code_model', '```python\nimport math\n```',
                 dict(code='import math')),
        'markdown': ('interpretation', 'call_markdown_model', '## Conclusion\nMean: 2.', '## Conclusion\nMean: 2.'),
    }[kind]
    routed = source == 'openai'
    client = lazyllm.OnlineChatModule(source='dynamic') if routed else lazyllm.OnlineChatModule(
        source=source, api_key='offline-test', model='f4-test')
    unrelated = lazyllm.OnlineChatModule(source=source, api_key='offline-test', model='other-flow')
    parser = unrelated.RESPONSE_PARSER_CLASS
    thinking, calls = 'Draft.\n  Continue.', []

    def reasoning(text):
        return {field: [dict(text=text)] if field == 'reasoning_details' else text}

    class Response:
        status_code = 200
        text = json.dumps(dict(choices=[dict(index=0, finish_reason='stop', message=dict(
            role='assistant', content=body, **reasoning(thinking)))]))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_lines(self):
            yield ('data: ' + json.dumps(dict(choices=[dict(index=0, delta=reasoning(thinking))]))).encode()
            step = json.loads((recorder.root / 'public.json').read_text())['process_steps'][0]
            assert step['reasoning']['text'] == thinking and step['reasoning']['mode'] == 'streaming'
            assert not step.get('output', {}).get('text')
            if interrupted:
                raise requests.ConnectionError('Offline stream interrupted')
            yield ('data: ' + json.dumps(dict(choices=[dict(index=0, delta=dict(content=body))]))).encode()
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'
            yield b'data: [DONE]'

    def post(_url, **kwargs):
        calls.append(kwargs['json'])
        return Response()

    monkeypatch.setattr('requests.post', post)
    monkeypatch.setattr(lazyllm, 'AutoModel', lambda **_: client)
    monkeypatch.setenv('LAZYMIND_PAPER_MODEL_STREAM', 'auto' if stream else 'off')
    routing = dynamic_chat_config(modules=client, source=source, model='f4-test',
                                  url='http://provider.test/v1/', skip_auth=True) if routed else nullcontext()
    with routing:
        if routed:
            with lazyllm.globals.stack_enter(client.identities):
                client._get_supplier()  # Exercise the cached caller, not just the router.
        with trace.capture(tmp_path, 'native-test', emit=lambda _: None) as recorder:
            with trace.stage(stage, attempt=1):
                if interrupted:
                    with pytest.raises(ModelCallError):
                        getattr(model, method)('Offline request')
                else:
                    assert getattr(model, method)('Offline request') == expected
    step = json.loads((recorder.root / 'public.json').read_text())['process_steps'][0]
    assert step['reasoning']['text'] == thinking
    expected_mode = 'interrupted' if interrupted else 'complete' if stream else 'buffered'
    assert step['reasoning']['mode'] == expected_mode
    assert step.get('output', {}).get('text', '') == ('' if interrupted else body)
    assert len(calls) == 1 and calls[0]['stream'] == stream
    assert calls[0].get('reasoning_split') is (True if source == 'minimax' else None)
    assert ('response_format' in calls[0]) == (kind == 'plan')
    assert type(unrelated).RESPONSE_PARSER_CLASS is parser and unrelated._model_optional_params == {}
    assert unrelated._formatter(dict(role='assistant', content='body', reasoning_content='draft')) \
        == '<think>draft</think>body'


def test_final_response_replaces_preview_without_reinterpreting_plan_fields(monkeypatch, tmp_path):
    client = lazyllm.OnlineChatModule(source='openai', api_key='offline-test', model='f4-test')
    raw = dict(content='Final body', reasoning='Final reasoning')

    def call(_client, _prompt, options, *_):
        options['stream_output']['_stream_sink'](dict(tag='think', delta='Partial preview'))
        return raw

    monkeypatch.setattr(lazyllm, 'AutoModel', lambda **_: client)
    monkeypatch.setattr(model.transport, 'call', call)
    monkeypatch.setenv('LAZYMIND_PAPER_MODEL_STREAM', 'auto')
    with trace.capture(tmp_path, 'final-response', emit=lambda _: None) as recorder:
        with trace.stage('interpretation'):
            assert model.call_markdown_model('Offline request') == 'Final body'
    step = json.loads((recorder.root / 'public.json').read_text())['process_steps'][0]
    assert step['reasoning']['text'] == 'Final reasoning' and raw['reasoning'] == 'Final reasoning'
    plan = dict(questions=['Means?'], outline='Means', reasoning='Method note')
    assert normalize_response(plan).body == plan
