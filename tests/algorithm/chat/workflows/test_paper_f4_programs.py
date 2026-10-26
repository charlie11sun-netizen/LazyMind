"""Exercise open-ended F4 programs in the real sandbox, with offline model responses."""
import copy
import math
from unittest.mock import Mock

import pytest

from lazymind.document_tools.paper.analysis import model, pipeline, program, recovery
from lazymind.document_tools.paper.analysis.interpretation import interpret
from lazymind.document_tools.paper.analysis.plan_bindings import evidence_catalog
from lazymind.document_tools.paper.analysis.schema import ContractError, normalize_input
from lazymind.document_tools.paper.plotting import analysis as plots

DISTRIBUTION = '''def analyze(frames, params, rng):
    data = frames["analysis"]
    emit('median', data['score'].median(), data.index, unit='point')
    emit('q75', data['score'].quantile(.75), data.index, unit='point')
'''


@pytest.fixture
def generic(monkeypatch):
    raw = dict(topic='分布分析', request='计算中位数和上四分位数。',
               data=dict(columns=['group', 'score'], column_types=['string', 'number'], units=[None, 'point'],
                         rows=[['a', 1.], ['a', 2.], ['b', 4.], ['b', 8.]]))
    columns = [dict(table_id='data', column='score')]
    plan = dict(targets=[dict(id='t', question='分布如何？', estimand='分布位置', scope='全部观测', columns=columns)],
                models=[], methods=['分位数'], limitations=['仅描述观测。'], figures=[],
                jobs=[dict(id='distribution', target_id='t', operation='quantiles', execution='code', column='score', table_id='data',
                           procedure='计算中位数与线性插值的上四分位数，保留所有观测。',
                           outputs=[dict(id='median', label='中位数'), dict(id='q75', label='上四分位数')])])
    make_plan = Mock(side_effect=lambda *a: copy.deepcopy(plan))
    monkeypatch.setattr(model, 'make_plan', make_plan)
    def explain(inputs, measured, locked, attempts):
        ids = [f['id'] for f in measured['findings']]
        answer = dict(summary='已完成声明的分析。', answers=[dict(target_id='t', conclusion='结果描述当前观测。',
                      finding_ids=ids, key_finding_ids=ids)], mechanism_explanation='不能据此推断机制。',
                      limitations=['不能自动外推。'])
        return interpret(inputs, measured, locked, attempts, Mock(return_value=answer))
    monkeypatch.setattr(model, 'interpret_results', explain)
    monkeypatch.setattr(program, 'call_code_model', Mock(return_value={'code': DISTRIBUTION}))
    monkeypatch.delenv('LAZYMIND_PAPER_PLOT_SKILLS', raising=False)
    monkeypatch.setenv('LAZYMIND_PAPER_ANALYSIS_ATTEMPTS', '2')
    monkeypatch.setenv('LAZYMIND_PAPER_PLOT_ATTEMPTS', '1')
    return raw, plan, make_plan


def design(raw, plan):
    raw['study_design'] = dict(description='每行是独立抽样的一个观测单位，没有重复测量。')
    identifier = evidence_catalog(normalize_input(raw))[0]['id']
    for job in plan['jobs']:
        job.update(inference=True, design_evidence_refs=[identifier])


@pytest.mark.parametrize('scores, expected', [([1., 2., 4., 8.], [3., 5.]),
                                             ([10., 13., 17., 24., 31.], [17., 24.])])
def test_new_method_multiple_results_uses_actual_input(generic, scores, expected):
    raw, plan, _ = generic
    raw['data']['rows'] = [['a', score] for score in scores]
    result = pipeline.run(raw)['analysis']
    assert [f['value'] for f in result['findings']] == pytest.approx(expected)
    assert [f['id'] for f in result['findings']] == ['distribution:median', 'distribution:q75']
    assert all(f['sample_size']['n'] == len(scores) for f in result['findings'])
    assert '中位数' in result['summary'] and any('quantiles' in m for m in result['methods'])
    prompt = program.call_code_model.call_args.args[0]
    assert 'previous_error' in prompt and 'statsmodels' in prompt


def test_custom_comparison_keeps_both_scopes_and_missing_policy(generic, monkeypatch):
    raw, plan, _ = generic
    raw['data']['rows'].append(['a', None])
    job = plan['jobs'][0]
    job.update(operation='median_difference', procedure='两个组分别取中位数，返回 a 减 b。',
               filters=[dict(column='group', operator='eq', value='a')],
               baseline_filters=[dict(column='group', operator='eq', value='b')], missing_policy='complete_case',
               outputs=[dict(id='delta', label='组间中位数差')])
    monkeypatch.setattr(program, 'generate', lambda *a: '''def analyze(frames, params, rng):
    data = frames["analysis"]
    a = frames['analysis']['score']
    b = frames['baseline']['score']
    emit('delta', a.median()-b.median(), frames['combined'].index, unit='point', kind='effect')
''')
    finding = pipeline.run(raw)['analysis']['findings'][0]
    assert finding['value'] == -4.5
    assert finding['sample_size']['n'] == 4 and finding['sample_size']['excluded_n'] == 1


def test_statsmodels_logistic_model_is_available_without_a_new_builtin(generic, monkeypatch):
    raw, plan, _ = generic
    raw['data'] = dict(columns=['x', 'y'], column_types=['number', 'integer'], units=[None, None],
                       rows=[[-1., y] for y in [0, 0, 0, 1]] + [[1., y] for y in [0, 1, 1, 1]])
    plan['targets'][0]['columns'] = [dict(table_id='data', column=c) for c in ['x', 'y']]
    plan['models'] = [dict(id='logit', target_ids=['t'], method='Logistic regression',
                          formula='logit(P(y=1)) = intercept + slope*x', likelihood='Bernoulli', priors=[])]
    plan['jobs'][0].update(operation='logistic_regression', column='y', columns=['x'], model_id='logit',
        procedure='拟合带截距的二项 logistic 回归，输出截距和 x 的 log-odds 系数，检查收敛。',
        outputs=[dict(id='intercept', label='截距'), dict(id='slope', label='Log-odds 系数')])
    design(raw, plan)
    monkeypatch.setattr(program, 'generate', lambda *a: '''import statsmodels.api as sm

def analyze(frames, params, rng):
    data = frames["analysis"]
    fit = sm.Logit(data['y'], sm.add_constant(data[['x']])).fit(disp=False)
    emit('intercept', fit.params['const'], data.index)
    emit('slope', fit.params['x'], data.index)
    diagnose('Optimizer', 'passed' if fit.mle_retvals['converged'] else 'failed', 'Checked optimizer convergence.')
''')
    result = pipeline.run(raw)['analysis']
    assert [f['value'] for f in result['findings']] == pytest.approx([0., math.log(3)])
    assert all(f['model_id'] == 'logit' for f in result['findings'])


def test_scipy_pure_test_and_bootstrap_interval_use_existing_delivery(generic, monkeypatch):
    raw, plan, _ = generic
    plan['jobs'][0].update(operation='distribution_inference', columns=['group'],
        procedure='比较两组分布并对总体均值做 percentile bootstrap，输出 Mann Whitney 检验及均值区间。',
        outputs=[dict(id='test', label='Mann Whitney 检验'), dict(id='bootstrap', label='均值 Bootstrap 区间')])
    plan['targets'][0]['columns'].append(dict(table_id='data', column='group'))
    plan['multiplicity'] = 'holm'
    design(raw, plan)
    monkeypatch.setattr(program, 'generate', lambda *a: '''from scipy import stats
import numpy as np

def analyze(frames, params, rng):
    data = frames["analysis"]
    result = stats.mannwhitneyu(data.loc[data['group']=='a','score'], data.loc[data['group']=='b','score'])
    emit('test', None, data.index, kind='observation', test=dict(name='Mann Whitney U',
        statistic=float(result.statistic), p_value=float(result.pvalue), alternative='two_sided',
        null_hypothesis='Same distributions', adjustment=None, adjusted_p_value=None, family_id=None))
    boot = stats.bootstrap((data['score'].to_numpy(),), np.mean, n_resamples=999,
        method='percentile', random_state=rng)
    emit('bootstrap', data['score'].mean(), data.index, unit='point', interval=dict(kind='confidence',
        level=.95, lower=float(boot.confidence_interval.low), upper=float(boot.confidence_interval.high),
        method='percentile bootstrap'))
''')
    result = pipeline.run(raw)['analysis']
    test, boot = result['findings']
    assert test['value'] is None and test['test']['p_value'] == pytest.approx(1/3)
    assert test['test']['adjustment'] == 'holm' and 'Mann Whitney' in result['summary']
    assert boot['interval']['lower'] <= boot['value'] <= boot['interval']['upper']
    assert not any('未执行显著性检验' in m for m in result['methods'])


def test_failed_program_repairs_locally_and_reuses_successful_tasks(generic, monkeypatch, tmp_path):
    raw, plan, make_plan = generic
    plan['jobs'].append({**copy.deepcopy(plan['jobs'][0]), 'id': 'second'})
    calls = []
    fail = True
    def generate(payload, inputs, libraries, previous='', error=''):
        calls.append((payload['task']['id'], previous, error))
        if payload['task']['id'] == 'second' and fail:
            return "def analyze(frames, params, rng):\n    data = frames['analysis']\n    raise ValueError('retry this task')\n"
        return DISTRIBUTION
    monkeypatch.setattr(program, 'generate', generate)
    with recovery.workspace(tmp_path):
        with pytest.raises(ContractError, match='部分必需分析未完成'):
            pipeline.run(raw)
        fail = False
        with pipeline.capture_execution() as traces:
            output = pipeline.run(raw)
    assert len(output['analysis']['findings']) == 4 and make_plan.call_count == 1
    assert len([c for c in calls if c[0]=='distribution']) == 1
    failed_retry = [c for c in calls if c[0]=='second'][1]
    assert 'raise ValueError' in failed_retry[1] and 'retry this task' in failed_retry[2]
    assert traces[0]['executions'][0]['reused']


@pytest.mark.parametrize('fault', ['missing_output', 'foreign_rows', 'nonfinite', 'changed_task'])
def test_invalid_program_results_are_not_accepted(generic, monkeypatch, fault):
    raw, _, _ = generic
    code = {
        'missing_output': "def analyze(frames, params, rng):\n    data = frames['analysis']\n    emit('median', 1., data.index)\n",
        'foreign_rows': DISTRIBUTION.replace('data.index', '[999]'),
        'nonfinite': DISTRIBUTION.replace("data['score'].median()", "float('nan')"),
        'changed_task': DISTRIBUTION + "    params['operation'] = 'changed'\n",
    }[fault]
    monkeypatch.setattr(program, 'generate', lambda *a: code)
    with pytest.raises(ContractError, match='部分必需分析未完成'):
        pipeline.run(raw)


def test_generic_findings_reach_custom_figure_without_losing_outputs(generic, monkeypatch):
    raw, plan, _ = generic
    plan['figures'] = [dict(id='distribution', title='Distribution', caption='Observed quantiles.', kind='point',
        style='custom', base_style='point_interval', finding_ids=['distribution'],
        columns=plan['targets'][0]['columns'], instructions='Draw both quantiles without invented intervals.')]
    monkeypatch.setattr(plots, 'call_code_model', Mock(return_value={'code': '''import matplotlib.pyplot as plt

def draw(data, statistics, spec):
    layout = spec['style_contract']['layout']
    fig, ax = plt.subplots(figsize=tuple(layout['single_inches']))
    fig.subplots_adjust(**layout['single_panel'])
    values = [finding['value'] for finding in statistics['findings']]
    ax.scatter(values, [0, 1], color=list(spec['style_contract']['series_colors'].values())[0])
    ax.set_yticks([0, 1], labels=['Median', 'Upper quartile'])
    ax.set_xlabel('Score (point)')
    return fig
'''}))
    output = pipeline.run(raw)
    assert output['charts'][0]['finding_ids'] == ['distribution:median', 'distribution:q75']
    assert '<svg' in output['charts'][0]['svg']
