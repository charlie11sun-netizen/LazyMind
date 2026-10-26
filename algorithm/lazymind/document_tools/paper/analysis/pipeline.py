"""One plan, reusable statistics and task programs, independently recoverable presentation."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import copy
import json
import os

from . import model
from .plan import validate_plan, uses_program
from . import program
from .plan_repair import PlanRepairError, AnalysisFeasibilityError, UnverifiedMaterialClaim
from .reference import reference, select
from .schema import (
    ContractError, binding, digest, normalize_input, table_map, validate_analysis, validate_charts, VERSION,
)
from .views import materialize
from .methods import executed_methods
from .presentation import finding_label
from ..execution.threads import ThreadPoolExecutor
from . import trace as diagnostics
from .recovery import Store
from .requirements import check_results, check_delivery

_trace_sink = ContextVar('paper_analysis_trace_sink', default=None)


@contextmanager
def capture_execution():
    records = []
    token = _trace_sink.set(records)
    try:
        yield records
    finally:
        _trace_sink.reset(token)


def build_analysis(inputs, plan, checked, programs=None):
    bindings = binding(inputs)
    targets = {t['id']: t for t in plan['targets']}
    findings, diagnostics = [], []
    views = materialize(inputs, plan['joins'])
    builtin_jobs = [job for job in plan['jobs'] if not uses_program(job)]
    for job in plan['jobs']:
        if uses_program(job):
            result = copy.deepcopy((programs or {})[job['id']])
            findings.extend(result['findings'])
            diagnostics.extend(result['diagnostics'])
            continue
        numeric, size = checked[job['id']]
        op = job['operation']
        posterior = op.startswith('beta_')
        correlation = op in ('pearson', 'spearman')
        probability = op == 'beta_probability'
        kind = ('probability' if probability else 'count' if op == 'count' else
                'effect' if op in ('difference', 'paired_difference', 'paired_t', 'welch_t', 'ols_coefficient')
                else 'estimate')
        interval = None
        if numeric['lower'] is not None:
            interval = dict(kind='credible' if posterior else 'confidence', level=job['interval_level'],
                            lower=numeric['lower'], upper=numeric['upper'],
                            method='Beta 后验等尾区间' if posterior else 'Pearson Fisher-z 双侧区间' if correlation
                            else '双侧 Student t 区间（独立组差使用 Welch 自由度）')
        test = None
        if numeric['p_value'] is not None:
            test = dict(name=op, statistic=numeric['statistic'], p_value=numeric['p_value'], adjusted_p_value=None,
                        adjustment=None, alternative=job['alternative'],
                        null_hypothesis=('总体 Pearson 相关系数为 0' if op == 'pearson' else
                                         '总体 Spearman 秩相关系数为 0' if op == 'spearman' else '总体均值差为 0'),
                        family_id=None)
        event = None
        if probability:
            sign = {'gt': '>', 'ge': '>=', 'lt': '<', 'le': '<='}[job['operator']]
            event = dict(kind='posterior', event=f'P(θ {sign} {job["threshold"]} | 当前数据、先验与模型)',
                         operator=job['operator'], threshold=job['threshold'], threshold_unit=None, mcse=None)
        columns = targets[job['target_id']]['columns']
        statement = finding_label({'probability': event}, job) + f'：{numeric["value"]:.8g}'
        calculation = json.dumps(job, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        findings.append(dict(id=job['id'], target_id=job['target_id'], kind=kind,
                             statement=statement,
                             columns=columns, calculation=calculation,
                             value=numeric['value'], unit=None if posterior or op == 'count' else job['unit'],
                             sample_size=size, interval=interval, test=test, probability=event,
                             model_id=job['model_id']))
        diagnostics.append(dict(id='computed_' + job['id'], model_id=job['model_id'],
                                name='固定统计实现与数据作用域检查', outcome='passed',
                                value=numeric['value'], threshold=None,
                                details='由已注册的统计实现从原始观测计算，校验筛选、缺失处理、样本单位和方法前提。'))
    tests = [f for f in findings if f['test']]
    if tests and plan['multiplicity'] == 'holm':
        ordered = sorted(tests, key=lambda f: f['test']['p_value'])
        maximum = 0.0
        for index, finding in enumerate(ordered):
            maximum = max(maximum, min(1.0, (len(ordered) - index) * finding['test']['p_value']))
            finding['test'].update(adjusted_p_value=maximum, adjustment='holm', family_id='planned_primary_tests')
    limitations = list(plan['limitations'])
    if any(job['operation'] in ('pearson', 'spearman') for job in builtin_jobs):
        limitations.append('相关系数描述锁定范围内两个变量的关联，不能证明因果；零相关也不等于独立。'
                           '不同变量对按共同非缺失观测分别计算，样本量可能不同；缺失值不填零。'
                           '按变量对删缺失值的矩阵不保证为半正定矩阵，不能直接当作协方差模型。')
    if any(job['operation'] == 'pearson' and (job['correlation_test'] or job['interval_level']) for job in builtin_jobs):
        limitations.append('Pearson 的零相关检验依赖独立、二元正态的观测假设，Fisher-z 区间为近似点态区间；'
                           '设计依据与单位键检查不能证明这些分布假设成立。')
    if any(job['operation'] == 'spearman' and job['correlation_test'] for job in builtin_jobs):
        limitations.append('Spearman 检验使用大样本渐近 p 值，只接受共同有效样本数大于五百的任务；未执行置换检验。')
    for identifier, count in views[2].items():
        if count:
            limitations.append(f'关联视图 {identifier} 使用 inner 连接，排除了 {count} 条左表未匹配记录；'
                               '筛选后实验单位排除数无法直接归因，sample_size.excluded_n 标为 null。')
    limitations.extend('诊断限制：' + d['name'] + '：' + d['details']
                       for d in diagnostics if d['outcome'] in ('failed', 'not_checked'))
    for m in plan['models']:
        beta_jobs = [j for j in builtin_jobs if j['model_id'] == m['id'] and j['operation'].startswith('beta_')]
        if m['priors'] and beta_jobs:
            diagnostics.append(dict(id='analytic_' + m['id'], model_id=m['id'], name='MCMC 收敛诊断',
                                    outcome='not_applicable', value=None, threshold=None,
                                    details='当前模型采用解析后验，不产生 MCMC 链或 ESS。'))
            limitations.append(f'{m["id"]} 的后验结果以声明的似然和先验为条件，不表示模型为真的概率。')
            # Analytic predictive checks and sensitivity use the declared model and full observations.
            from scipy import stats
            job = beta_jobs[0]
            table = views[0][job['table_id']]
            column = table['columns'].index(job['column'])
            trial_index = table['columns'].index(job['trials_column']) if job['trials_column'] else None
            chosen = [i for i in select(table, job['filters']) if table['rows'][i][column] is not None
                      and (trial_index is None or table['rows'][i][trial_index] is not None)]
            successes = sum(table['rows'][i][column] for i in chosen)
            n = checked[job['id']][1]['n']
            parameters = m['priors'][0]['parameters']
            a, b = parameters['alpha'], parameters['beta']
            for prefix, alpha, beta in [('prior', a, b), ('posterior', a + successes, b + n - successes)]:
                mass = float(stats.betabinom.pmf(successes, n, alpha, beta))
                diagnostics.append(dict(id=prefix + '_predictive_' + m['id'], model_id=m['id'],
                                        name=prefix + ' predictive count check', outcome='passed', value=mass,
                                        threshold=None, details='在相同试验次数下，预测成功总数等于当前观测数的概率。'
                                        '这是解析预测分布检查，不是 p 值，也不证明模型正确。'))
            alternatives = {model['id']: copy.deepcopy(model) for model in plan['models']}
            alternatives[m['id']]['priors'][0]['parameters'] = dict(alpha=2 * a, beta=2 * b)
            sensitivity, _ = reference(job, inputs, alternatives, views)
            diagnostics.append(dict(id='sensitivity_' + m['id'], model_id=m['id'], name='先验集中程度敏感性',
                                    outcome='passed', value=sensitivity['value'], threshold=None,
                                    details=f'保持先验均值，将 alpha/beta 加倍至 {2 * a}/{2 * b}，'
                                    f'同一目标的对照值为 {sensitivity["value"]}；不据此选择更有利先验。'))
    summary = '；'.join(f['statement'] for f in findings)
    return validate_analysis(dict(**bindings, summary=summary, targets=plan['targets'], methods=executed_methods(plan, findings),
                                  findings=findings, models=plan['models'], diagnostics=diagnostics,
                                  limitations=list(dict.fromkeys(limitations))), inputs)


def without_optional(plan, failures):
    """Only tasks declared optional before execution can be omitted, with an explicit record."""
    dropped = {i['job_id'] for i in failures}
    if any(j['required'] for j in plan['jobs'] if j['id'] in dropped):
        raise ValueError('不能省略必需任务')
    result = copy.deepcopy(plan)
    result['jobs'] = [j for j in result['jobs'] if j['id'] not in dropped]
    targets = {j['target_id'] for j in result['jobs']}
    models = {j['model_id'] for j in result['jobs']}
    result['targets'] = [t for t in result['targets'] if t['id'] in targets]
    result['models'] = [{**m, 'target_ids': [t for t in m['target_ids'] if t in targets]}
                        for m in result['models'] if m['id'] in models]
    result['limitations'].extend('可选分析未完成 ' + i['job_id'] + '：' + i['message'] for i in failures)
    return result


def figures_after_results(inputs, analysis, plan, attempts, store, references):
    from ..plotting.analysis import draw
    presentation = copy.deepcopy(plan)
    failures = []
    for request in plan['figure_requests']:
        key = digest(dict(request=request, analysis=analysis))
        figure = store.load('figure_plan', key)
        error = ''
        if figure is None:
            for attempt in range(min(attempts, 2)):
                try:
                    figure = model.plan_figure(inputs, analysis, plan, request, error)
                    store.save('figure_plan', key, figure)
                    break
                except (ValueError, TypeError, KeyError) as exc:
                    error = str(exc)[-3000:]
        if figure is None:
            failures.append(dict(figure_id=request['id'], required=request['required'], error=error))
        else:
            presentation['figures'].append(figure)
    charts, records = draw(copy.deepcopy(analysis), presentation['figures'], inputs=inputs,
        jobs=copy.deepcopy(plan['jobs']), plan=presentation, references=references, recovery=store)
    if any(f['required'] for f in failures):
        raise ValueError('必需图表规划未完成: ' + json.dumps(failures, ensure_ascii=False))
    return charts, records + failures


def finish_verified(analysis, inputs, plan, attempts, store, references):
    """Finish every independent branch and save successes even when another branch fails."""
    from ..plotting.analysis import draw
    identity = digest(dict(analysis=analysis, plan=plan))

    def interpret():
        saved = store.load('interpretation', identity)
        if saved:
            # The snapshot is bound to this exact verified analysis, code and request.
            from .interpretation import validate, render
            try:
                result = validate(saved[1]['response'], analysis, inputs['constraints']['paper_language'])
                return render(result, analysis, plan), {**saved[1], 'reused': True}
            except (ValueError, KeyError, TypeError, IndexError):
                pass
        result = model.interpret_results(inputs, copy.deepcopy(analysis), copy.deepcopy(plan), attempts)
        store.save('interpretation', identity, result)
        return result

    outcomes, errors = {}, []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = dict(interpretation=pool.submit(interpret))
        if plan['figures'] or plan['figure_requests']:
            futures['figures'] = pool.submit(figures_after_results, inputs, copy.deepcopy(analysis),
                                            copy.deepcopy(plan), attempts, store, references)
        for name, future in futures.items():
            try:
                outcomes[name] = future.result()
            except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                errors.append(f'{name}: {str(exc)[-1800:]}')
    if errors:
        raise ContractError('INCOMPLETE_OUTPUT', 'analysis.presentation',
                            '统计计算已完成；结论或图表尚未全部完成。重试会复用已保存且有效的恢复点。'
                            + '；'.join(errors), retryable=True)
    summary, interpretation_trace = outcomes['interpretation']
    charts, figure_trace = outcomes.get('figures', ([], []))
    return summary, interpretation_trace, charts, figure_trace


def plan_statistics(inputs, attempts, store=None):
    """One initial plan plus bounded statistical repair; figures have their own budgets."""
    plan, error, planning, candidate, issues = None, '', [], None, []
    completed = {}
    pending = store.load('statistics_plan', 'pending') if store else None
    if isinstance(pending, dict) and isinstance(pending.get('plan'), dict):
        candidate = pending['plan']
        issues, error = pending.get('issues', []), pending.get('error', '')
    for attempt in range(1, attempts + 2):
        try:
            if issues and candidate is not None:
                with diagnostics.stage('plan_repair', attempt=attempt):
                    candidate = model.repair_plan(inputs, candidate, issues, previous_error=error)
                    diagnostics.save(f'plan-attempt-{attempt}.json', candidate)
            elif candidate is None:
                with diagnostics.stage('plan_generation', attempt=attempt):
                    candidate = model.make_plan(inputs, error)
                    diagnostics.save(f'plan-attempt-{attempt}.json', candidate)
            with diagnostics.stage('plan_validation', attempt=attempt):
                candidate = validate_plan(candidate, inputs, figures=False)
            if store:
                store.save('statistics_plan', 'pending', dict(plan=candidate, issues=[], error=''))
            with diagnostics.stage('statistics_execution', attempt=attempt):
                views = materialize(inputs, candidate['joins'])
                models = {m['id']: m for m in candidate['models']}
                references, failed_jobs = {}, []
                for job in candidate['jobs']:
                    if uses_program(job):
                        continue
                    try:
                        identity = digest(dict(job=job, model=models.get(job['model_id'])))
                        cached = completed.get(identity)
                        if cached is None and store:
                            saved = store.load('statistical_cell', identity)
                            if isinstance(saved, dict) and saved.get('job') == job:
                                value = saved.get('result')
                                if (isinstance(value, list) and len(value) == 2 and
                                        all(isinstance(v, dict) for v in value) and
                                        set(value[0]) == {'value', 'lower', 'upper', 'statistic', 'p_value'} and
                                        set(value[1]) == {'source_rows', 'n', 'unit', 'excluded_n'}):
                                    cached = value
                        if cached is None:
                            cached = reference(job, inputs, models, views)
                            if store:
                                store.save('statistical_cell', identity, dict(job=job, result=cached))
                        completed[identity] = cached
                        references[job['id']] = cached
                    except AnalysisFeasibilityError as exc:
                        failed_jobs.append(dict(job_id=job['id'], field=None, message=str(exc),
                                                reason_code=exc.code, repair_scope='analytical_choice',
                                                diagnostics=exc.diagnostics))
                    except (ValueError, TypeError, KeyError) as exc:
                        failed_jobs.append(dict(job_id=job['id'], field=None, message=str(exc)))
                if failed_jobs:
                    if attempt == attempts + 1:
                        by_id = {j['id']: j for j in candidate['jobs']}
                        optional = [i for i in failed_jobs if not by_id[i['job_id']]['required']]
                        if optional:
                            candidate = without_optional(candidate, optional)
                        failed_jobs = [i for i in failed_jobs if by_id[i['job_id']]['required']]
                    if failed_jobs:
                        raise PlanRepairError(failed_jobs)
            plan = candidate
            diagnostics.save('locked-plan.json', plan)
            planning.append(dict(attempt=attempt, outcome='passed'))
            break
        except PlanRepairError as exc:
            if exc.plan is not None:
                candidate = exc.plan
            issues = exc.issues
            error = str(exc)[-4000:]
            if store and candidate is not None:
                store.save('statistics_plan', 'pending', dict(plan=candidate, issues=issues, error=error))
            planning.append(dict(attempt=attempt, outcome='failed', error=error, repair_fields=issues))
            if attempt == attempts + 1 and candidate is not None:
                indexed = {j['id']: j for j in candidate['jobs']}
                if issues and all(i.get('job_id') in indexed and not indexed[i['job_id']]['required'] for i in issues):
                    reduced = validate_plan(without_optional(candidate, issues), inputs, figures=False)
                    views = materialize(inputs, reduced['joins'])
                    models = {m['id']: m for m in reduced['models']}
                    references = {}
                    for job in reduced['jobs']:
                        if not uses_program(job):
                            identity = digest(dict(job=job, model=models.get(job['model_id'])))
                            references[job['id']] = completed.get(identity)
                            if references[job['id']] is None:
                                references[job['id']] = reference(job, inputs, models, views)
                    plan = reduced
                    break
        except UnverifiedMaterialClaim as exc:
            error = str(exc)[-4000:]
            planning.append(dict(attempt=attempt, outcome='failed', error=error,
                                 unverified_material_claims=exc.claims))
            if not issues:
                candidate = None
        except ContractError as exc:
            if exc.code in ('MISSING_MATERIAL', 'EXECUTION_FAILED'):
                raise
            error = str(exc)[-4000:]
            planning.append(dict(attempt=attempt, outcome='failed', error=error))
            if not issues:
                candidate = None
        except (ValueError, TypeError, KeyError) as exc:
            error = str(exc)[-4000:]
            planning.append(dict(attempt=attempt, outcome='failed', error=error))
            if not issues:
                candidate = None
    if plan is None:
        raise ContractError('INVALID_OUTPUT', 'analysis.plan', '分析计划验证失败: ' + error)
    return plan, references, planning


def run(raw_inputs):
    with diagnostics.stage('input_validation'):
        inputs = normalize_input(raw_inputs)
        diagnostics.save('input-summary.json', {
            **{key: value for key, value in binding(inputs).items() if key.endswith('sha256')},
            'tables': [{'id': key, 'rows': len(table['rows']), 'columns': len(table['columns'])}
                       for key, table in table_map(inputs).items()],
        })
    try:
        attempts = int(os.environ.get('LAZYMIND_PAPER_ANALYSIS_ATTEMPTS', '3'))
        if not 1 <= attempts <= 3:
            raise ValueError
    except ValueError as exc:
        raise ValueError('统计修复尝试次数必须为 1..3') from exc
    # Skill overrides are deployment-owned resources and participate in cache identity.
    from ..plotting.skills import PlotSkills
    skills = PlotSkills()
    from importlib.metadata import version
    versions = {**program.capabilities(), 'matplotlib': version('matplotlib')}
    store = Store(inputs, dict(versions=versions, plot_resources=skills.resource_digest()))
    saved = store.load('statistics', 'planned')
    if saved:
        try:
            plan = validate_plan(saved['plan'], inputs, figures=False)
            checked = saved['checked']
            if set(checked) != {job['id'] for job in plan['jobs'] if not uses_program(job)}:
                raise ValueError('恢复点统计任务不匹配')
            planning = saved['planning']
        except (ValueError, KeyError, TypeError):
            saved = None
    if not saved:
        plan, checked, planning = plan_statistics(inputs, attempts, store)
        store.save('statistics', 'planned', dict(plan=plan, checked=checked, planning=planning))
    else:
        with diagnostics.stage('statistics_recovery'):
            diagnostics.save('recovery.json', dict(statistics_reused=True, request=store.key))
    programs, executions = {}, []
    for decision_round in range(2):
        errors, analytical = [], []
        views = materialize(inputs, plan['joins'])
        jobs = {j['id']: j for j in plan['jobs']}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {job['id']: pool.submit(program.run, job, inputs, plan, views, store, attempts)
                       for job in plan['jobs'] if uses_program(job) and job['id'] not in programs}
            for identifier, future in futures.items():
                try:
                    result, execution = future.result()
                    programs[identifier] = result
                    executions.append(execution)
                except AnalysisFeasibilityError as exc:
                    issue = dict(job_id=identifier, field=None, message=str(exc), reason_code=exc.code,
                                 diagnostics=exc.diagnostics)
                    (analytical if decision_round == 0 else errors).append(issue)
                except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                    errors.append(dict(job_id=identifier, message=str(exc)[-1800:]))
        if analytical:
            try:
                revised = model.repair_plan(inputs, plan, analytical)
                revised = validate_plan(revised, inputs, figures=False)
                old = {j['id']: j for j in plan['jobs']}
                for job in revised['jobs']:
                    if job != old.get(job['id']):
                        programs.pop(job['id'], None)
                        if not uses_program(job):
                            checked[job['id']] = reference(job, inputs, {m['id']: m for m in revised['models']}, views)
                plan = revised
                checked = {k: v for k, v in checked.items() if k in {j['id'] for j in plan['jobs'] if not uses_program(j)}}
                store.save('statistics', 'planned', dict(plan=plan, checked=checked, planning=planning))
            except (ValueError, TypeError, KeyError) as exc:
                errors.extend({**i, 'message': i['message'] + '；分析决策修复失败：' + str(exc)} for i in analytical)
                analytical = []
        optional = [i for i in errors if not jobs[i['job_id']]['required']]
        if optional:
            plan = without_optional(plan, optional)
        errors = [i for i in errors if jobs[i['job_id']]['required']]
        if errors:
            diagnostics.save('incomplete-tasks.json', errors)
            raise ContractError('INCOMPLETE_STATISTICS', 'analysis.tasks',
                '部分必需分析未完成；已保存成功任务。' + '；'.join(i['message'] for i in errors), retryable=True)
        if not analytical:
            break
    analysis = build_analysis(inputs, plan, checked, programs)
    check_results(plan, analysis)
    locked = digest(plan)
    diagnostics.save('locked-plan.json', plan)
    diagnostics.save('verified-results.json', analysis)
    presentation_plan = copy.deepcopy(plan)
    for figure in presentation_plan['figures']:
        if isinstance(figure, dict) and isinstance(figure.get('finding_ids'), list):
            figure['finding_ids'] = [result_id for identifier in figure['finding_ids']
                for result_id in ([f['id'] for f in programs[identifier]['findings']]
                                  if identifier in programs else [identifier])]
    summary, interpretation_trace, charts, figure_trace = finish_verified(
        analysis, inputs, presentation_plan, attempts, store, checked)
    if digest(plan) != locked:
        raise ContractError('INVALID_OUTPUT', 'analysis.plan', '执行中统计计划发生变化')
    analysis['summary'] = summary
    analysis['limitations'].extend('可选图表未完成 ' + f['figure_id'] + '：' + f['error']
                                  for f in figure_trace if f.get('error') and not f.get('required', True))
    ledger = check_delivery(plan, analysis, charts)
    diagnostics.save('requirement-delivery.json', ledger)
    with diagnostics.stage('output_validation'):
        analysis = validate_analysis(analysis, inputs)
        validate_charts(charts, analysis)
        diagnostics.save('analysis.json', analysis)
        diagnostics.save('figures.json', [dict(id=chart['id'], title=chart['title'],
                                              svg_bytes=len(chart['svg'].encode())) for chart in charts])
    trace = dict(contract=VERSION, input_sha256=analysis['input_sha256'], plan=plan, plan_sha256=locked,
                 executor='hybrid', executions=executions, statistics_reused=bool(saved), versions=versions,
                 planning=planning, requirements=ledger, interpretation=interpretation_trace, figures=figure_trace)
    sink = _trace_sink.get()
    if sink is not None:
        sink.append(trace)
    return dict(is_demo=False, analysis=analysis, charts=charts)
