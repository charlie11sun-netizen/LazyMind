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


@pytest.mark.parametrize('fields', [
    dict(reasoning_content='SDK reasoning', reasoning='Duplicate reasoning'),
    dict(reasoning='SDK reasoning', reasoning_details=[dict(text='Duplicate reasoning')]),
    dict(reasoning_details=[dict(text='SDK '), dict(text='reasoning')]),
])
def test_final_sdk_fields_are_authoritative_over_preview_deltas(monkeypatch, tmp_path, fields):
    client = lazyllm.OnlineChatModule(source='openai', api_key='offline-test', model='f4-test')
    raw = dict(content='Final body', **fields)

    def call(_client, _prompt, options, _policy, _supported):
        sink = options['stream_output']['_stream_sink']
        sink(dict(tag='think', delta='Preview reasoning'))
        sink(dict(tag='text', delta='Preview body'))
        return raw

    monkeypatch.setattr(lazyllm, 'AutoModel', lambda **_: client)
    monkeypatch.setattr(model.transport, 'call', call)
    monkeypatch.setenv('LAZYMIND_PAPER_MODEL_STREAM', 'auto')
    with trace.capture(tmp_path, 'authoritative-response', emit=lambda _: None) as recorder:
        with trace.stage('interpretation', attempt=1):
            assert model.call_markdown_model('Offline request') == 'Final body'
    step = json.loads((recorder.root / 'public.json').read_text())['process_steps'][0]
    assert step['output']['text'] == 'Final body'
    assert step['reasoning']['text'] == 'SDK reasoning'
    assert raw == dict(content='Final body', **fields)


@pytest.mark.parametrize('fields,expected', [
    (dict(reasoning_content='', reasoning=' \n  draft\n'), ' \n  draft\n'),
    (dict(reasoning_details=dict(text=' \n  draft\n')), ' \n  draft\n'),
    (dict(reasoning_details=[dict(text=' \n'), dict(text='  draft\n'),
                            dict(type='reasoning.encrypted', data='opaque')]), ' \n  draft\n'),
    (dict(reasoning_details=[dict(summary='Visible summary')]), 'Visible summary'),
    (dict(reasoning_details=[None, 42, dict(data='opaque', signature='not-display-text')]), ''),
])
def test_reasoning_aliases_preserve_text_without_changing_body(fields, expected):
    response = normalize_response(dict(content=' \nFinal body\n', **fields))
    assert response.body == ' \nFinal body\n'
    assert response.reasoning == expected


def test_reasoning_alias_does_not_turn_an_ordinary_plan_into_a_chat_message():
    plan = dict(questions=['Compare means?'], outline='Group means', reasoning='Method note')
    assert normalize_response(plan).body == plan
    response = normalize_response(dict(role='assistant', reasoning='Draft code'))
    assert response.body == '' and response.reasoning == 'Draft code'


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('source', ['minimax', 'deepseek', 'qwen', 'openai'])
@pytest.mark.parametrize('route', ['direct', 'dynamic_new', 'dynamic_cached'])
@pytest.mark.parametrize('stage,method,body,expected', [
    ('plan_generation', 'call_model', '  {"questions":["Observed means?"],"outline":"Group means"}\n',
     dict(questions=['Observed means?'], outline='Group means')),
    ('figure_code_generation', 'call_code_model', '```python\nimport matplotlib.pyplot as plt\n```\n',
     dict(code='import matplotlib.pyplot as plt')),
    ('interpretation', 'call_markdown_model', '## Conclusion\n\nMean score: 2.\n',
     '## Conclusion\n\nMean score: 2.\n'),
])
def test_native_provider_channels_through_sdk_and_f4(
        monkeypatch, tmp_path, stream, source, route, stage, method, body, expected,
        reasoning_field='reasoning_content'):
    from lazyllm.module.llms.onlinemodule.chat import OnlineChatModule, dynamic_chat_config

    client = (OnlineChatModule(source='dynamic') if route != 'direct' else
              OnlineChatModule(source=source, api_key='offline-test', model='f4-test'))
    unrelated = OnlineChatModule(source=source, api_key='offline-test', model='other-flow')
    converter = type(unrelated)._convert_msg_format
    calls, thinking = [], 'Draft candidate; final code belongs in content.'
    fields = {reasoning_field: [dict(text=thinking)] if reasoning_field == 'reasoning_details' else thinking}

    class Response:
        status_code = 200
        text = json.dumps(dict(choices=[dict(index=0, finish_reason='stop', message=dict(
            role='assistant', content=body, **fields))]))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_lines(self):
            deltas = [dict(role='assistant', content=body[:7], **fields),
                      dict(content=body[7:])]
            for delta in deltas:
                yield ('data: ' + json.dumps(dict(choices=[dict(index=0, delta=delta)]))).encode()
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'
            yield b'data: [DONE]'

    def post(_url, **kwargs):
        calls.append(kwargs['json'])
        return Response()

    monkeypatch.setattr('requests.post', post)
    monkeypatch.setattr(lazyllm, 'AutoModel', lambda **_: client)
    monkeypatch.setenv('LAZYMIND_PAPER_MODEL_STREAM', 'auto' if stream else 'off')
    routing = (dynamic_chat_config(modules=client, source=source, model='f4-test',
                                   url='http://provider.test/v1/', skip_auth=True)
               if route != 'direct' else nullcontext())
    with routing:
        if route == 'dynamic_cached':
            with lazyllm.globals.stack_enter(client.identities):
                client._get_supplier()
        with trace.capture(tmp_path, 'native-test', emit=lambda _: None) as recorder:
            with trace.stage(stage, attempt=1):
                assert getattr(model, method)('Offline request') == expected
        if route == 'direct':
            supplier = client
        else:
            with lazyllm.globals.stack_enter(client.identities):
                supplier = client._get_supplier()
    step = json.loads((recorder.root / 'public.json').read_text())['process_steps'][0]
    assert step['output']['text'] == body and step['reasoning']['text'] == thinking
    assert len(calls) == 1 and calls[0]['stream'] == stream
    assert calls[0].get('reasoning_split') is (True if source == 'minimax' else None)
    assert ('response_format' in calls[0]) == (method == 'call_model')
    assert type(unrelated)._convert_msg_format is converter
    assert '_convert_msg_format' not in supplier.__dict__
    assert supplier._timeout == (15, 600)
    assert '_convert_msg_format' not in unrelated.__dict__ and unrelated._model_optional_params == {}
    assert unrelated._formatter(dict(role='assistant', content='body', reasoning_content='draft')) \
        == '<think>draft</think>body'


@pytest.mark.parametrize('reasoning_field', ['reasoning', 'reasoning_details'])
@pytest.mark.parametrize('stream', [False, True])
def test_gateway_reasoning_aliases_through_sdk_and_preview(monkeypatch, tmp_path, reasoning_field, stream):
    code = 'import matplotlib.pyplot as plt\ndef draw(data, statistics, spec):\n    return plt.figure()'
    test_native_provider_channels_through_sdk_and_f4(
        monkeypatch, tmp_path, stream, 'openai', 'dynamic_cached', 'figure_code_generation',
        'call_code_model', f'```python\n{code}\n```', dict(code=code), reasoning_field=reasoning_field)


@pytest.mark.parametrize('reasoning_field', ['reasoning', 'reasoning_details'])
@pytest.mark.parametrize('interrupted', [False, True])
def test_alias_reasoning_is_visible_before_body_and_survives_interruption(
        monkeypatch, tmp_path, reasoning_field, interrupted):
    import requests
    from lazyllm.module.llms.onlinemodule.base import ModelCallError

    client = lazyllm.OnlineChatModule(source='openai', api_key='offline-test', model='f4-test')
    parser_class = client.RESPONSE_PARSER_CLASS
    body, thinking, calls = 'Final body', '', []

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_lines(self):
            nonlocal thinking
            for text in ('Draft ', '\n  continuation'):
                fields = {reasoning_field: [dict(text=text)] if reasoning_field == 'reasoning_details' else text}
                if reasoning_field == 'reasoning':
                    fields['reasoning_details'] = [dict(text='Duplicate; do not emit this')]
                yield ('data: ' + json.dumps(dict(choices=[dict(index=0, delta=fields)]))).encode()
                thinking += text
                step = next(iter(recorder.public.steps.values()))
                assert step['reasoning']['text'] == thinking
                assert step['reasoning']['mode'] == 'streaming'
                assert not step.get('output', {}).get('text')
                published = json.loads((recorder.root / 'public.json').read_text())['process_steps'][0]
                assert published['reasoning']['text'] and published['reasoning']['mode'] == 'streaming'
            if interrupted:
                raise requests.ConnectionError('Offline stream interrupted')
            yield ('data: ' + json.dumps(dict(choices=[dict(index=0, delta=dict(content=body))]))).encode()
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'
            yield b'data: [DONE]'

    def post(*_args, **_kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr('requests.post', post)
    monkeypatch.setattr(lazyllm, 'AutoModel', lambda **_: client)
    monkeypatch.setenv('LAZYMIND_PAPER_MODEL_STREAM', 'auto')
    with trace.capture(tmp_path, 'live-alias', emit=lambda _: None) as recorder:
        if interrupted:
            with pytest.raises(ModelCallError), trace.stage('interpretation', attempt=1):
                model.call_markdown_model('Offline request')
        else:
            with trace.stage('interpretation', attempt=1):
                assert model.call_markdown_model('Offline request') == body
    step = json.loads((recorder.root / 'public.json').read_text())['process_steps'][0]
    assert step['reasoning']['text'] == thinking
    assert step['reasoning']['mode'] == ('interrupted' if interrupted else 'complete')
    assert step.get('output', {}).get('text', '') == ('' if interrupted else body)
    assert len(calls) == 1
    assert type(client).RESPONSE_PARSER_CLASS is parser_class
    assert lazyllm.OnlineChatModule(source='openai', api_key='offline-test', model='other-flow') \
        .RESPONSE_PARSER_CLASS is parser_class


def test_stream_preview_keeps_the_provider_specific_error_parser(monkeypatch, tmp_path):
    from lazyllm.module.llms.onlinemodule.base import ModelCallError

    client = lazyllm.OnlineChatModule(source='minimax', api_key='offline-test', model='f4-test')

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_lines(self):
            yield b'data: {"base_resp":{"status_code":1004},"choices":[]}'

    monkeypatch.setattr('requests.post', lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(lazyllm, 'AutoModel', lambda **_: client)
    monkeypatch.setenv('LAZYMIND_PAPER_MODEL_STREAM', 'auto')
    with trace.capture(tmp_path, 'provider-error', emit=lambda _: None):
        with pytest.raises(ModelCallError) as error, trace.stage('plan_generation', attempt=1):
            model.call_model('Offline request')
    assert error.value.terminal.failure.code.value == 'authentication_failed'
