import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_rewrite import _load_rewrite_routes_module, rewrite

selection = rewrite.range_selection


def response(prompt):
    payload = json.loads(prompt.split('\n')[-1])
    return json.dumps({'results': [
        {'id': item['id'], 'content': item['content'].replace('原文', '表述')}
        for item in payload['paragraphs']
    ]}, ensure_ascii=False)


def test_disjoint_unicode_ranges_preserve_every_gap():
    source = '# 标题\n\n😀原文。\n\n`代码` [链接](https://example.org)\n\n尾段原文。'
    starts = [source.index('原文'), source.rindex('原文')]
    ranges = [{'start': i, 'end': i + 2, 'content': '原文'} for i in starts]
    result = selection.rewrite_ranges(source, ranges, '润色', generate=response)
    candidate = selection.apply_paragraph_results(source, result['results'])
    assert candidate == source.replace('原文', '表述')


def test_continuous_cross_paragraph_preserves_boundaries():
    source = '前缀原文。\n\n下一段原文。后缀'
    start, end = 2, len(source) - 2
    result = selection.rewrite_ranges(source, [{'start': start, 'end': end, 'content': source[start:end]}],
                                      '更正式', generate=response)
    assert len(result['results']) == 2
    assert result['results'][0]['old_content'] == '前缀原文。'
    assert selection.apply_paragraph_results(source, result['results']) == source.replace('原文', '表述')


@pytest.mark.parametrize('ranges', [
    [{'start': -1, 'end': 1, 'content': 'a'}],
    [{'start': 0, 'end': 1, 'content': 'wrong'}],
])
def test_invalid_ranges_rejected_before_model(ranges):
    with pytest.raises(rewrite.BadRequestError):
        selection.rewrite_ranges('abcdef', ranges, '润色', generate=lambda _: pytest.fail('model was called'))


@pytest.mark.parametrize('items', [
    [{'id': '0', 'content': '润色'}],
    [{'id': '0', 'content': '润色'}, {'id': '0', 'content': '润色'}],
    [{'id': '0', 'content': '润色'}, {'id': 'unknown', 'content': '润色'}],
    [{'id': '0', 'content': '润色'}, {'id': '1', 'content': ''}],
    [{'id': '0', 'content': '润色'}, {'id': '1', 'content': '拆成\n\n两段'}],
])
def test_invalid_model_output_is_not_a_success(items):
    source = '原文。\n\n尾段。'
    with pytest.raises(rewrite.UnprocessableContentError):
        selection.rewrite_ranges(source, [{'start': 0, 'end': len(source), 'content': source}], '润色',
                                 generate=lambda _: json.dumps({'results': items}))


def test_route_returns_exact_patch_and_validates_config(monkeypatch):
    routes = _load_rewrite_routes_module()
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    monkeypatch.setattr(selection, 'AutoModel', lambda **_: response)
    payload = {'task_type': 'polish', 'content': '原文', 'user_instruct': '润色',
               'llm_config': {'llm': {'model': 'test'}}, 'full_content': '前原文后',
               'selection_ranges': [{'start': 1, 'end': 3, 'content': '原文'}]}
    result = client.post('/api/chat/rewrite', json=payload)
    assert result.status_code == 200
    assert result.json()['results'][0]['content'] == '前表述后'
    assert client.post('/api/chat/rewrite', json={**payload, 'llm_config': {}}).status_code == 200
    for config in (None, {'llm': None}):
        result = client.post('/api/chat/rewrite', json={**payload, 'llm_config': config})
        assert result.status_code == 422
    for field in ('selection_start', 'selection_end'):
        assert client.post('/api/chat/rewrite', json={**payload, field: 0}).status_code == 422


@pytest.mark.parametrize('source', ['# 原文', '```\n原文\n```'])
def test_non_paragraph_selection_is_rejected(source):
    start = source.index('原文')
    with pytest.raises(rewrite.BadRequestError):
        selection.rewrite_ranges(source, [{'start': start, 'end': start + 2, 'content': '原文'}],
                                 '润色', generate=lambda _: pytest.fail('model called'))
