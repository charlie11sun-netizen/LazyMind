"""Internal analysis plans: generated after intake, never an upstream configuration."""
from __future__ import annotations

import json
import copy
from typing import Literal

from pydantic import Field, model_validator

from .schema import (
    Object, Text, Number, Level, AnalysisTarget, ModelSummary,
    ColumnRef, ContractError, parse, refs_exist, table_map, unique,
)
from .views import materialize
from .plan_repair import PlanRepairError, material_claim
from .plan_bindings import evidence_catalog, bind_job_columns, bind_job_fields, bind_figure_mapping
from .requirements import Requirement, FigureRequest


class Filter(Object):
    column: Text
    operator: Literal['eq', 'ne', 'gt', 'ge', 'lt', 'le', 'in', 'not_null']
    value: str | int | Number | bool | list[str | int | Number | bool] | None = None


BUILTIN_OPERATIONS = {'mean', 'difference', 'paired_difference', 'welch_t', 'paired_t',
                      'ols_coefficient', 'beta_mean', 'beta_probability', 'count', 'pearson', 'spearman'}


def uses_program(job):
    return job['execution'] == 'code'


class ResultSpec(Object):
    id: Text
    label: Text


class InputBinding(Object):
    table_id: Text
    columns: list[Text] = Field(min_length=1)
    filters: list[Filter] = Field(default_factory=list)
    missing_policy: Literal['error', 'complete_case', 'custom'] = 'error'


class ComponentSpec(Object):
    operation: Literal['mean', 'count', 'difference', 'paired_difference', 'welch_t', 'paired_t',
                       'pearson', 'spearman', 'ols_coefficient', 'beta_mean', 'beta_probability']
    other_column: Text | None = None
    correlation_test: bool = False
    predictors: list[Text] = Field(default_factory=list)
    coefficient: Text | None = None
    trials_column: Text | None = None
    interval_level: Level | None = None
    threshold: Number | None = None
    operator: Literal['gt', 'ge', 'lt', 'le'] = 'gt'
    alternative: Literal['two_sided', 'greater', 'less'] = 'two_sided'


def component_job(job):
    spec = job.get('equivalent_component')
    if not spec or spec['operation'] != job['operation'] or len(job['outputs']) != 1:
        raise ValueError('等价组件须明确声明同一标准方法及完整参数，且对应唯一结果')
    if job['missing_policy'] == 'custom' or job['required_diagnostics'] or job['inputs']:
        raise ValueError('自定义转换或必要代码诊断不能用常规组件代替')
    return {**job, **spec, 'execution': 'component', 'procedure': '', 'outputs': [],
            'equivalent_component': None, 'columns': []}


class Job(Object):
    id: Text
    target_id: Text
    operation: Text
    execution: Literal['component', 'code'] = 'component'
    inputs: dict[Text, InputBinding] = Field(default_factory=dict)
    procedure: str = ''
    columns: list[Text] = Field(default_factory=list)
    outputs: list[ResultSpec] = Field(default_factory=list, max_length=200)
    inference: bool = False
    table_id: Text
    column: Text
    other_column: Text | None = None
    correlation_test: bool = False
    filters: list[Filter] = Field(default_factory=list)
    baseline_filters: list[Filter] = Field(default_factory=list)
    pair_by: list[Text] = Field(default_factory=list)
    predictors: list[Text] = Field(default_factory=list)
    coefficient: Text | None = None
    trials_column: Text | None = None
    model_id: Text | None = None
    interval_level: Level | None = None
    threshold: Number | None = None
    operator: Literal['gt', 'ge', 'lt', 'le'] = 'gt'
    alternative: Literal['two_sided', 'greater', 'less'] = 'two_sided'
    missing_policy: Literal['error', 'complete_case', 'custom'] = 'error'
    design_evidence: str = Field(default='', json_schema_extra={'readOnly': True})
    design_evidence_refs: list[Text] = Field(default_factory=list)
    unit: Text | None = None
    required: bool = True
    method_locked: bool = True
    result_key: Text | None = None
    required_diagnostics: list[Text] = Field(default_factory=list)
    equivalent_component: ComponentSpec | None = None


class FigureMapping(Object):
    series_column: Text | None = None
    category_column: Text
    facets: list[Text] = Field(default_factory=list)
    xscale: Literal['linear', 'log'] = 'linear'
    value_label: Text | None = None
    value_labels: dict[str, Text] = Field(default_factory=dict)
    category_label: Text | None = None
    display_labels: dict[str, Text] = Field(default_factory=dict)


class FigurePlan(Object):
    id: Text
    title: Text
    caption: Text
    kind: Text
    style: Text
    base_style: Text | None = None
    render_mode: Literal['template', 'code'] = 'template'
    finding_ids: list[Text]
    columns: list[ColumnRef]
    instructions: Text
    data_mapping: FigureMapping | None = None


class Join(Object):
    id: Text
    left_table: Text
    right_table: Text
    left_keys: list[Text]
    right_keys: list[Text]
    how: Literal['left', 'inner']
    rationale: Text


class Plan(Object):
    targets: list[AnalysisTarget]
    models: list[ModelSummary]
    methods: list[Text]
    limitations: list[Text]
    jobs: list[Job]
    task_groups: dict[str, list[Text]] = Field(default_factory=dict)
    requirements: list[Requirement] = Field(default_factory=list)
    figure_requests: list[FigureRequest] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)
    figures: list[FigurePlan] = Field(default_factory=list)
    multiplicity: Literal['none', 'holm'] = 'none'
    missing_materials: list[Text] = Field(default_factory=list)
    unavailable_method: Text | None = None

    @model_validator(mode='after')
    def sizes(self):
        if len(self.jobs) > 1000 or len(self.figures) > 20 or len(self.joins) > 10:
            raise ValueError('分析任务或图表过多')
        return self


def validate_plan(value, inputs, *, figures=True, findings=None):
    if isinstance(value, dict) and set(value) == {'missing_materials'}:
        raise material_claim(value['missing_materials'])
    value = copy.deepcopy(value)
    if isinstance(value, dict):
        items = [item for field in ('targets', 'figures') if isinstance(value.get(field), list)
                 for item in value[field]]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get('columns'), list):
                # Repeating the exact same provenance reference carries no additional evidence.
                refs = {json.dumps(ref, sort_keys=True): ref for ref in item['columns']}
                item['columns'] = list(refs.values())
    # Figure syntax/appearance must not prevent a valid statistical plan from running.
    figure_values = copy.deepcopy(value.get('figures', [])) if isinstance(value, dict) else []
    plan = parse(Plan, value if figures or not isinstance(value, dict) else {**value, 'figures': []})
    if plan['unavailable_method']:
        raise ContractError('EXECUTION_FAILED', 'analysis.method', plan['unavailable_method'])
    if plan['missing_materials']:
        raise material_claim(plan['missing_materials'])
    sources = table_map(inputs)
    tables, _, _ = materialize(inputs, plan['joins'])
    targets, models, jobs = (unique(plan[k], 'plan.' + k) for k in ('targets', 'models', 'jobs'))
    if not jobs or not targets or not plan['methods']:
        raise ValueError('分析计划缺少目标、计算任务或方法说明')
    if {j['target_id'] for j in jobs.values()} != targets.keys():
        raise ValueError('目标与计算任务不对应')
    for target in targets.values():
        refs_exist(target['columns'], sources, 'plan.targets')
    for job in jobs.values():
        bind_job_columns(job, tables)
    for model in models.values():
        if not set(model['target_ids']) <= targets.keys():
            raise ValueError('模型引用未知目标')
        model_jobs = [j for j in jobs.values() if j['model_id'] == model['id']]
        if not model_jobs:
            raise ValueError('计划包含未实际使用的模型')
        if model['priors'] and any(not uses_program(j) and not j['operation'].startswith('beta_') for j in model_jobs):
            raise ValueError('当前已注册先验复核仅适用于解析 Beta 模型')
        if model['priors'] and all(not uses_program(j) for j in model_jobs):
            fields = ('table_id', 'column', 'filters', 'trials_column', 'missing_policy')
            if any(any(j[k] != model_jobs[0][k] for k in fields) for j in model_jobs[1:]):
                raise ValueError('同一后验模型的任务必须使用相同观测范围；不同范围需要不同模型 ID')
    missing = []
    catalog = {item['id']: item for item in evidence_catalog(inputs)}
    for job in jobs.values():
        missing.extend(bind_job_fields(job, tables, catalog, bind_unit=not uses_program(job)))
        inference = (job['inference'] or job['interval_level'] is not None
                     or job['correlation_test']
                     or job['operation'] in ('paired_t', 'welch_t', 'ols_coefficient')
                     or job['operation'].startswith('beta_'))
        if (inference or paired_operation(job['operation'])) and not job['design_evidence_refs']:
            missing.append(dict(job_id=job['id'], field='design_evidence_refs',
                                message='从 evidence_catalog 选择已有实验单位和依赖结构依据的编号；不要改写原文或修改其他字段'))
        if paired_operation(job['operation']) and not job['pair_by']:
            missing.append(dict(job_id=job['id'], field='pair_by',
                                message='根据已有设计事实补充配对键；不要修改其他字段'))
    for job in jobs.values():
        try:
            validate_job(job, tables, sources, targets, models)
            if job['equivalent_component']:
                fallback = component_job(job)
                validate_job(fallback, tables, sources, targets, models)
        except (ValueError, KeyError, TypeError) as exc:
            missing.append(dict(job_id=job['id'], field=None, message=str(exc)))
    program_ids = [j['id'] + ':' + o['id'] for j in jobs.values() for o in j['outputs']]
    exported_ids = program_ids + [j['id'] for j in jobs.values() if not uses_program(j)]
    if len(set(exported_ids)) != len(exported_ids) or set(program_ids) & jobs.keys():
        raise ValueError('任务及输出 ID 冲突；使用互不重叠的任务/输出标识')
    correlation_tests = set()
    for job in jobs.values():
        if job['correlation_test'] and not uses_program(job):
            signature = (job['operation'], job['table_id'], tuple(sorted((job['column'], job['other_column']))),
                         json.dumps(sorted(job['filters'], key=lambda f: json.dumps(f, sort_keys=True)), sort_keys=True))
            if signature in correlation_tests:
                raise ValueError('同一范围的同一变量对不能反向重复检验或重复计入校正')
            correlation_tests.add(signature)
    unique(plan['figures'], 'plan.figures')
    result_ids = ({f['id'] for f in findings} if findings is not None else
                  set(jobs) | {j['id'] + ':' + o['id'] for j in jobs.values() for o in j['outputs']})
    for figure in plan['figures']:
        if not figure['finding_ids'] or not set(figure['finding_ids']) <= result_ids:
            raise ValueError('结果图须引用实际计算任务')
        refs_exist(figure['columns'], sources, 'plan.figures.columns')
        if all(identifier in jobs and not uses_program(jobs[identifier]) for identifier in figure['finding_ids']):
            missing.extend(bind_figure_mapping(figure, jobs, sources))
        # Display language is a presentation preference, not a statistical prerequisite.
    if not figures:
        plan['figures'] = figure_values
    if missing:
        raise PlanRepairError(missing, plan=plan)
    return plan


def paired_operation(operation):
    return operation in ('paired_difference', 'paired_t')


def validate_job(job, tables, sources, targets, models):
    columns = [job['column'], *job['columns'], *job['pair_by'], *job['predictors'],
               *[f['column'] for f in job['filters'] + job['baseline_filters']]]
    if job['other_column']:
        columns.append(job['other_column'])
    if job['trials_column']:
        columns.append(job['trials_column'])
    refs_exist([dict(table_id=job['table_id'], column=c) for c in dict.fromkeys(columns)], tables, 'plan.jobs')
    tab = tables[job['table_id']]
    target_refs = {(ref['table_id'], ref['column']) for ref in targets[job['target_id']]['columns']}
    numeric_columns = [job['column'], *job['columns'], *job['predictors']]
    if job['other_column']:
        numeric_columns.append(job['other_column'])
    if job['trials_column']:
        numeric_columns.append(job['trials_column'])
    required_refs = {(job['table_id'], c) if job['table_id'] in sources else tuple(c.split('.', 1))
                     for c in numeric_columns}
    if not required_refs <= target_refs:
        raise ValueError('分析目标遗漏实际计算的响应、自变量或试验次数来源列')
    for rule in job['filters'] + job['baseline_filters']:
        kind = tab['column_types'][tab['columns'].index(rule['column'])]
        if rule['operator'] == 'not_null':
            if rule['value'] is not None:
                raise ValueError('not_null 不能附带比较值')
            continue
        values = rule['value'] if rule['operator'] == 'in' else [rule['value']]
        if not isinstance(values, list) or not values:
            raise ValueError('in 比较必须提供非空列表')
        permitted = {'integer': (int,), 'number': (int, float), 'boolean': (bool,),
                     'string': (str,), 'date': (str,), 'datetime': (str,)}[kind]
        if any(v is not None and type(v) not in permitted for v in values):
            raise ValueError('筛选值类型与原始列不一致')
    model = models.get(job['model_id'])
    if job['model_id'] is not None and (model is None or job['target_id'] not in model['target_ids']):
        raise ValueError('任务引用的模型无效')
    if uses_program(job):
        if {'analysis', 'baseline', 'combined'} & job['inputs'].keys():
            raise ValueError('附加输入不能覆盖 analysis、baseline 或 combined')
        for name, source in job['inputs'].items():
            refs_exist([dict(table_id=source['table_id'], column=c) for c in source['columns']], tables, 'plan.inputs')
            refs = {(source['table_id'], c) if source['table_id'] in sources else tuple(c.split('.', 1))
                    for c in source['columns']}
            if not refs <= target_refs:
                raise ValueError('分析目标遗漏附加输入来源列: ' + name)
        if not job['procedure'].strip() or not job['outputs']:
            raise ValueError('代码任务须说明 procedure 和非空 outputs；可以一次输出多个系数、分组或诊断数值')
        unique(job['outputs'], 'plan.jobs.outputs')
        return
    if job['operation'] not in BUILTIN_OPERATIONS:
        raise ValueError('组件名称未知；专用方法须声明 execution=code')
    if job['inputs'] or job['outputs'] or job['procedure'] or job['missing_policy'] == 'custom':
        raise ValueError('自定义输出或缺失处理需要填写 procedure，使用代码任务')
    op = job['operation']
    if op in ('pearson', 'spearman'):
        if not job['other_column']:
            raise ValueError('相关分析必须声明两个数值列，相关系数无量纲，unit 必须为 null')
        if job['baseline_filters'] or job['pair_by'] or job['predictors'] or job['trials_column']:
            raise ValueError('相关分析使用同一行的两个变量，不接受组间基线、匹配键或回归字段')
        if tab['column_types'][tab['columns'].index(job['other_column'])] not in ('integer', 'number'):
            raise ValueError('相关分析的第二个变量必须为数值列')
        if op == 'spearman' and job['interval_level'] is not None:
            raise ValueError('Spearman 区间尚未注册；不能使用 Pearson Fisher 区间替代')
        if job['column'] == job['other_column'] and (job['correlation_test'] or job['interval_level']):
            raise ValueError('自相关对角线只能作描述，不检验也不附加区间')
    elif job['other_column'] is not None or job['correlation_test']:
        raise ValueError('other_column/correlation_test 仅用于相关分析')
    if op != 'count':
        tab = tables[job['table_id']]
        if tab['column_types'][tab['columns'].index(job['column'])] not in ('integer', 'number'):
            raise ValueError('统计响应列必须为数值类型')
    if op in ('difference', 'paired_difference', 'paired_t', 'welch_t') and not job['baseline_filters']:
        raise ValueError('比较任务缺少基线范围')
    if op.startswith('beta_'):
        if (not model or not model['likelihood'] or len(model['priors']) != 1
                or model['priors'][0]['distribution'] != 'beta'):
            raise ValueError('Beta 模型需要一个已声明的 beta 先验和二元/计数似然')
        if model['priors'][0]['source'] == 'historical':
            raise ValueError('历史先验的数据构建与去重检查尚未注册，不能作为默认先验静默替代')
        if op == 'beta_probability' and job['threshold'] is None:
            raise ValueError('后验事件需要明确阈值')
    if op == 'ols_coefficient' and (
        not model or not job['predictors'] or job['coefficient'] not in ['intercept', *job['predictors']]
    ):
        raise ValueError('回归任务缺少模型、自变量或系数名称')
    if op == 'count' and job['interval_level'] is not None:
        raise ValueError('计数任务不能自动附加推断区间')
