from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_TOOLS_PATH = _ROOT / 'workflows' / 'writer-workflow' / 'scripts' / 'tools.py'


def _load_tools_module() -> ModuleType:
    module_name = 'writer_workflow_tools_draft_stream_test'
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, _TOOLS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ('query', 'expected'),
    [
        ('写一篇 800 字左右的小说', {'target_chars': 800, 'max_chars': 880}),
        ('写一篇不超过500字的摘要', {'target_chars': 500, 'max_chars': 500}),
        ('写一篇短文', {}),
    ],
)
def test_build_writing_task_extracts_document_length_constraints(query, expected):
    tools = _load_tools_module()

    task = json.loads(tools.WriterCreateToolkit().build_writing_task(query))

    assert task.get('constraints', {}) == expected


@pytest.mark.parametrize(
    ('structure_mode', 'expected_step'),
    [
        ('flat', 'write_document'),
        ('sectioned', 'outline'),
    ],
)
def test_writer_command_trusts_explicit_workflow_structure_mode(
    monkeypatch,
    tmp_path,
    structure_mode,
    expected_step,
):
    tools = _load_tools_module()
    query = '写一篇介绍新能源汽车降价的文章'
    context = SimpleNamespace(
        workspace_path=str(tmp_path),
        params={
            'step_id': 'prepare',
            'user_input': query,
            'workflow_parameters': {'structure_mode': structure_mode},
        },
    )
    monkeypatch.setattr(tools, 'require_context', lambda: context)

    command_path = tools.writer_resolve_command(
        user_input=query,
        action='create',
        source_role='none',
    )
    command = tools._load_writer_command(command_path)

    assert command.structure_mode == structure_mode
    assert command.next_step == expected_step


def test_writer_command_does_not_infer_structure_mode_from_keywords(monkeypatch, tmp_path):
    tools = _load_tools_module()
    query = '写一篇连续正文，不使用小标题'
    context = SimpleNamespace(
        workspace_path=str(tmp_path),
        params={'step_id': 'prepare', 'user_input': query},
    )
    monkeypatch.setattr(tools, 'require_context', lambda: context)

    command_path = tools.writer_resolve_command(
        user_input=query,
        action='create',
        source_role='none',
    )
    command = tools._load_writer_command(command_path)

    assert command.structure_mode == 'sectioned'
    assert command.next_step == 'outline'


def test_task_mode_writer_rejects_missing_structure_mode(monkeypatch, tmp_path):
    tools = _load_tools_module()
    query = '写一篇介绍新能源汽车降价的文章'
    context = SimpleNamespace(
        workspace_path=str(tmp_path),
        params={
            'step_id': 'prepare',
            'user_input': query,
            'workflow_parameters': {'task_mode': True},
        },
    )
    monkeypatch.setattr(tools, 'require_context', lambda: context)

    with pytest.raises(
        ValueError,
        match=r'Task-mode Writer requires workflow_parameters\.structure_mode',
    ):
        tools.writer_resolve_command(
            user_input=query,
            action='create',
            source_role='none',
        )


def test_writer_workflow_consumes_task_mode_structure_from_host():
    content = (_ROOT / 'workflows' / 'writer-workflow' / 'workflow.yaml').read_text(
        encoding='utf-8',
    )

    assert 'In task mode only' in content
    assert 'immutable `structure_mode`' in content
    assert 'Outside task mode, keep the existing' in content
    assert 'do not classify the request text again' in content


def test_flat_draft_workspace_skips_outline_and_section_generation(monkeypatch, tmp_path):
    tools = _load_tools_module()
    query = '写一篇连续正文，不使用小标题'
    command = tools.WriterCommand(
        action='create',
        source_role='none',
        target_stage='document',
        next_step='write_document',
        structure_mode='flat',
        user_instruction=query,
        request_fingerprint=tools._writer_request_fingerprint(query),
    )
    command_path = tmp_path / 'writer_command.json'
    command_path.write_text(command.model_dump_json(), encoding='utf-8')
    task_path = tmp_path / 'writing_task.json'
    task_path.write_text(json.dumps({
        'query': query,
        'task_type': 'write',
        'output': {'representation': 'markdown'},
    }), encoding='utf-8')
    context_path = tmp_path / 'writing_context.json'
    context_path.write_text('{"context_id":"ctx-flat"}', encoding='utf-8')
    media_path = tmp_path / 'media_assets.json'
    media_path.write_text('{"assets":{}}', encoding='utf-8')
    plan_path = tmp_path / 'short_writing_plan.json'
    plan_path.write_text('{}', encoding='utf-8')
    draft_path = tmp_path / 'draft.md'
    draft_path.write_text('# 标题\n\n正文。\n', encoding='utf-8')
    updated_context_path = tmp_path / 'writing_context_after_draft.json'
    updated_context_path.write_text('{"context_id":"ctx-flat"}', encoding='utf-8')
    short_document_args = {}
    context = SimpleNamespace(
        workspace_path=str(tmp_path),
        params={
            'step_id': 'write_document',
            'user_input': query,
            'remote_inputs': {
                'writer_command': str(command_path),
                'writing_task': str(task_path),
                'writing_context': str(context_path),
                'media_assets': str(media_path),
            },
        },
        emit=lambda _event: None,
    )
    calls = []
    monkeypatch.setattr(tools, 'require_context', lambda: context)
    monkeypatch.setattr(
        tools,
        'writer_generate_short_writing_plan',
        lambda **_kwargs: calls.append('short_plan') or str(plan_path),
    )
    monkeypatch.setattr(
        tools,
        'writer_generate_short_document',
        lambda **kwargs: (
            short_document_args.update(kwargs),
            calls.append('short_document'),
            str(draft_path),
        )[-1],
    )
    monkeypatch.setattr(
        tools,
        'writer_generate_section_instructions',
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError('must skip section planning')),
    )
    monkeypatch.setattr(
        tools,
        'writer_update_writing_context',
        lambda **_kwargs: str(updated_context_path),
    )
    monkeypatch.setattr(
        tools,
        '_save_draft_workspace_artifacts',
        lambda _result: ['short_writing_plan', 'draft_document', 'writing_context_after_draft'],
    )

    result = tools.writer_draft_workspace()

    assert calls == ['short_plan', 'short_document']
    assert result['structure_mode'] == 'flat'
    assert result['draft_section_count'] is None
    assert Path(short_document_args['visual_plan_path']).is_file()
    assert short_document_args['resolved_media_assets_path'] == ''


def test_flat_draft_workspace_resolves_planned_visuals(monkeypatch, tmp_path):
    tools = _load_tools_module()
    query = '写一篇连续正文，并加入一张说明图片'
    command = tools.WriterCommand(
        action='create',
        source_role='none',
        target_stage='document',
        next_step='write_document',
        structure_mode='flat',
        user_instruction=query,
        request_fingerprint=tools._writer_request_fingerprint(query),
    )
    command_path = tmp_path / 'writer_command.json'
    command_path.write_text(command.model_dump_json(), encoding='utf-8')
    task_path = tmp_path / 'writing_task.json'
    task_path.write_text(json.dumps({
        'query': query,
        'task_type': 'write',
        'output': {'representation': 'markdown'},
    }), encoding='utf-8')
    context_path = tmp_path / 'writing_context.json'
    context_path.write_text('{"context_id":"ctx-flat-visual"}', encoding='utf-8')
    media_path = tmp_path / 'media_assets.json'
    media_path.write_text('{"library_id":"available","assets":{}}', encoding='utf-8')
    plan_path = tmp_path / 'short_writing_plan.json'
    plan_path.write_text(json.dumps({
        'visual_needs': [
            {
                'need_id': 'visual-document-1',
                'content_ref': {'document_root': True},
                'visual_type': 'image',
                'purpose': '说明消费者购车决策因素',
                'required': True,
            },
        ],
    }), encoding='utf-8')
    resolved_path = tmp_path / 'resolved_media_assets.json'
    resolved_path.write_text(json.dumps({
        'library_id': 'resolved',
        'assets': {},
        'visual_need_asset_ids': {},
    }), encoding='utf-8')
    draft_path = tmp_path / 'draft.md'
    draft_path.write_text('# 标题\n\n正文。\n', encoding='utf-8')
    updated_context_path = tmp_path / 'writing_context_after_draft.json'
    updated_context_path.write_text('{"context_id":"ctx-flat-visual"}', encoding='utf-8')
    context = SimpleNamespace(
        workspace_path=str(tmp_path),
        params={
            'step_id': 'write_document',
            'user_input': query,
            'remote_inputs': {
                'writer_command': str(command_path),
                'writing_task': str(task_path),
                'writing_context': str(context_path),
                'media_assets': str(media_path),
            },
        },
        emit=lambda _event: None,
    )
    calls = []
    short_document_args = {}
    monkeypatch.setattr(tools, 'require_context', lambda: context)
    monkeypatch.setattr(tools, 'writer_generate_short_writing_plan', lambda **_kwargs: str(plan_path))
    monkeypatch.setattr(
        tools,
        'writer_resolve_visual_media',
        lambda **kwargs: (
            calls.append(('resolve', kwargs)),
            {'resolved_media_assets': str(resolved_path), 'warnings': []},
        )[-1],
    )
    monkeypatch.setattr(
        tools,
        'writer_generate_short_document',
        lambda **kwargs: (
            short_document_args.update(kwargs),
            calls.append(('draft', kwargs)),
            str(draft_path),
        )[-1],
    )
    monkeypatch.setattr(tools, 'writer_update_writing_context', lambda **_kwargs: str(updated_context_path))
    monkeypatch.setattr(
        tools,
        '_save_draft_workspace_artifacts',
        lambda _result: [
            'short_writing_plan',
            'visual_plan',
            'resolved_media_assets',
            'draft_document',
            'writing_context_after_draft',
        ],
    )

    result = tools.writer_draft_workspace()

    assert [name for name, _kwargs in calls] == ['resolve', 'draft']
    assert short_document_args['resolved_media_assets_path'] == str(resolved_path)
    assert Path(short_document_args['visual_plan_path']).is_file()
    assert result['warnings'] == []


def test_write_document_revision_emits_markdown_draft_stream(monkeypatch, tmp_path):
    tools = _load_tools_module()
    events: list[dict] = []
    context = SimpleNamespace(
        workspace_path=str(tmp_path),
        params={'step_id': 'write_document'},
        emit=events.append,
    )

    class FakeWriterRevisionToolkit:
        def apply_string_replace(self, **_kwargs) -> str:
            return json.dumps({
                'string_replace_result': {'replaced': 1},
                'revised_document': '# Revised title\n\nUpdated body.\n',
            })

    monkeypatch.setattr(tools, 'require_context', lambda: context)
    monkeypatch.setattr(tools, 'WriterRevisionToolkit', FakeWriterRevisionToolkit)
    base_document_path = tmp_path / 'draft.md'
    base_document_path.write_text('# Original\n', encoding='utf-8')
    writing_context_path = tmp_path / 'context.json'
    writing_context_path.write_text('{}', encoding='utf-8')
    revision_set_path = tmp_path / 'revisions.json'
    revision_set_path.write_text('{}', encoding='utf-8')

    result = tools.writer_apply_revision(
        str(base_document_path),
        str(writing_context_path),
        str(revision_set_path),
    )

    assert Path(result['draft_document']).read_text(encoding='utf-8') == (
        '# Revised title\n\nUpdated body.\n'
    )
    assert events[0]['type'] == 'artifact_stream_start'
    assert events[-1]['type'] == 'artifact_stream_end'
    assert all(event['slot'] == 'draft_document' for event in events)
    assert all(event['content_type'] == 'text/markdown' for event in events)
    deltas = [
        event['delta']
        for event in events
        if event['type'] == 'artifact_stream'
    ]
    assert ''.join(deltas) == '# Revised title\n\nUpdated body.'
    assert all(0 < len(delta) <= 2 for delta in deltas)
    assert [event['chunk_index'] for event in events] == list(
        range(1, len(events) + 1),
    )


def test_markdown_draft_blocks_do_not_pass_resolved_media(monkeypatch, tmp_path):
    tools = _load_tools_module()
    context = SimpleNamespace(
        workspace_path=str(tmp_path),
        params={'step_id': 'write_document'},
        emit=lambda _event: None,
    )
    captured = {}

    class FakeWriterCreateToolkit:
        def stream_draft_blocks_markdown(self, **kwargs):
            captured.update(kwargs)
            return json.dumps(['## 第一章\n\n正文。\n'])

    monkeypatch.setattr(tools, 'require_context', lambda: context)
    monkeypatch.setattr(tools, 'WriterCreateToolkit', FakeWriterCreateToolkit)
    writing_task_path = tmp_path / 'writing_task.json'
    writing_task_path.write_text('{}', encoding='utf-8')
    section_instructions_path = tmp_path / 'section_instructions.json'
    section_instructions_path.write_text('{}', encoding='utf-8')
    writing_context_path = tmp_path / 'writing_context.json'
    writing_context_path.write_text('{}', encoding='utf-8')
    visual_plan_path = tmp_path / 'visual_plan.json'
    visual_plan_path.write_text('{"instructions": []}', encoding='utf-8')

    paths = tools.writer_generate_draft_blocks_markdown(
        str(writing_task_path),
        str(section_instructions_path),
        str(writing_context_path),
        str(visual_plan_path),
    )

    assert 'media_assets_json' not in captured
    assert captured['visual_plan_json'] == '{"instructions": []}'
    assert Path(paths[0]).read_text(encoding='utf-8') == '## 第一章\n\n正文。\n'


def test_markdown_media_fill_uses_persistent_uri_and_drops_missing_assets():
    tools = _load_tools_module()

    filled = tools._fill_markdown_media_placeholders(
        '\n'.join([
            '# Draft',
            '',
            '![Resolved](media-placeholder://need-1)',
            '![Unresolved](media-placeholder://need-2)',
            '![[Legacy]](media-placeholder://need-1)',
            '(media-placeholder://need-3)',
        ]),
        {
            'assets': {
                'asset-1': {
                    'uri': 'https://example.com/generated-1.png',
                    'local_path': '/data/subagent/assets/generated-1.png',
                },
                'asset-2': {'uri': 'https://example.com/unmaterialized.png'},
            },
            'visual_need_asset_ids': {
                'need-1': ['asset-1'],
                'need-2': ['asset-2'],
            },
        },
    )

    assert '![Resolved](https://example.com/generated-1.png)' in filled
    assert '![Legacy](https://example.com/generated-1.png)' in filled
    assert '![Unresolved](https://example.com/unmaterialized.png)' in filled
    assert 'media-placeholder://' not in filled
    assert 'media-asset://' not in filled


def test_short_visual_plan_reuses_normalized_plan_needs(monkeypatch, tmp_path):
    tools = _load_tools_module()
    context = SimpleNamespace(workspace_path=str(tmp_path), params={'step_id': 'write_document'})
    monkeypatch.setattr(tools, 'require_context', lambda: context)
    short_plan_path = tmp_path / 'short_writing_plan.json'
    short_plan_path.write_text(json.dumps({
        'visual_needs': [
            {
                'need_id': 'visual-document-1',
                'content_ref': {'document_root': True},
                'visual_type': 'image',
                'purpose': '展示购车决策因素',
                'required': False,
                'meta': {'placement_hint': '分析风险之后'},
            },
        ],
    }), encoding='utf-8')

    result = tools._save_short_visual_plan(str(short_plan_path))
    visual_plan = tools._read_json_file(result['visual_plan'])

    assert result['visual_need_count'] == 1
    assert result['visual_need_ids'] == ['visual-document-1']
    visual = visual_plan['instructions'][0]
    assert visual['content_ref']['document_root'] is True
    assert visual['content_ref']['node_id'] is None
    assert visual['content_ref']['heading_path'] == []
    assert visual['required'] is False


def test_short_document_fills_resolved_visual_placeholder(monkeypatch, tmp_path):
    tools = _load_tools_module()
    context = SimpleNamespace(
        workspace_path=str(tmp_path),
        params={'step_id': 'write_document'},
        emit=lambda _event: None,
    )
    captured = {}

    class FakeWriterCreateToolkit:
        def stream_short_document(self, **kwargs):
            captured.update(kwargs)
            kwargs['on_delta']('# 标题\n\n正文。\n')
            return '# 标题\n\n正文。\n\n![购车决策](media-placeholder://visual-document-1)\n'

    monkeypatch.setattr(tools, 'require_context', lambda: context)
    monkeypatch.setattr(tools, 'WriterCreateToolkit', FakeWriterCreateToolkit)
    writing_task_path = tmp_path / 'writing_task.json'
    writing_task_path.write_text('{}', encoding='utf-8')
    short_plan_path = tmp_path / 'short_writing_plan.json'
    short_plan_path.write_text('{}', encoding='utf-8')
    writing_context_path = tmp_path / 'writing_context.json'
    writing_context_path.write_text('{}', encoding='utf-8')
    visual_plan_path = tmp_path / 'visual_plan.json'
    visual_plan_path.write_text('{"instructions": []}', encoding='utf-8')
    media_assets_path = tmp_path / 'resolved_media_assets.json'
    media_assets_path.write_text(json.dumps({
        'assets': {
            'asset-1': {'uri': 'https://example.com/short-visual.png'},
        },
        'visual_need_asset_ids': {
            'visual-document-1': ['asset-1'],
        },
    }), encoding='utf-8')

    document_path = tools.writer_generate_short_document(
        writing_task_path=str(writing_task_path),
        short_writing_plan_path=str(short_plan_path),
        writing_context_path=str(writing_context_path),
        visual_plan_path=str(visual_plan_path),
        resolved_media_assets_path=str(media_assets_path),
    )
    document = Path(document_path).read_text(encoding='utf-8')

    assert captured['visual_plan_json'] == '{"instructions": []}'
    assert 'visual-document-1' in captured['media_assets_json']
    assert '![购车决策](https://example.com/short-visual.png)' in document
    assert 'media-placeholder://' not in document


def test_markdown_revision_fills_resolved_media_placeholder(monkeypatch, tmp_path):
    tools = _load_tools_module()
    context = SimpleNamespace(
        workspace_path=str(tmp_path),
        params={'step_id': 'write_document'},
        emit=lambda _event: None,
    )

    class FakeWriterRevisionToolkit:
        def apply_string_replace(self, **_kwargs) -> str:
            return json.dumps({
                'string_replace_result': {'replaced': 1},
                'revised_document': '![Visual](media-placeholder://need-1)',
            })

    monkeypatch.setattr(tools, 'require_context', lambda: context)
    monkeypatch.setattr(tools, 'WriterRevisionToolkit', FakeWriterRevisionToolkit)
    base_document_path = tmp_path / 'draft.md'
    base_document_path.write_text('# Original\n', encoding='utf-8')
    writing_context_path = tmp_path / 'context.json'
    writing_context_path.write_text('{}', encoding='utf-8')
    revision_set_path = tmp_path / 'revisions.json'
    revision_set_path.write_text('{}', encoding='utf-8')
    media_assets_path = tmp_path / 'media_assets.json'
    media_assets_path.write_text(json.dumps({
        'assets': {'asset-1': {'local_path': '/data/subagent/assets/visual.png'}},
        'visual_need_asset_ids': {'need-1': ['asset-1']},
    }), encoding='utf-8')

    result = tools.writer_apply_revision(
        str(base_document_path),
        str(writing_context_path),
        str(revision_set_path),
        str(media_assets_path),
    )

    assert Path(result['draft_document']).read_text(encoding='utf-8') == (
        '![Visual](/data/subagent/assets/visual.png)'
    )


def test_markdown_no_image_request_skips_visual_planning(monkeypatch, tmp_path):
    from lazymind.chat.engine.tools import writer

    calls = []

    class FakePlanningTools:
        def __init__(self, **_kwargs):
            pass

        def generate_visual_plan(self, **_kwargs):
            calls.append('generate_visual_plan')
            raise AssertionError('explicit no-image request must skip visual planning')

        def generate_section_instructions(self, **_kwargs):
            path = tmp_path / 'section_instructions.json'
            path.write_text(json.dumps({
                'data': {
                    'instruction_set_id': 'instructions-1',
                    'instructions': [],
                    'meta': {'representation': 'markdown'},
                },
            }), encoding='utf-8')
            return {'artifact_path': str(path)}

    monkeypatch.setattr(writer, 'WriterPlanningTools', FakePlanningTools)
    monkeypatch.setattr(writer, 'AutoModel', lambda **_kwargs: object())

    result = json.loads(writer.WriterCreateToolkit().generate_section_instructions(
        writing_task_json=json.dumps({
            'task_id': 'task-1',
            'query': '请扩写这个大纲，不要图片',
            'task_type': 'write',
        }),
        outline_json='# 标题\n\n## 第一章\n',
        writing_context_json=json.dumps({'context_id': 'context-1'}),
    ))

    assert calls == []
    assert result['visual_plan']['instructions'] == []


def test_markdown_rewrite_no_image_request_skips_visual_planning(monkeypatch, tmp_path):
    from lazymind.chat.engine.tools import writer

    calls = []

    class FakePlanningTools:
        def __init__(self, **_kwargs):
            pass

        def generate_rewrite_section_instructions(self, **_kwargs):
            path = tmp_path / 'rewrite_section_instructions.json'
            path.write_text(json.dumps({
                'data': {
                    'instruction_set_id': 'instructions-1',
                    'instructions': [],
                    'meta': {
                        'representation': 'markdown',
                        'document_title': 'Rewritten title',
                    },
                },
            }), encoding='utf-8')
            return {'artifact_path': str(path)}

        def generate_visual_plan(self, **_kwargs):
            calls.append('generate_visual_plan')
            raise AssertionError('explicit no-image request must skip visual planning')

    monkeypatch.setattr(writer, 'WriterPlanningTools', FakePlanningTools)
    monkeypatch.setattr(writer, 'AutoModel', lambda **_kwargs: object())

    result = json.loads(writer.WriterCreateToolkit().generate_rewrite_section_instructions(
        writing_task_json=json.dumps({
            'task_id': 'task-1',
            'query': '请重写全文，不要图片',
            'task_type': 'write',
        }),
        source_document_json='# 原文\n\n正文。\n',
        writing_context_json=json.dumps({'context_id': 'context-1'}),
    ))

    assert calls == []
    assert result['visual_plan']['instructions'] == []
    assert result['document_title'] == 'Rewritten title'


def test_selection_rewrite_uses_slot_markdown_artifact_filename(monkeypatch, tmp_path):
    tools = _load_tools_module()

    class FakeWriterRevisionTools:
        def __init__(self, *, llm, artifact_store):
            self.artifact_store = artifact_store

        def build_selected_markdown_replace_set(self, *_args):
            return {
                'replacements': [{
                    'replacement_id': 'replace-1',
                    'content_ref': {'document_root': True},
                    'old_string': 'Original body.',
                    'new_string': 'Polished body.',
                }],
            }

        def apply_string_replace(self, *_args):
            path = Path(self.artifact_store) / 'revised_document.md'
            path.write_text('# Title\n\nPolished body.\n', encoding='utf-8')
            return {'revised_document_md': str(path)}

    monkeypatch.setattr(tools, 'AutoModel', lambda **_kwargs: object())
    monkeypatch.setattr(tools, 'WriterRevisionTools', FakeWriterRevisionTools)
    source_path = tmp_path / 'revised_document.md'
    source_path.write_text('# Title\n\nOriginal body.\n', encoding='utf-8')

    result = tools.writer_preview_selection_rewrite(
        artifact={
            'path': str(source_path),
            'filename': source_path.name,
            'size': source_path.stat().st_size,
        },
        instruction='润色',
        selection={'type': 'markdown', 'selected_text': 'Original body.'},
        artifact_store=str(tmp_path),
        slot='draft_document',
    )

    artifact = result['artifact']['value']
    assert artifact['filename'] == 'draft_document.md'
    assert Path(artifact['path']).name == 'draft_document.md'
    assert Path(artifact['path']).read_text(encoding='utf-8') == (
        '# Title\n\nPolished body.\n'
    )


def test_selection_rewrite_uses_slot_ir_artifact_filename(monkeypatch, tmp_path):
    tools = _load_tools_module()

    class FakeWriterRevisionTools:
        def __init__(self, *, llm, artifact_store):
            self.artifact_store = artifact_store

        def generate_patch_set(self, *_args):
            return {'artifact_path': str(tmp_path / 'patch.json')}

    document = {
        'document_id': 'doc-1',
        'stage': 'final',
        'blocks': [{
            'node_id': 'paragraph-1',
            'type': 'paragraph',
            'content': 'Original body.',
            'stage': 'final',
        }],
    }
    monkeypatch.setattr(tools, 'AutoModel', lambda **_kwargs: object())
    monkeypatch.setattr(tools, 'WriterRevisionTools', FakeWriterRevisionTools)
    monkeypatch.setattr(
        tools,
        'load_artifact_json',
        lambda *_args: tools.PatchSet(target_doc_id='doc-1', hunks=[]),
    )
    monkeypatch.setattr(
        tools,
        'apply_patch_to_ir',
        lambda source, _patch: (source, None),
    )

    result = tools.writer_preview_selection_rewrite(
        artifact={'data': document},
        instruction='Polish',
        selection={'type': 'ir', 'node_id': 'paragraph-1'},
        artifact_store=str(tmp_path),
        slot='draft_document',
    )

    artifact = result['artifact']['value']
    assert artifact['filename'] == 'draft_document.lmd'
    assert Path(artifact['path']).name == 'draft_document.lmd'


def test_load_local_lmd_rejects_invalid_document(monkeypatch, tmp_path):
    tools = _load_tools_module()
    source = tmp_path / 'broken.lmd'
    source.write_text('{"stage":"outline","blocks":[]}', encoding='utf-8')
    context = SimpleNamespace(
        workspace_path=str(tmp_path),
        params={'history_files_per_turn': {'turn-1': [str(source)]}},
    )
    monkeypatch.setattr(tools, 'require_context', lambda: context)

    with pytest.raises(ValueError, match=r'Cannot parse LMD file broken\.lmd'):
        tools.writer_load_local_document('broken.lmd')


def test_load_local_lmd_removes_cloud_binding(monkeypatch, tmp_path):
    tools = _load_tools_module()
    source = tmp_path / 'bound.lmd'
    source.write_text(json.dumps({'document_id': 'local-doc', 'blocks': [{
        'node_id': 'p1', 'type': 'paragraph', 'content': 'body',
        'provider_binding': {'block_id': 'cloud-block'},
    }], 'provider_binding': {'provider': 'feishu', 'document_id': 'cloud-doc'},
        'metadata': {'source': {'uri': 'https://example.feishu.cn/docx/cloud-doc'}},
    }), encoding='utf-8')
    context = SimpleNamespace(
        workspace_path=str(tmp_path),
        params={'history_files_per_turn': {'turn-1': [str(source)]}},
    )
    monkeypatch.setattr(tools, 'require_context', lambda: context)

    loaded = tools._read_json_file(tools.writer_load_local_document('bound.lmd'))

    assert loaded['document_id'] == 'local-doc'
    assert not loaded.get('provider_binding')
    assert 'source' not in loaded.get('metadata', {})
    assert not loaded['blocks'][0].get('provider_binding')
