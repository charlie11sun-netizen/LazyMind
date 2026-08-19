from lazymind.chat.engine.prompts.writer_structure import (
    resolve_writer_structure_route,
    writer_structure_route_from_ask_answer,
)


def test_writer_structure_route_accepts_model_decision():
    seen = {}

    def classifier(prompt):
        seen['prompt'] = prompt
        return '{"structure_mode":"flat"}'

    assert resolve_writer_structure_route('写一篇 800 字文章', classifier=classifier) == 'flat'
    assert 'Never infer article length from topic complexity' in seen['prompt']


def test_writer_structure_route_accepts_direct_json_object():
    assert resolve_writer_structure_route(
        '写一篇连续正文',
        classifier=lambda _prompt: {'structure_mode': 'flat'},
    ) == 'flat'


def test_writer_structure_route_prompt_covers_multilingual_semantic_examples():
    seen = {}

    def classifier(prompt):
        seen['prompt'] = prompt
        return '{"structure_mode":"flat"}'

    assert resolve_writer_structure_route('请写一篇文章', classifier=classifier) == 'flat'
    assert '写一篇1000字的文章' in seen['prompt']
    assert '写一篇1000字的文章，要有小标题' in seen['prompt']
    assert '写一篇1000字的文章，先列大纲再写' in seen['prompt']
    assert '写一篇2000字的文章，不要小标题' in seen['prompt']
    assert 'write a 900-word article with subheadings' in seen['prompt']
    assert 'write a 900-word article and provide an outline first' in seen['prompt']
    assert 'write a 2000-word report without sections' in seen['prompt']


def test_writer_structure_route_prompt_prioritizes_explicit_structure_over_length():
    seen = {}

    def classifier(prompt):
        seen['prompt'] = prompt
        return {'structure_mode': 'sectioned'}

    assert resolve_writer_structure_route(
        '写一篇1000字的文章，要有小标题', classifier=classifier,
    ) == 'sectioned'
    assert 'An explicit presentation requirement has priority over article length' in seen['prompt']
    assert 'at or below 1200 Chinese characters/words' in seen['prompt']
    assert 'above 1200 -> sectioned' in seen['prompt']
    assert 'presentation requirements contradict one another' in seen['prompt']


def test_writer_structure_route_does_not_treat_outline_as_sectioned():
    seen = {}

    def classifier(prompt):
        seen['prompt'] = prompt
        return {'structure_mode': 'flat'}

    assert resolve_writer_structure_route(
        '写一篇1000字的文章，先列大纲再写', classifier=classifier,
    ) == 'flat'
    assert 'A request for an outline is a' in seen['prompt']
    assert 'planning requirement' in seen['prompt']


def test_writer_structure_route_uses_clarification_decision():
    response = '```json\n{"structure_mode":"sectioned"}\n```'
    assert resolve_writer_structure_route(
        'Original request: 写一篇文章\nClarification answer: 分章节展开',
        classifier=lambda _prompt: response,
    ) == 'sectioned'


def test_writer_structure_route_fails_closed_to_ask_user():
    assert resolve_writer_structure_route(
        '写一篇文章', classifier=lambda _prompt: '{}',
    ) == 'clarify'
    assert resolve_writer_structure_route(
        '写一篇文章', classifier=lambda _prompt: 'not json',
    ) == 'clarify'


def test_writer_structure_route_maps_fixed_ask_user_answer():
    assert writer_structure_route_from_ask_answer(
        '您希望文章使用哪种结构？: 连续正文（不使用小标题）',
    ) == 'flat'
    assert writer_structure_route_from_ask_answer(
        '您希望文章使用哪种结构？：分章节展开',
    ) == 'sectioned'
    assert writer_structure_route_from_ask_answer('写一篇连续正文') is None
