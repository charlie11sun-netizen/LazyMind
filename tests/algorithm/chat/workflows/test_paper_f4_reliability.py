"""Offline recovery and invariant tests; no live model or service calls."""
import base64
import copy
import json
from unittest.mock import Mock

import pytest

from lazymind.document_tools.paper.analysis import model, pipeline, recovery
from lazymind.document_tools.paper.analysis.interpretation import interpret
from lazymind.document_tools.paper.analysis.plan import validate_plan
from lazymind.document_tools.paper.analysis.plan_repair import PlanRepairError, apply_updates
from lazymind.document_tools.paper.analysis.schema import ContractError, normalize_input
from lazymind.document_tools.paper.plotting import analysis as plots


@pytest.fixture
def case(monkeypatch):
    raw = dict(topic='组间分数比较', request='比较当前分组的平均分数。',
               data=dict(columns=['group', 'score'], column_types=['string', 'number'],
                         units=[None, 'point'], rows=[['a', 1.], ['a', 3.], ['b', 4.], ['b', 6.]]))
    columns = [dict(table_id='data', column='score')]
    plan = dict(targets=[dict(id='scores', question='各组分数如何？', estimand='均值', scope='当前数据', columns=columns)],
                models=[], methods=['分组均值'], limitations=['仅描述当前观测。'],
                jobs=[dict(id=group, target_id='scores', table_id='data', column='score', operation='mean',
                           filters=[dict(column='group', operator='eq', value=group)]) for group in ('a', 'b')],
                figures=[])
    answer = dict(summary='第二组平均分数为 5.0，高于第一组。',
                  answers=[dict(target_id='scores', conclusion='仅描述当前观测，不能解释原因。',
                                finding_ids=['a', 'b'], key_finding_ids=['a', 'b'])],
                  mechanism_explanation='数据不足以解释机制。', limitations=['不能外推。'])
    make_plan = Mock(side_effect=lambda *args: copy.deepcopy(plan))
    explain = Mock(return_value=answer)
    monkeypatch.setattr(model, 'make_plan', make_plan)
    monkeypatch.setattr(model, 'interpret_results', lambda inputs, analysis, locked, attempts:
                        interpret(inputs, analysis, locked, attempts, explain))
    monkeypatch.setattr(plots, 'call_code_model', Mock(side_effect=AssertionError('Unexpected model call')))
    monkeypatch.delenv('LAZYMIND_PAPER_PLOT_SKILLS', raising=False)
    monkeypatch.setenv('LAZYMIND_PAPER_ANALYSIS_ATTEMPTS', '2')
    return raw, plan, make_plan, explain


def add_figures(plan, *ids):
    plan['figures'] = [dict(id=identifier, title='Scores', caption='Observed group means.',
                           kind='bar', style='bar_grouped_hatch', finding_ids=['a', 'b'],
                           columns=copy.deepcopy(plan['targets'][0]['columns']), instructions='Compare group means.',
                           data_mapping=dict(category_column='data.group')) for identifier in ids]


def test_registered_statistics_need_no_generated_program_or_execution_environment(case, monkeypatch):
    from lazymind.document_tools.paper.execution import container
    raw, _, make_plan, _ = case
    monkeypatch.setattr(container, 'execute', Mock(side_effect=AssertionError('Unnecessary sandbox')))
    with pipeline.capture_execution() as traces:
        output = pipeline.run(raw)
    assert [finding['value'] for finding in output['analysis']['findings']] == [2., 5.]
    assert output['charts'] == [] and make_plan.call_count == 1
    assert traces[0]['executor'] == 'hybrid'
    assert all(d['name'] == '固定统计实现与数据作用域检查' for d in output['analysis']['diagnostics'])


def test_invalid_data_still_fails_before_planning(case):
    raw, _, make_plan, _ = case
    raw['data']['rows'][0].pop()
    with pytest.raises(ContractError, match='第 1 行'):
        pipeline.run(raw)
    make_plan.assert_not_called()


def test_only_unambiguous_representation_is_normalized(case):
    raw, plan, _, _ = case
    add_figures(plan, 'scores')
    plan['targets'][0]['columns'] *= 2
    plan['figures'][0]['data_mapping']['value_labels'] = {'a': 'Score', 'b': 'Score'}
    before = copy.deepcopy(plan)
    normalized = validate_plan(plan, normalize_input(raw))
    assert len(normalized['targets'][0]['columns']) == 1
    assert normalized['figures'][0]['data_mapping']['value_labels'] == {'score': 'Score'}
    assert plan == before  # Normalization never mutates the caller's facts/candidate.
    plan['figures'][0]['data_mapping']['value_labels']['b'] = 'Different quantity'
    with pytest.raises(PlanRepairError, match='互相冲突'):
        validate_plan(plan, normalize_input(raw))


def test_chinese_presentation_does_not_block_statistics(case):
    raw, plan, _, _ = case
    add_figures(plan, 'scores')
    plan['figures'][0].update(title='分数比较', caption='当前观测的均值。')
    assert validate_plan(plan, normalize_input(raw))['figures'][0]['title'] == '分数比较'


def test_figure_only_patch_and_advertised_schema_agree(case, monkeypatch):
    raw, plan, _, _ = case
    add_figures(plan, 'scores')
    inputs = normalize_input(raw)
    plan = validate_plan(plan, inputs)
    issues = [dict(figure_id='scores', field='data_mapping', message='repair mapping')]
    response = dict(figure_updates=[dict(figure_id='scores', figure=plan['figures'][0])])
    call = Mock(return_value=response)
    monkeypatch.setattr(model, 'call_model', call)
    assert model.repair_plan(inputs, plan, issues)['jobs'] == plan['jobs']
    prompt = call.call_args.args[0]
    payload = json.loads(prompt[prompt.index('{"plan":'):])
    assert payload['schema']['title'] == 'PlanPatch'
    assert set(payload['schema']['properties']) == {'updates', 'model_updates', 'figure_updates'}
    with pytest.raises(ValueError):
        apply_updates(plan, {**response, 'unexpected': []}, issues)


def test_patch_failure_is_forwarded_and_last_candidate_is_preserved(case, monkeypatch):
    raw, plan, make_plan, _ = case
    plan['jobs'][0]['unit'] = 'wrong'
    contexts = []
    original = model.repair_plan
    call = Mock(side_effect=[{}, dict(updates=[dict(job_id='a', field='unit', value='point')])])
    monkeypatch.setattr(model, 'call_model', call)

    def repair(inputs, candidate, issues, previous_error=''):
        contexts.append((copy.deepcopy(candidate), previous_error))
        return original(inputs, candidate, issues, previous_error)
    monkeypatch.setattr(model, 'repair_plan', repair)
    assert pipeline.run(raw)['analysis']['findings'][0]['value'] == 2.
    assert make_plan.call_count == 1 and len(contexts) == 2
    assert contexts[0][0] == contexts[1][0]
    assert '局部修复缺少指定字段' in contexts[1][1]


def test_statistical_repair_preserves_unvalidated_figures(case, monkeypatch):
    raw, plan, _, _ = case
    plan['jobs'][0]['unit'] = 'wrong'
    plan['figures'] = [dict(id='pending', style='bar')]
    monkeypatch.setattr(model, 'call_model', Mock(return_value=dict(
        updates=[dict(job_id='a', field='unit', value='point')])))
    locked, _, _ = pipeline.plan_statistics(normalize_input(raw), 2)
    assert locked['jobs'][0]['unit'] == 'point'
    assert locked['figures'] == plan['figures']


@pytest.mark.parametrize('fault', ['evidence', 'dimensions', 'source'])
def test_statistical_and_data_identity_guards_remain(case, fault):
    raw, plan, _, _ = case
    add_figures(plan, 'scores')
    if fault == 'evidence':
        plan['jobs'][0].update(interval_level=.95, design_evidence_refs=['invented'])
    elif fault == 'dimensions':
        plan['figures'][0]['data_mapping'].update(series_column='data.group')
    else:
        plan['targets'][0]['columns'][0]['column'] = 'missing'
    with pytest.raises(ValueError):
        validate_plan(plan, normalize_input(raw))


def fake_chart(spec):
    exports = {extension: base64.b64encode(magic + b'offline fixture').decode()
               for extension, magic in [('png', b'\x89PNG\r\n\x1a\n'), ('pdf', b'%PDF-')]}
    return dict(svg='<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0 L1 1"/></svg>', exports=exports), {}


def test_retry_reuses_statistics_interpretation_and_successful_sibling_figure(case, monkeypatch, tmp_path):
    raw, plan, make_plan, explain = case
    add_figures(plan, 'good', 'retry_me')
    counts = dict(good=0, retry_me=0)

    def draw(bound, measured, spec, *args, **kwargs):
        counts[spec['id']] += 1
        if spec['id'] == 'retry_me' and counts[spec['id']] == 1:
            raise RuntimeError('temporary plot failure')
        return fake_chart(spec)
    monkeypatch.setattr(plots, 'draw_one', draw)
    with recovery.workspace(tmp_path / 'user' / 'first', 'first', 'conversation'):
        with pytest.raises(ContractError, match='统计计算已完成') as error:
            pipeline.run(raw)
    assert error.value.retryable
    assert counts == dict(good=1, retry_me=1)
    # A new task workspace in the same user's conversation resumes the same request.
    with recovery.workspace(tmp_path / 'user' / 'second', 'second', 'conversation'):
        with pipeline.capture_execution() as traces:
            output = pipeline.run(raw)
    assert [chart['id'] for chart in output['charts']] == ['good', 'retry_me']
    assert counts == dict(good=1, retry_me=2)
    assert make_plan.call_count == explain.call_count == 1
    assert traces[0]['statistics_reused'] and traces[0]['figures'][0]['reused']
    # A different input must invalidate all stages.
    changed = copy.deepcopy(raw)
    changed['data']['rows'][0][1] = 7.
    with recovery.workspace(tmp_path / 'user' / 'third', 'third', 'conversation'):
        output = pipeline.run(changed)
    assert output['analysis']['findings'][0]['value'] == 5.
    assert make_plan.call_count == 2 and counts == dict(good=2, retry_me=3)


def test_interpretation_failure_does_not_cancel_or_discard_figures(case, monkeypatch, tmp_path):
    raw, plan, make_plan, explain = case
    add_figures(plan, 'figure')
    original = model.interpret_results
    monkeypatch.setattr(model, 'interpret_results', Mock(side_effect=RuntimeError('temporary transport failure')))
    draw = Mock(side_effect=lambda bound, measured, spec, *args, **kwargs: fake_chart(spec))
    monkeypatch.setattr(plots, 'draw_one', draw)
    with recovery.workspace(tmp_path):
        with pytest.raises(ContractError, match='interpretation'):
            pipeline.run(raw)
        monkeypatch.setattr(model, 'interpret_results', original)
        pipeline.run(raw)
    assert make_plan.call_count == draw.call_count == explain.call_count == 1


def test_recovery_is_scoped_and_corrupt_snapshots_are_recomputed(case, tmp_path):
    raw, _, make_plan, _ = case
    with recovery.workspace(tmp_path / 'user1' / 'task1', 'task1', 'conversation'):
        pipeline.run(raw)
    for file in tmp_path.rglob('*.json'):
        file.write_text('{invalid')
    with recovery.workspace(tmp_path / 'user1' / 'task2', 'task2', 'conversation'):
        pipeline.run(raw)
    with recovery.workspace(tmp_path / 'user2' / 'task1', 'task1', 'conversation'):
        pipeline.run(raw)
    assert make_plan.call_count == 3


def test_recovery_version_and_plot_resources_invalidate_snapshots(case, monkeypatch, tmp_path):
    raw, *_ = case
    with recovery.workspace(tmp_path):
        first = recovery.Store(raw, {'plot_resources': 'original'})
        first.save('test', 'key', {'ok': True})
        assert first.load('test', 'key') == {'ok': True}
        assert recovery.Store(raw, {'plot_resources': 'changed'}).load('test', 'key') is None
        monkeypatch.setattr(recovery, 'implementation', lambda: 'different code')
        assert recovery.Store(raw, {'plot_resources': 'original'}).load('test', 'key') is None


@pytest.mark.parametrize('style', ['bar_grouped_hatch', 'point_interval'])
def test_standard_figures_render_without_model_code(case, style):
    raw, plan, _, _ = case
    add_figures(plan, 'standard')
    plan['figures'][0]['style'] = style
    plan['figures'][0]['data_mapping'].update(value_label='得分', category_label='分组')
    with pipeline.capture_execution() as traces:
        output = pipeline.run(raw)
    assert '<svg' in output['charts'][0]['svg']
    assert traces[0]['figures'][0]['renderer'] == 'registered_template'
    assert set(traces[0]['figures'][0]['exports']) == {'png', 'pdf'}


def test_model_can_customize_a_standard_layout(case, monkeypatch):
    raw, plan, _, _ = case
    add_figures(plan, 'custom_layout')
    plan['figures'][0]['render_mode'] = 'code'
    generated = Mock(return_value={'code': '''import matplotlib.pyplot as plt

def draw(data, statistics, spec):
    layout = spec['style_contract']['layout']
    fig, ax = plt.subplots(figsize=tuple(layout['single_inches']))
    fig.subplots_adjust(**layout['single_panel'])
    draw_reference_panel(ax, **data['figure']['panels'][0]['arguments'])
    return fig
'''})
    monkeypatch.setattr(plots, 'call_code_model', generated)
    with pipeline.capture_execution() as traces:
        output = pipeline.run(raw)
    assert '<svg' in output['charts'][0]['svg']
    assert generated.call_count == 1 and traces[0]['figures'][0]['renderer'] == 'generated'
