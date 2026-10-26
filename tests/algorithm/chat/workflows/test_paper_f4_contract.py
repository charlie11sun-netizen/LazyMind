"""Behavioral checks for the new F4 protocol, scoped validation and recovery decisions."""
import copy
import json
from unittest.mock import Mock
import pytest
from lazymind.document_tools.paper.analysis import model, pipeline, program, recovery
from lazymind.document_tools.paper.analysis.schema import normalize_input, ContractError
from lazymind.document_tools.paper.analysis.planning import compile_plan
from lazymind.document_tools.paper.analysis.plan import validate_plan
from lazymind.document_tools.paper.analysis.profiles import query_data
from lazymind.document_tools.paper.analysis.requirements import request_parts, check_results
from lazymind.document_tools.paper.analysis.plan_repair import apply_updates
from lazymind.document_tools.paper.plotting import analysis as plots


def case():
    inputs = normalize_input(dict(topic='检验', request='报告 score 均值。',
        data=dict(columns=['id', 'score'], column_types=['integer', 'number'],
                  units=[None, 'point'], rows=[[i, float(i)] for i in range(1, 31)])))
    compact = dict(targets=[dict(id='t', question='均值', estimand='算术均值', scope='全部',
        columns=[dict(table_id='data', column='score')])], methods=['算术均值'],
        tasks=[dict(id='mean', kind='summary', operation='mean', table_id='data', target_id='t', metrics=['score'])],
        requirements=[dict(id='r', source_ids=[p['id'] for p in request_parts(inputs)], description='score 均值',
                           results=[dict(task_id='mean')])])
    return inputs, compact


def setup(monkeypatch, inputs, compact):
    monkeypatch.setattr(model, 'make_plan', lambda *args: compile_plan(copy.deepcopy(compact), inputs))
    monkeypatch.setattr(model, 'interpret_results', lambda *args: ('当前观测的均值。', {}))
    monkeypatch.setenv('LAZYMIND_PAPER_ANALYSIS_ATTEMPTS', '2')


def test_queries_can_read_levels_omitted_from_initial_profile(monkeypatch):
    inputs, compact = case()
    query = dict(id='q', table_id='data', kind='levels', columns=['id'], offset=20, limit=10)
    result = query_data(inputs, query)['result']
    assert result['total'] == 30 and len(result['cells']) == 10
    call = Mock(side_effect=[{'queries': [query]}, compact])
    monkeypatch.setattr(model, 'call_model', call)
    plan = model.make_plan(inputs)
    assert len(plan['jobs']) == 1
    assert 'query_results' in call.call_args.args[0] and '"total":30' in call.call_args.args[0]


def test_requirements_cannot_be_satisfied_by_an_unrelated_result():
    inputs, compact = case()
    compact['requirements'][0]['results'][0]['fields'] = ['value', 'interval']
    plan = compile_plan(compact, inputs)
    with pytest.raises(ContractError, match='interval'):
        check_results(plan, dict(findings=[dict(id='mean', value=15.5)]))
    compact['requirements'][0]['source_ids'] = ['invented']
    with pytest.raises(ValueError, match='request_parts'):
        compile_plan(compact, inputs)


def test_required_binding_overrides_model_optional_flag_and_repair_cannot_demote():
    inputs, compact = case()
    compact['tasks'][0]['required'] = False
    plan = validate_plan(compile_plan(compact, inputs), inputs)
    assert plan['jobs'][0]['required']
    replacement = {**plan['jobs'][0], 'required': False}
    with pytest.raises(ValueError, match='可选'):
        apply_updates(plan, dict(updates=[dict(job_id='mean', field=None, value=[replacement])]),
                      [dict(job_id='mean', field=None, message='error')])


def test_figure_preparation_never_validates_the_statistical_plan(monkeypatch):
    inputs, compact = case()
    plan = validate_plan(compile_plan(compact, inputs), inputs)
    from lazymind.document_tools.paper.analysis.reference import reference
    checked = {'mean': reference(plan['jobs'][0], inputs, {})}
    analysis = pipeline.build_analysis(inputs, plan, checked)
    figure = dict(id='f', title='Mean', caption='Observed mean.', kind='point', style='point_interval',
        finding_ids=['mean'], columns=plan['targets'][0]['columns'], instructions='Show the verified mean.', data_mapping=dict(category_column='finding_id'))
    plan['figures'] = [figure]
    import lazymind.document_tools.paper.analysis.plan as plan_module
    monkeypatch.setattr(plan_module, 'validate_plan', Mock(side_effect=AssertionError('full plan revalidation')))
    item = plots.prepare_recoverable(analysis, figure, inputs, plan['jobs'], plan, checked, 1)
    assert item[0]['finding_ids'] == ['mean']


def code_task(required=False, diagnostics=()):
    return dict(id='extra', kind='code', operation='median', table_id='data', target_id='t', column='score',
        procedure='Compute median', outputs=[dict(id='median', label='中位数')], required=required,
        required_diagnostics=list(diagnostics))


def test_optional_code_failure_does_not_block_required_delivery(monkeypatch):
    inputs, compact = case(); compact['tasks'].append(code_task())
    setup(monkeypatch, inputs, compact)
    monkeypatch.setattr(program, 'generate', lambda *args: 'def analyze(frames, params, rng):\n    raise ValueError("bad optional")')
    result = pipeline.run(inputs)['analysis']
    assert [f['value'] for f in result['findings']] == [15.5]
    assert any('可选分析未完成' in s for s in result['limitations'])


@pytest.mark.parametrize('required', [False, True])
def test_warning_diagnostic_is_reported_but_required_diagnostic_blocks(monkeypatch, required):
    inputs, compact = case(); compact['tasks'].append(code_task(True, ['convergence'] if required else []))
    setup(monkeypatch, inputs, compact)
    monkeypatch.setattr(program, 'generate', lambda *args: '''def analyze(frames, params, rng):
    data = frames['analysis']
    emit('median', data['score'].median(), data.index)
    diagnose('convergence', 'failed', 'Synthetic diagnostic failure')
''')
    repair = Mock(side_effect=ValueError('no valid alternative'))
    monkeypatch.setattr(model, 'repair_plan', repair)
    if required:
        with pytest.raises(ContractError, match='必要诊断'):
            pipeline.run(inputs)
        assert repair.call_count == 1
    else:
        result = pipeline.run(inputs)['analysis']
        assert len(result['findings']) == 2
        assert any('Synthetic diagnostic failure' in s for s in result['limitations'])
        repair.assert_not_called()


def test_exact_component_recovery_preserves_output_id_and_value(monkeypatch):
    inputs, compact = case()
    task = code_task(True); task.update(operation='mean', procedure='Arithmetic mean, no transformation',
                                      equivalent_component=dict(operation='mean'))
    compact['tasks'].append(task)
    setup(monkeypatch, inputs, compact)
    monkeypatch.setattr(program, 'generate', lambda *args: 'def analyze(frames, params, rng):\n    raise ValueError("bad code")')
    result = pipeline.run(inputs)['analysis']
    assert [(f['id'], f['value']) for f in result['findings']] == [('mean', 15.5), ('extra:median', 15.5)]


def test_additional_named_inputs_keep_per_table_provenance(monkeypatch):
    inputs, compact = case()
    inputs['data'] = dict(tables=[
        dict(id='measure', description='Measurements', columns=['id', 'score'], column_types=['integer','number'],
             units=[None,'point'], rows=[[1,2.],[2,4.],[3,8.]]),
        dict(id='lookup', description='Weights', columns=['id','weight'], column_types=['integer','number'],
             units=[None,None], rows=[[1,1.],[2,2.],[3,3.]])])
    inputs['data']['tables'] = [dict(id=t['id'], description=t['description'], table={k:v for k,v in t.items()
        if k not in ('id','description')}) for t in inputs['data']['tables']]
    inputs = normalize_input(inputs)
    compact['targets'][0]['columns'] = [dict(table_id=t,column=c) for t,c in
        [('measure','score'),('lookup','id'),('lookup','weight')]]
    compact['tasks'] = [dict(id='mean',kind='code',operation='weighted_mean',table_id='measure',target_id='t',
        column='score',procedure='Match by id; calculate sum(score*weight)/sum(weight).',
        inputs=dict(weights=dict(table_id='lookup',columns=['id','weight'])), outputs=[dict(id='weighted',label='加权均值')])]
    setup(monkeypatch, inputs, compact)
    monkeypatch.setattr(program, 'generate', lambda *args: '''def analyze(frames, params, rng):
    data, weights = frames['analysis'], frames['weights']
    w = data['id'].map(weights.set_index('id')['weight'])
    emit('weighted', (data['score']*w).sum()/w.sum(), {'analysis':data.index, 'weights':weights.index}, n=len(data))
''')
    result = pipeline.run(inputs)['analysis']['findings'][0]
    assert result['value'] == pytest.approx(34/6)
    assert result['sample_size']['source_rows'] == [dict(table_id='lookup',count=3),dict(table_id='measure',count=3)]
    assert result['sample_size']['excluded_n'] is None


def test_figure_intent_cannot_silently_drop_an_unknown_output_key():
    inputs, compact = case()
    compact['figure_requests'] = [dict(id='figure', purpose='Show mean', task_ids=['mean'],
                                       output_keys=['score', 'invented'])]
    with pytest.raises(ValueError, match='图表意图引用未声明'):
        compile_plan(compact, inputs)
