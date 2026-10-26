"""Open-ended statistical tasks with fixed execution, provenance and output handling."""
import ast
import copy
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path

from .schema import Finding, Diagnostic, parse, digest
from .reference import select
from ..execution import container
# Analysis policy is independent of plot rendering. Isolation remains the security boundary.
FORBIDDEN = {'open', 'exec', 'eval', 'compile', '__import__', 'setattr', 'delattr',
             'globals', 'locals', 'input', 'breakpoint', 'exit', 'quit'}
FORBIDDEN_ATTRIBUTES = {'save', 'savez', 'load', 'read_csv', 'read_json', 'read_pickle', 'read_excel',
    'read_table', 'to_csv', 'to_json', 'to_pickle', 'to_excel', 'fromfile', 'tofile', 'memmap', 'system', 'popen'}
from ..plotting.code_response import code_response, CODE_INSTRUCTION, REPAIR_INSTRUCTION
from ..plotting.model import call_code_model, PlotModelError
from . import trace
from .program_worker import INTERFACE
from .plan_repair import AnalysisFeasibilityError
LIBRARIES = {'numpy': 'numpy', 'scipy': 'scipy', 'pandas': 'pandas',
             'statsmodels': 'statsmodels', 'sklearn': 'scikit-learn'}
STANDARD = {'math', 'statistics', 'collections', 'itertools', 'functools', 'copy'}


def capabilities():
    available = {}
    for module, distribution in LIBRARIES.items():
        if importlib.util.find_spec(module) is not None:
            available[module] = importlib.metadata.version(distribution)
    return available


def validate_code(code, libraries):
    if not isinstance(code, str) or not code.strip() or len(code.encode()) > 64 * 1024:
        raise ValueError('分析代码须为不超过 64 KiB 的非空文本')
    tree = ast.parse(code)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == INTERFACE['function']]
    if len(functions) != 1 or [a.arg for a in functions[0].args.args] != INTERFACE['arguments']:
        raise ValueError('定义 analyze(frames, params, rng)；使用 frames["analysis"] 和 params["column"]')
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue
        if isinstance(node, ast.Assign):
            ast.literal_eval(node.value)
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        raise ValueError('顶层只允许导入、函数和常量；调用由宿主完成')
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in {'getattr', 'vars', 'hasattr'} and node.args
                and isinstance(node.args[0], ast.Name) and node.args[0].id in {'params', 'frames'}):
            raise ValueError(f'第 {node.lineno} 行使用直接键访问；无需类型探测或反射')
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ('params', 'frames'):
            if node.attr not in {'get', 'keys', 'values', 'items', 'copy'}:
                raise ValueError(f'第 {node.lineno} 行 {node.value.id} 是字典；使用 {node.value.id}[{node.attr!r}]，无需 getattr/vars')
        roots = ([a.name.split('.')[0] for a in node.names] if isinstance(node, ast.Import) else
                 [(node.module or '').split('.')[0]] if isinstance(node, ast.ImportFrom) else [])
        if set(roots) - (set(libraries) | STANDARD) or isinstance(node, ast.ImportFrom) and node.level:
            raise ValueError('导入不在当前可用库中；允许：' + ', '.join(sorted(set(libraries) | STANDARD)))
        if isinstance(node, ast.Name) and (node.id in FORBIDDEN or node.id.startswith('__')):
            raise ValueError(f'第 {node.lineno} 行不允许 {node.id}；数据使用 frames["analysis"]，参数使用 params["column"]，无需类型探测或反射')
        if isinstance(node, ast.Attribute) and (node.attr.startswith('_') or
                node.attr in FORBIDDEN_ATTRIBUTES - {'random', 'default_rng'}):
            raise ValueError('禁止访问属性: ' + node.attr)
        if isinstance(node, ast.FunctionDef) and node.decorator_list:
            raise ValueError('分析函数不使用装饰器')


def prepare(job, inputs, views):
    table = views[0][job['table_id']]
    selected = select(table, job['filters'])
    baseline = select(table, job['baseline_filters']) if job['baseline_filters'] else []
    all_rows = sorted(set(selected) | set(baseline))
    required = list(dict.fromkeys([job['column'], *job['columns'], *job['predictors'], *job['pair_by'],
                                  *([job['other_column']] if job['other_column'] else []),
                                  *([job['trials_column']] if job['trials_column'] else [])]))
    indices = [table['columns'].index(c) for c in required]
    complete = [i for i in all_rows if all(table['rows'][i][c] is not None for c in indices)]
    if len(complete) != len(all_rows) and job['missing_policy'] == 'error':
        raise ValueError('代码任务输入包含缺失值；需在计划声明完整观测筛选或自定义处理')
    used = complete if job['missing_policy'] == 'complete_case' else all_rows
    if not used:
        raise ValueError('代码任务筛选后没有观测')
    task = copy.deepcopy(job)
    used_set = set(used)
    task['selected_rows'] = [i for i in selected if i in used_set]
    task['baseline_rows'] = [i for i in baseline if i in used_set]
    additional = {}
    for name, binding in job['inputs'].items():
        source = views[0][binding['table_id']]
        chosen = select(source, binding['filters'])
        positions = [source['columns'].index(c) for c in binding['columns']]
        complete_rows = [i for i in chosen if all(source['rows'][i][j] is not None for j in positions)]
        if len(chosen) != len(complete_rows) and binding['missing_policy'] == 'error':
            raise ValueError('附加输入 ' + name + ' 含缺失值，需声明处理策略')
        selected_rows = complete_rows if binding['missing_policy'] == 'complete_case' else chosen
        if not selected_rows:
            raise ValueError('附加输入 ' + name + ' 无观测')
        additional[name] = dict(table_id=binding['table_id'], row_ids=selected_rows,
            columns=binding['columns'], rows=[[source['rows'][i][j] for j in positions] for i in selected_rows])
    return dict(task=task, table={**table, 'rows': [table['rows'][i] for i in used]},
                row_ids=used, selected_ids=all_rows, additional_frames=additional)


def generate(payload, inputs, libraries, previous='', error=''):
    prompt = CODE_INSTRUCTION + '''Implement only the function shown in interface and skeleton. The host loads ALL selected
observations into named pandas DataFrames with original integer row indices, executes in a sandbox, and saves outputs.
Do not read/write files, install packages, access network/environment, or redefine emit/diagnose.
Use the available scientific libraries and actual versions below. Prefer library estimators to handwritten formulas.
Implement params['procedure'] and every declared output; never replace a requested method by a simpler one to pass.
Data previews are not the dataset. Calculate from data, never copy preview numbers or invent observations.
Filters and declared complete_case handling have already been applied. frames['analysis'] is the selected scope.
For comparisons frames['baseline'] is a SEPARATE baseline and frames['combined'] is their union. Never pool these groups.
frames is a dictionary; params is a read-only mapping, not an attribute object. Use direct key access, not hasattr/getattr/vars or compatibility readers.
Custom transformations and missing handling
must follow the task; preserve original row indices for provenance. Do not change task, scope, comparisons or pairing.
Use supplied rng for resampling. No top-level calls. Plain Python source only.

Host helper emit(key, value, rows, *, unit=None, kind='estimate', interval=None, test=None,
probability=None, n=None, sample_unit='observation') records ONE output from params['outputs'].
Call exactly once per declared key. Pass the ORIGINAL row indices actually used, e.g. subset.index.
For multiple input frames pass rows={'analysis': analysis_indices, 'declared_name': other_indices}; name only frames
actually used, preserving their original indices separately. Supply the effective sample size n and unit explicitly
for combined sources. New DataFrame indices from merge/melt are not source indices; retain originals before transforming.
The host derives source-row counts; n defaults to len(rows). For pairs/clusters/trials explicitly supply the effective
n and sample_unit. Neither record counts nor a seed name prove independence. Never silently remove requested groups.
interval is None or {kind:'confidence'|'credible'|'prediction',level:float,lower:float,upper:float,method:str}.
test is None or {name:str,statistic:float|None,p_value:float,adjusted_p_value:None,adjustment:None,
 alternative:'two_sided'|'greater'|'less',null_hypothesis:str,family_id:None}.
Host applies the plan's multiplicity correction; do not adjust twice. Preserve actual interval semantics.
kind is estimate/effect/count/probability/observation; pure test outputs may use observation and value=None.
Posterior probability also requires a model and a probability object matching the supplied Finding schema.
Use diagnose(name, outcome, details, value=None, blocking=False) for convergence, assumptions or numerical checks you actually ran;
outcome is passed/failed/not_applicable/not_checked. Call every params["required_diagnostics"] name;
those must pass. Set blocking=True only for indispensable numerical validity. Other failed checks are limitations. Do not report assumptions as verified just because fitting succeeds.
If the requested calculation cannot be performed, raise ValueError with the concrete cause. Never fabricate success.

Examples (adapt column names and keys to the task, do not copy measurements):
  emit('median', data['score'].median(), data.index, unit='point')
  fit = sm.OLS(data['score'], sm.add_constant(data[['x']])).fit()
  emit('slope', fit.params['x'], data.index, unit='point/unit')
  result = stats.chi2_contingency(pd.crosstab(data['arm'], data['outcome']))
  emit('association', None, data.index, kind='observation', test=dict(name='Pearson chi-square',
       statistic=float(result.statistic), p_value=float(result.pvalue), adjusted_p_value=None,
       adjustment=None, alternative='two_sided', null_hypothesis='Variables are independent', family_id=None))
'''
    if previous:
        prompt += REPAIR_INSTRUCTION
    table = payload['table']
    task_view = {key: value for key, value in payload['task'].items() if key not in ('selected_rows', 'baseline_rows')}
    interface = copy.deepcopy(INTERFACE)
    if not task_view['baseline_filters']:
        interface['frames'] = {'analysis': interface['frames']['analysis']}
    interface['frames'].update({name: 'Additional DataFrame, original row indices; columns: ' + ', '.join(frame['columns'])
                                for name, frame in payload['additional_frames'].items()})
    context = dict(params=task_view, libraries=libraries, interface=interface,
        skeleton='def ' + INTERFACE['function'] + '(' + ', '.join(INTERFACE['arguments']) + '):\n    data = frames["analysis"]\n',
        code_policy=dict(forbidden_names=sorted(FORBIDDEN),
                         forbidden_attributes=sorted(FORBIDDEN_ATTRIBUTES - {'random', 'default_rng'}),
                         private_attributes=False, allowed_imports=sorted(set(libraries) | STANDARD)),
        scope_counts={key: len(payload['task'][key]) for key in ('selected_rows', 'baseline_rows')},
        data=dict(columns=table['columns'], column_types=table['column_types'], units=table['units'],
                  row_count=len(table['rows']), preview_rows=table['rows'][:3]),
        request=inputs['request'], study_design=inputs.get('study_design'), models=payload.get('models', []),
        finding_schema=Finding.model_json_schema(), previous_code=previous, previous_error=error)
    answer = call_code_model(prompt + '\nINPUT_JSON:\n' + json.dumps(context, ensure_ascii=False, allow_nan=False))
    return code_response(answer, previous)


def validate_results(raw, payload, inputs, plan, views):
    if not isinstance(raw, dict) or set(raw) != {'findings', 'diagnostics'} or not isinstance(raw['findings'], list):
        raise ValueError('分析任务输出须包含 findings/diagnostics')
    job = payload['task']
    expected = {item['id']: item['label'] for item in job['outputs']}
    keys = [item.get('key') for item in raw['findings'] if isinstance(item, dict)]
    if len(keys) != len(raw['findings']) or len(set(keys)) != len(keys) or set(keys) != set(expected):
        raise ValueError('每个声明的输出必须 emit 一次；不能遗漏、新增或重复结果')
    allowed = set(payload['row_ids'])
    target = next(t for t in plan['targets'] if t['id'] == job['target_id'])
    findings = []
    for item in raw['findings']:
        raw_rows = item['rows']
        row_sets = raw_rows if isinstance(raw_rows, dict) else {'analysis': raw_rows}
        scopes = {'analysis': (job['table_id'], allowed), 'baseline': (job['table_id'], allowed),
                  'combined': (job['table_id'], allowed),
                  **{name: (frame['table_id'], set(frame['row_ids']))
                     for name, frame in payload['additional_frames'].items()}}
        if not row_sets or not set(row_sets) <= scopes.keys():
            raise ValueError('结果来源须引用实际输入 DataFrame')
        provenance = {}
        rows = []
        for name, indices in row_sets.items():
            table_id, permitted = scopes[name]
            if (not isinstance(indices, list) or not indices or any(type(i) is not int for i in indices)
                    or len(set(indices)) != len(indices) or not set(indices) <= permitted):
                raise ValueError('结果来源行必须唯一且属于锁定输入范围；请保留原始 DataFrame 索引')
            rows.extend(indices)
            for row in indices:
                for table, index in views[1][table_id][row].items():
                    provenance.setdefault(table, set()).add(index)
        inferential = item.get('interval') or item.get('test') or item.get('probability')
        if inferential and not job['design_evidence_refs']:
            raise ValueError('推断结果缺少计划中的设计依据；不能以代码运行成功代替研究设计事实')
        n = item['n'] if item['n'] is not None else len(rows)
        if item['sample_unit'] != 'trial' and (type(n) is not int or not 0 < n <= len(rows)):
            raise ValueError('有效样本量必须符合实际来源行数和样本单位')
        value = dict(id=job['id'] + ':' + item['key'], target_id=job['target_id'],
            statement=expected[item['key']], kind=item['kind'], value=item['value'], unit=item['unit'],
            columns=target['columns'], calculation=json.dumps(dict(task=job['id'], method=job['operation'],
                procedure=job['procedure'], output=item['key']), ensure_ascii=False),
            sample_size=dict(source_rows=[dict(table_id=t, count=len(ids)) for t, ids in sorted(provenance.items())],
                n=n, unit=item['sample_unit'], excluded_n=(len(set(payload['selected_ids']) - set(rows))
                    if not isinstance(raw_rows, dict) and n == len(rows) and item['sample_unit'] in ('observation', 'row') else None)),
            interval=item['interval'], test=item['test'], probability=item['probability'], model_id=job['model_id'])
        findings.append(parse(Finding, value, 'output.analysis.findings'))
    diagnostics = []
    required = set(job['required_diagnostics'])
    for i, raw_note in enumerate(raw['diagnostics']):
        note = dict(raw_note)
        blocking = note.pop('blocking', False)
        if blocking:
            required.add(note['name'])
        diagnostics.append(parse(Diagnostic, dict(id=job['id'] + '_diagnostic_' + str(i),
            model_id=job['model_id'], threshold=None, **note), 'output.analysis.diagnostics'))
    passed = {d['name'] for d in diagnostics if d['outcome'] == 'passed'}
    if required - passed:
        raise AnalysisFeasibilityError('REQUIRED_DIAGNOSTIC_FAILED',
            '必要诊断未通过: ' + ', '.join(sorted(required - passed)), diagnostics)
    diagnostics.append(dict(id=job['id'] + '_execution', model_id=job['model_id'],
        name='代码执行与来源检查', outcome='passed', value=None, threshold=None,
        details='代码已在隔离环境执行，输出字段、来源范围与数值约束通过检查；未进行另一实现的独立数值复核。'))
    return dict(findings=findings, diagnostics=diagnostics)


def run(job, inputs, plan, views, store, attempts):
    payload = prepare(job, inputs, views)
    payload['models'] = [m for m in plan['models'] if m['id'] == job['model_id']]
    identity = digest(dict(task=job, models=payload['models']))
    saved = store.load('analysis_task', identity)
    if saved and saved.get('executor') == 'equivalent_component':
        result, execution = run_component(job, inputs, plan, views, saved['history'])
        return result, {**execution, 'reused': True}
    if saved:
        try:
            with trace.stage('analysis_recovery', task=job['id']):
                result = validate_results(saved['raw'], payload, inputs, plan, views)
                return result, {**saved['trace'], 'reused': True}
        except (ValueError, KeyError, TypeError):
            pass
    container.validate_environment()
    libraries = capabilities()
    timeout = int(os.environ.get('LAZYMIND_PAPER_ANALYSIS_TIMEOUT', '180'))
    if not 1 <= timeout <= 3600:
        raise ValueError('分析执行超时须为 1..3600 秒')
    pending = store.load('analysis_task_pending', identity) or {}
    code, error, history = pending.get('code', ''), pending.get('error', ''), []
    for attempt in range(1, min(attempts, 2) + 1):
        try:
            with trace.stage('analysis_code_generation', task=job['id'], attempt=attempt):
                code = generate(payload, inputs, libraries, code, error)
                validate_code(code, libraries)
                trace.save(f'analysis-{trace.figure_name(job["id"])}-{attempt}.py', code)
            with trace.stage('analysis_execution', task=job['id'], attempt=attempt):
                raw = container.execute({**payload, 'code': code}, Path(__file__).with_name('program_worker.py'),
                                        timeout, label='分析任务 ' + job['id'])
                result = validate_results(raw, payload, inputs, plan, views)
            history.append(dict(attempt=attempt, outcome='passed'))
            execution = dict(task_id=job['id'], code=code, code_sha256=digest(code), attempts=history, libraries=libraries)
            store.save('analysis_task', identity, dict(raw=raw, trace=execution))
            return result, execution
        except AnalysisFeasibilityError:
            # Change analytical choice upstream, not diagnostics or numerical thresholds in code.
            raise
        except (PlotModelError, container.ContainerEnvironmentError):
            raise
        except (ValueError, TypeError, KeyError, SyntaxError, RuntimeError) as exc:
            next_error = str(exc)[-5000:]
            history.append(dict(attempt=attempt, outcome='failed', error=next_error))
            error = next_error
            store.save('analysis_task_pending', identity, dict(code=code, error=error))
    if job['equivalent_component']:
        result = run_component(job, inputs, plan, views, history)
        store.save('analysis_task', identity, dict(executor='equivalent_component', history=history))
        return result
    trace.save('analysis-' + trace.figure_name(job['id']) + '-failures.json', history)
    raise ValueError('分析任务 ' + job['id'] + ' 未完成：' + error)


def run_component(job, inputs, plan, views, history):
    from .plan import component_job, validate_plan
    from .reference import reference
    from .pipeline import build_analysis
    candidate = component_job(job)
    small = {**plan, 'jobs': [candidate], 'figures': [],
             'targets': [t for t in plan['targets'] if t['id'] == job['target_id']],
             'models': [m for m in plan['models'] if m['id'] == job['model_id']], 'multiplicity': 'none'}
    small = validate_plan(small, inputs, figures=False)
    numeric = reference(small['jobs'][0], inputs, {m['id']: m for m in small['models']}, views)
    result = build_analysis(inputs, small, {job['id']: numeric})
    finding = result['findings'][0]
    finding.update(id=job['id'] + ':' + job['outputs'][0]['id'], statement=job['outputs'][0]['label'])
    return dict(findings=[finding], diagnostics=result['diagnostics']), dict(task_id=job['id'],
        executor='equivalent_component', attempts=history, component=job['equivalent_component'])
