"""Compact model-authored tasks compiled into independently checked execution jobs."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from itertools import combinations
from typing import Annotated, Literal

from pydantic import Field

from .schema import Object, Text, Level, Number, AnalysisTarget, ModelSummary, parse, digest, unique
from .plan import Filter, Join, ResultSpec, Job, ComponentSpec, InputBinding
from .requirements import Requirement, FigureRequest, bind_requirements
from .plan_repair import material_claim
from .reference import select
from .views import materialize

MAX_CALCULATIONS = 1000
Names = Annotated[list[Text], Field(min_length=1, max_length=200)]


class Task(Object):
    id: Text
    target_id: Text
    table_id: Text
    filters: list[Filter] = Field(default_factory=list)
    design_evidence_refs: list[Text] = Field(default_factory=list)
    required: bool = True
    method_origin: Literal['requested', 'selected'] = 'requested'


class GroupedTask(Task):
    group_by: list[Text] = Field(default_factory=list)
    missing_policy: Literal['error', 'complete_case'] = 'error'
    interval_level: Level | None = None


class SummaryTask(GroupedTask):
    kind: Literal['summary']
    operation: Literal['mean', 'count']
    metrics: Names


class ComparisonTask(GroupedTask):
    kind: Literal['comparison']
    operation: Literal['difference', 'paired_difference', 'welch_t', 'paired_t']
    metrics: Names
    baseline_filters: Annotated[list[Filter], Field(min_length=1)]
    pair_by: list[Text] = Field(default_factory=list)
    alternative: Literal['two_sided', 'greater', 'less'] = 'two_sided'


class Pair(Object):
    column: Text
    other_column: Text


class CorrelationTask(GroupedTask):
    kind: Literal['correlation']
    operation: Literal['pearson', 'spearman']
    variables: list[Text] = Field(default_factory=list, max_length=200)
    pairs: list[Pair] = Field(default_factory=list, max_length=1000)
    diagonal: bool = False
    correlation_test: bool = False
    alternative: Literal['two_sided', 'greater', 'less'] = 'two_sided'


class RegressionTask(GroupedTask):
    kind: Literal['regression']
    operation: Literal['ols_coefficient'] = 'ols_coefficient'
    column: Text
    predictors: Names
    coefficients: Names
    model_id: Text


class ProbabilityTask(GroupedTask):
    kind: Literal['probability']
    operation: Literal['beta_mean', 'beta_probability']
    column: Text
    model_id: Text
    trials_column: Text | None = None
    threshold: Number | None = None
    operator: Literal['gt', 'ge', 'lt', 'le'] = 'gt'


class CodeTask(Task):
    inputs: dict[Text, InputBinding] = Field(default_factory=dict)
    kind: Literal['code']
    operation: Text
    column: Text
    columns: list[Text] = Field(default_factory=list)
    procedure: Text
    outputs: Annotated[list[ResultSpec], Field(min_length=1, max_length=200)]
    baseline_filters: list[Filter] = Field(default_factory=list)
    pair_by: list[Text] = Field(default_factory=list)
    missing_policy: Literal['error', 'complete_case', 'custom'] = 'error'
    inference: bool = False
    model_id: Text | None = None
    required_diagnostics: list[Text] = Field(default_factory=list)
    equivalent_component: ComponentSpec | None = None


TaskSpec = Annotated[SummaryTask | ComparisonTask | CorrelationTask | RegressionTask | ProbabilityTask | CodeTask,
                     Field(discriminator='kind')]


class CompactPlan(Object):
    targets: list[AnalysisTarget]
    models: list[ModelSummary] = Field(default_factory=list)
    methods: list[Text]
    limitations: list[Text] = Field(default_factory=list)
    tasks: Annotated[list[TaskSpec], Field(min_length=1, max_length=100)]
    joins: Annotated[list[Join], Field(max_length=10)] = Field(default_factory=list)
    requirements: list[Requirement] = Field(min_length=1, max_length=100)
    figure_requests: list[FigureRequest] = Field(default_factory=list, max_length=20)
    multiplicity: Literal['none', 'holm'] = 'none'
    missing_materials: list[Text] = Field(default_factory=list)
    unavailable_method: Text | None = None


def distinct(names, label):
    if len(set(names)) != len(names):
        raise ValueError(label + ' 不能重复')


def cells(task):
    """Emit only requested estimands; no implicit tests or additional statistics."""
    kind = task['kind']
    if kind in ('summary', 'comparison'):
        distinct(task['metrics'], 'metrics')
        return [dict(column=c) for c in task['metrics']]
    if kind == 'correlation':
        variables, pairs = task['variables'], task['pairs']
        if bool(variables) == bool(pairs):
            raise ValueError('相关任务须选择 variables 或 pairs，不能同时使用或同时为空')
        distinct(variables, 'variables')
        if variables and len(variables) < 2 and not task['diagonal']:
            raise ValueError('相关矩阵至少需要两个变量，或明确请求描述性对角线')
        result = ([dict(column=a, other_column=b) for a, b in combinations(variables, 2)]
                  if variables else copy.deepcopy(pairs))
        signatures = [tuple(sorted((p['column'], p['other_column']))) for p in result]
        distinct(signatures, '相关变量对')
        if task['diagonal']:
            if not variables:
                raise ValueError('diagonal 仅适用于 variables 矩阵')
            result.extend(dict(column=c, other_column=c, correlation_test=False, interval_level=None)
                          for c in variables)
        return result
    if kind == 'regression':
        distinct(task['predictors'], 'predictors')
        distinct(task['coefficients'], 'coefficients')
        if not set(task['coefficients']) <= {'intercept', *task['predictors']}:
            raise ValueError('coefficients 必须为 intercept 或实际 predictor')
        return [dict(column=task['column'], coefficient=c) for c in task['coefficients']]
    return [dict(column=task['column'])]


def groups(task, table):
    keys = task.get('group_by', [])
    distinct(keys, 'group_by')
    if not keys:
        return [[]]
    if not set(keys) <= set(table['columns']):
        raise ValueError('group_by 引用了不存在的列')
    indices = [table['columns'].index(key) for key in keys]
    selected = select(table, task['filters'])
    if task.get('baseline_filters'):
        # Include baseline-only cells so missing requested comparisons fail visibly.
        selected += select(table, task['baseline_filters'])
    observed = {}
    for i in selected:
        values = [table['rows'][i][j] for j in indices]
        # Match select() equality: 1 and 1.0 (or equivalent zoned instants) are one group.
        # Integral floats become ints without rounding distinct large integer values.
        values = [(int(value) if isinstance(value, float) and value.is_integer() else
                   datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
                   if value is not None and table['column_types'][j] == 'datetime' else value)
                  for value, j in zip(values, indices)]
        observed[digest(values)] = [dict(column=k, operator='eq', value=v) for k, v in zip(keys, values)]
    if not observed:
        raise ValueError('分组范围内没有实际观测')
    # Stable IDs/order independent of the first observed row; no Cartesian product of levels.
    return [observed[key] for key in sorted(observed)]


def compile_plan(value, inputs):
    """The only model planning protocol. Execution Job is internal, not a legacy input path."""
    if isinstance(value, dict) and set(value) == {'missing_materials'}:
        raise material_claim(value['missing_materials'])
    compact = parse(CompactPlan, value, 'analysis.compact_plan')
    if compact['missing_materials']:
        raise material_claim(compact['missing_materials'])
    unique(compact['tasks'], 'tasks')
    tables = materialize(inputs, compact['joins'])[0]
    models = unique(compact['models'], 'models')
    compiled_models = copy.deepcopy(models)
    jobs, task_groups = [], {}
    for task in compact['tasks']:
        if task['table_id'] not in tables:
            raise ValueError('任务引用不存在的表: ' + task['table_id'])
        variations = cells(task)
        scopes = groups(task, tables[task['table_id']])
        if len(jobs) + len(scopes) * len(variations) > MAX_CALCULATIONS:
            raise ValueError(f'展开后的计算项超过 {MAX_CALCULATIONS}，不能截断或丢弃结果')
        task_groups[task['id']] = []
        for scope in scopes:
            base = {k: copy.deepcopy(v) for k, v in task.items() if k in Job.model_fields}
            base['execution'] = 'code' if task['kind'] == 'code' else 'component'
            base['method_locked'] = task['method_origin'] == 'requested'
            base['filters'] = task['filters'] + scope
            if task.get('baseline_filters'):
                base['baseline_filters'] = task['baseline_filters'] + scope
            if scope and task.get('model_id'):
                model_id = task['model_id']
                if model_id not in models:
                    raise ValueError('任务引用不存在的模型: ' + model_id)
                derived = model_id + '__' + digest(dict(task=task['id'], scope=scope))[:16]
                if derived in compiled_models:
                    raise ValueError('派生模型 ID 冲突')
                compiled_models[derived] = {**copy.deepcopy(models[model_id]), 'id': derived}
                base['model_id'] = derived
            for cell in variations:
                leaf = {**base, **cell}
                leaf['result_key'] = (cell.get('coefficient') or
                    (cell['column'] + '|' + cell['other_column'] if cell.get('other_column') else cell['column']))
                leaf['id'] = (task['id'] if len(scopes) == len(variations) == 1 else
                              task['id'] + '__' + digest(dict(scope=scope, cell=cell))[:16])
                jobs.append(parse(Job, leaf, 'analysis.compiled_job'))
                task_groups[task['id']].append(leaf['id'])
    unique(jobs, 'compiled_jobs')
    used_models = {job['model_id'] for job in jobs}
    result = {k: copy.deepcopy(v) for k, v in compact.items() if k != 'tasks'}
    result.update(jobs=jobs, task_groups=task_groups,
                  models=[model for key, model in compiled_models.items() if key in used_models],
                  figures=[])
    return bind_requirements(result, inputs)
