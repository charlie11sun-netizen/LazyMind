"""Compact plans use real observations, preserve every result, and recover individual cells."""
import copy
import json
from collections import Counter
from unittest.mock import Mock

import pytest

from lazymind.document_tools.paper.analysis import model, pipeline, recovery, program
from lazymind.document_tools.paper.analysis.planning import compile_plan
from lazymind.document_tools.paper.analysis.plan import validate_plan
from lazymind.document_tools.paper.analysis.schema import normalize_input, ContractError
from lazymind.document_tools.paper.analysis.requirements import request_parts, result_ids
from lazymind.document_tools.paper.analysis.views import materialize
from lazymind.document_tools.paper.analysis.reference import reference


def batch_case():
    raw = dict(topic='分组指标', request='按方法和数据集分别报告两个指标的均值。',
        data=dict(columns=['method', 'dataset', 'score', 'cost'],
                  column_types=['string', 'integer', 'number', 'number'], units=[None, None, 'point', 's'],
                  rows=[[f'm{m}', d, float(m + d + i), float(10 * m + d + 2 * i)]
                        for m in range(10) for d in range(5) for i in range(3)]))
    refs = [dict(table_id='data', column=c) for c in ('score', 'cost')]
    compact = dict(targets=[dict(id='t', question='每组指标如何？', estimand='均值', scope='实际分组', columns=refs)],
                   methods=['分别计算各实际分组的指标均值'],
                   tasks=[dict(id='summary', target_id='t', table_id='data', kind='summary', operation='mean',
                               group_by=['method', 'dataset'], metrics=['score', 'cost'])])
    compact['requirements'] = [dict(id='req', source_ids=[p['id'] for p in request_parts(raw)],
        description=raw['request'], results=[dict(task_id='summary')])]
    return normalize_input(raw), compact


def execute(compact, inputs):
    plan = validate_plan(compile_plan(compact, inputs), inputs)
    views = materialize(inputs, plan['joins'])
    models = {m['id']: m for m in plan['models']}
    return plan, {j['id']: reference(j, inputs, models, views) for j in plan['jobs']}


def test_one_model_task_delivers_all_100_real_calculations(monkeypatch):
    inputs, compact = batch_case()
    call = Mock(return_value=compact)
    monkeypatch.setattr(model, 'call_model', call)
    monkeypatch.setattr(model, 'interpret_results', lambda *args: ('描述实际分组结果。', {}))
    with pipeline.capture_execution() as traces:
        result = pipeline.run(inputs)
    plan = traces[0]['plan']
    assert call.call_count == 1
    assert len(plan['task_groups']) == 1 and len(result['analysis']['findings']) == 100
    for finding, job in zip(result['analysis']['findings'], plan['jobs']):
        group = {f['column']: f['value'] for f in job['filters']}
        m, d = int(group['method'][1:]), group['dataset']
        assert finding['value'] == (m + d + 1 if job['column'] == 'score' else 10 * m + d + 2)
        assert finding['sample_size']['n'] == 3
        assert finding['unit'] == ('point' if job['column'] == 'score' else 's')
    assert len(json.dumps(compact)) < len(json.dumps(plan)) / 10
    prompt = call.call_args.args[0]
    payload = json.loads(prompt[prompt.index('{"available_libraries":'):])
    assert payload['schema']['title'] == 'CompactPlan'
    assert 'tasks' in payload['schema']['properties'] and 'jobs' not in payload['schema']['properties']


def test_observed_combinations_and_ids_are_independent_of_row_order():
    inputs, compact = batch_case()
    inputs['data']['rows'] = [['a', 1, 2., 3.], ['b', 2, 5., 8.], [None, 3, 7., 9.]]
    first = compile_plan(compact, inputs)
    inputs['data']['rows'].reverse()
    second = compile_plan(compact, inputs)
    assert first['jobs'] == second['jobs']
    assert len(first['jobs']) == 6  # 3 observed combinations, not 3 x 3 x 2.
    assert any(f['value'] is None for job in first['jobs'] for f in job['filters'])


def test_missing_values_are_handled_per_metric_not_entire_batch():
    inputs, compact = batch_case()
    inputs['data']['rows'] = [['a', 1, 2., None], ['a', 1, 4., 10.], ['a', 1, None, 20.]]
    compact['tasks'][0]['missing_policy'] = 'complete_case'
    plan, values = execute(compact, inputs)
    by_metric = {j['column']: values[j['id']] for j in plan['jobs']}
    assert by_metric['score'][0]['value'] == 3.
    assert by_metric['cost'][0]['value'] == 15.
    assert all(value[1]['n'] == 2 for value in values.values())


@pytest.mark.parametrize('kind,first,second', [
    ('number', 1, 1.0),
    ('datetime', '2026-10-26T08:00:00+08:00', '2026-10-26T00:00:00Z'),
])
def test_equivalent_group_values_do_not_duplicate_results(kind, first, second):
    inputs, compact = batch_case()
    inputs['data']['column_types'][1] = kind
    inputs['data']['rows'] = [['a', first, 2., 3.], ['a', second, 4., 5.]]
    plan, values = execute(compact, normalize_input(inputs))
    assert len(plan['jobs']) == 2
    assert all(value[1]['n'] == 2 for value in values.values())
    assert sorted(value[0]['value'] for value in values.values()) == [3., 4.]
    before = compile_plan(compact, inputs)
    inputs['data']['rows'].reverse()
    assert compile_plan(compact, inputs)['jobs'] == before['jobs']


def test_comparison_strata_apply_to_both_groups_and_keep_baseline_only_cells():
    inputs, compact = batch_case()
    inputs['data']['rows'] = [['a', 1, 5., 1.], ['b', 1, 2., 1.], ['b', 2, 8., 1.]]
    compact['tasks'][0].update(kind='comparison', operation='difference', metrics=['score'], group_by=['dataset'],
        filters=[dict(column='method', operator='eq', value='a')],
        baseline_filters=[dict(column='method', operator='eq', value='b')])
    plan = validate_plan(compile_plan(compact, inputs), inputs)
    assert len(plan['jobs']) == 2
    values, failures = [], []
    for job in plan['jobs']:
        assert job['filters'][-1] == job['baseline_filters'][-1]
        try:
            values.append(reference(job, inputs, {})[0]['value'])
        except ValueError as exc:
            failures.append(str(exc))
    assert values == [3.] and len(failures) == 1


def test_correlation_matrix_does_not_duplicate_reversed_pairs_or_tests():
    inputs, compact = batch_case()
    compact['tasks'] = [dict(id='matrix', target_id='t', table_id='data', kind='correlation',
                             operation='pearson', variables=['score', 'cost'], diagonal=True)]
    compact['requirements'][0]['results'][0]['task_id'] = 'matrix'
    plan, values = execute(compact, inputs)
    assert len(values) == 3
    assert sum(j['column'] != j['other_column'] for j in plan['jobs']) == 1
    assert all(value[0]['p_value'] is None for value in values.values())


def test_figure_intent_expands_every_computed_metric_after_statistics():
    inputs, compact = batch_case()
    compact['figure_requests'] = [dict(id='plot', purpose='每个实际分组', task_ids=['summary'])]
    plan = validate_plan(compile_plan(compact, inputs), inputs)
    assert plan['figures'] == []
    assert result_ids(plan, ['summary']) == [job['id'] for job in plan['jobs']]


def test_compact_batch_reaches_native_figure(monkeypatch):
    from lazymind.document_tools.paper.plotting import analysis as plots
    inputs, compact = batch_case()
    inputs['data']['rows'] = inputs['data']['rows'][:6]
    figure = dict(id='plot', title='Metrics', caption='Observed group means.', kind='bar',
        style='bar_grouped_hatch', finding_ids=['summary'], columns=compact['targets'][0]['columns'],
        instructions='Separate the metrics into panels.', data_mapping=dict(category_column='data.dataset',
            series_column='data.method', value_labels=dict(score='Score', cost='Cost')))
    compact['figure_requests'] = [dict(id='plot', purpose='Observed group means', task_ids=['summary'])]
    monkeypatch.setattr(model, 'plan_figure', lambda inputs, analysis, plan, *args:
        {**figure, 'finding_ids': [f['id'] for f in analysis['findings']]})
    monkeypatch.setattr(model, 'call_model', Mock(return_value=compact))
    monkeypatch.setattr(model, 'interpret_results', lambda *args: ('描述实际分组结果。', {}))
    monkeypatch.setattr(plots, 'call_code_model', Mock(side_effect=AssertionError('Unnecessary code generation')))
    output = pipeline.run(inputs)
    assert len(output['analysis']['findings']) == 4
    assert len(output['charts']) == 1 and '<svg' in output['charts'][0]['svg']
    assert set(output['charts'][0]['finding_ids']) == {f['id'] for f in output['analysis']['findings']}


@pytest.mark.parametrize('kind', ['regression', 'probability'])
def test_group_models_are_distinct_and_coefficients_share_only_their_own_fit(kind):
    from lazymind.document_tools.paper.analysis.plan_bindings import evidence_catalog
    inputs, compact = batch_case()
    inputs['data']['rows'] = [[g, 1, float(i), float(offset + 2 * i)]
                              for g, offset in [('a', 1), ('b', 5)] for i in range(4)]
    inputs['study_design'] = dict(description='各行是独立观测，没有重复测量。')
    inputs = normalize_input(inputs)
    evidence = [evidence_catalog(inputs)[0]['id']]
    task = dict(id='fits', target_id='t', table_id='data', kind=kind, group_by=['method'],
                model_id='model', design_evidence_refs=evidence)
    statistical_model = dict(id='model', target_ids=['t'], method='Group models',
                             formula='cost ~ 1 + score', likelihood='Normal', priors=[])
    if kind == 'regression':
        task.update(column='cost', predictors=['score'], coefficients=['intercept', 'score'])
    else:
        task.update(operation='beta_mean', column='score')
        for row in inputs['data']['rows']:
            row[2] = float(row[2] > (0 if row[0] == 'a' else 2))
        statistical_model.update(formula='score ~ Bernoulli(theta)', likelihood='Bernoulli',
            priors=[dict(parameter='theta', distribution='beta', parameters=dict(alpha=1., beta=1.),
                         source='supplied', source_table_ids=[], rationale='固定提供的先验。')])
    compact.update(tasks=[task], models=[statistical_model])
    compact['requirements'][0]['results'][0]['task_id'] = 'fits'
    plan, results = execute(compact, inputs)
    assert len(plan['models']) == 2
    by_group = {}
    for job in plan['jobs']:
        group = job['filters'][0]['value']
        by_group.setdefault(group, set()).add(job['model_id'])
        expected = ((1. if group == 'a' else 5.) if job['coefficient'] == 'intercept' else 2.)
        if kind == 'probability':
            expected = 4 / 6 if group == 'a' else 2 / 6
        assert results[job['id']][0]['value'] == pytest.approx(expected)
    assert all(len(ids) == 1 for ids in by_group.values()) and by_group['a'] != by_group['b']


def test_code_task_uses_new_interface_through_actual_model_entry(monkeypatch):
    inputs, compact = batch_case()
    inputs['data']['columns'][2] = '实际测量'
    compact['targets'][0]['columns'][0]['column'] = '实际测量'
    compact['tasks'] = [dict(id='custom', target_id='t', table_id='data', kind='code', operation='quantile',
        column='实际测量', procedure='对实际输入计算中位数。', outputs=[dict(id='median', label='中位数')])]
    compact['requirements'][0]['results'][0]['task_id'] = 'custom'
    monkeypatch.setattr(model, 'call_model', Mock(return_value=compact))
    monkeypatch.setattr(model, 'interpret_results', lambda *args: ('描述当前观测。', {}))
    code_model = Mock(return_value={'code': '''def analyze(frames, params, rng):
    data = frames['analysis']
    emit('median', data[params['column']].median(), data.index, unit='point')
'''})
    monkeypatch.setattr(program, 'call_code_model', code_model)
    result = pipeline.run(inputs)
    assert result['analysis']['findings'][0]['value'] == 7.5
    context = json.loads(code_model.call_args.args[0].split('INPUT_JSON:\n', 1)[1])
    assert set(context['interface']['frames']) == {'analysis'}
    assert context['params']['column'] == '实际测量'
    assert 'open' in context['code_policy']['forbidden_names']
    assert 'read_csv' in context['code_policy']['forbidden_attributes']


def test_old_figure_planning_protocol_is_rejected():
    inputs, compact = batch_case()
    compact['figures'] = [dict(id='pending', finding_ids=['summary'])]
    with pytest.raises(ValueError):
        compile_plan(compact, inputs)


@pytest.mark.parametrize('fault', ['duplicate_metrics', 'unknown_group', 'too_many', 'legacy'])
def test_invalid_batches_fail_without_truncation_or_legacy_fallback(fault, monkeypatch):
    inputs, compact = batch_case()
    if fault == 'duplicate_metrics':
        compact['tasks'][0]['metrics'] *= 2
    elif fault == 'unknown_group':
        compact['tasks'][0]['group_by'] = ['invented']
    elif fault == 'too_many':
        monkeypatch.setattr('lazymind.document_tools.paper.analysis.planning.MAX_CALCULATIONS', 99)
    else:
        compact['jobs'] = compact.pop('tasks')
    with pytest.raises(ValueError):
        compile_plan(compact, inputs)


def test_failed_cell_does_not_recompute_successful_siblings_across_attempts_and_runs(monkeypatch, tmp_path):
    inputs, compact = batch_case()
    inputs['data']['rows'] = inputs['data']['rows'][:3]
    compact['tasks'][0]['group_by'] = []
    call = Mock(return_value=compact)
    monkeypatch.setattr(model, 'call_model', call)
    monkeypatch.setattr(model, 'repair_plan', lambda inputs, candidate, *args, **kwargs: copy.deepcopy(candidate))
    counts, fail = Counter(), True
    original = pipeline.reference
    def compute(job, *args):
        counts[job['column']] += 1
        if job['column'] == 'cost' and fail:
            raise ValueError('temporary cell failure')
        return original(job, *args)
    monkeypatch.setattr(pipeline, 'reference', compute)
    with recovery.workspace(tmp_path):
        store = recovery.Store(inputs, {})
        with pytest.raises(ContractError):
            pipeline.plan_statistics(inputs, 1, store)
        assert counts == {'score': 1, 'cost': 2}
        fail = False
        plan, values, _ = pipeline.plan_statistics(inputs, 1, store)
    assert counts == {'score': 1, 'cost': 3}
    assert call.call_count == 1 and len(values) == 2


def test_code_interface_rejects_object_access_with_actionable_feedback():
    with pytest.raises(ValueError, match='params 是字典'):
        program.validate_code('def analyze(frames, params, rng):\n    x = params.column\n', {})
    with pytest.raises(ValueError, match='无需类型探测或反射'):
        program.validate_code('def analyze(frames, params, rng):\n    x = getattr(params, "column")\n', {})
    with pytest.raises(ValueError, match='analyze\\(frames, params, rng\\)'):
        program.validate_code('def analyze(data, task, rng):\n    pass\n', {})
