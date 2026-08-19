from lazymind.chat.engine.prompts.writer_structure import (
    resolve_writer_structure_route,
    writer_structure_route_from_ask_answer,
)


def test_writer_structure_route_accepts_model_decision():
    seen = {}

    def classifier(prompt):
        seen['prompt'] = prompt
        return '{"structure_mode":"flat"}'

    assert resolve_writer_structure_route('写一篇文章', classifier=classifier) == 'flat'
    assert 'Never infer article length from topic complexity' in seen['prompt']


def test_writer_structure_route_accepts_direct_json_object():
    assert resolve_writer_structure_route(
        '写一篇连续正文',
        classifier=lambda _prompt: {'structure_mode': 'flat'},
    ) == 'flat'


def test_writer_structure_route_resolves_explicit_short_length_without_llm():
    called = False

    def classifier(_prompt):
        nonlocal called
        called = True
        return '{"structure_mode":"clarify"}'

    assert resolve_writer_structure_route(
        '写一篇面向普通消费者的文章，1000字', classifier=classifier,
    ) == 'flat'
    assert called is False


def test_writer_structure_route_keeps_long_request_on_classifier_path():
    called = False

    def classifier(_prompt):
        nonlocal called
        called = True
        return '{"structure_mode":"sectioned"}'

    assert resolve_writer_structure_route(
        '写一篇2000字的文章', classifier=classifier,
    ) == 'sectioned'
    assert called is True


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
