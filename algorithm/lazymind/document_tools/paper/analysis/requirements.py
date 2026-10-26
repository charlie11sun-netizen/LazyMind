"""Request-to-result bindings; completion checks use facts, never model self-reported success."""
import re
from typing import Literal

from pydantic import Field

from .schema import Object, Text, ContractError, digest, unique


class ExpectedResult(Object):
    task_id: Text
    keys: list[Text] = Field(default_factory=list)
    fields: list[Literal['value', 'interval', 'test', 'probability']] = Field(default_factory=lambda: ['value'])


class Requirement(Object):
    id: Text
    source_ids: list[Text] = Field(min_length=1)
    description: Text
    results: list[ExpectedResult] = Field(default_factory=list)
    figure_ids: list[Text] = Field(default_factory=list)
    presentation: Literal['language', 'interpretation', 'format'] | None = None


class FigureRequest(Object):
    id: Text
    purpose: Text
    task_ids: list[Text] = Field(min_length=1)
    output_keys: list[Text] = Field(default_factory=list)
    required: bool = True


def request_parts(inputs):
    text = inputs.get('request') or inputs['topic']
    parts = [s.strip() for s in re.split(r'[；;。\n]+', text) if s.strip()]
    return [dict(id='r_' + digest(part)[:12], text=part) for part in dict.fromkeys(parts)]


def bind_requirements(plan, inputs):
    requirements = unique(plan['requirements'], 'requirements')
    sources = {p['id'] for p in request_parts(inputs)}
    covered = set()
    tasks = plan['task_groups']
    figures = unique(plan['figure_requests'], 'figure_requests')
    jobs = {job['id']: job for job in plan['jobs']}
    required_tasks = set()
    for requirement in requirements.values():
        ids = set(requirement['source_ids'])
        if not ids <= sources:
            raise ValueError('需求引用了不存在的 request_parts 编号')
        covered |= ids
        if not requirement['results'] and not requirement['figure_ids'] and not requirement['presentation']:
            raise ValueError('每项需求必须绑定结果、图表或明确的呈现要求')
        for expected in requirement['results']:
            task = expected['task_id']
            if task not in tasks:
                raise ValueError('需求引用了不存在的分析任务')
            required_tasks.add(task)
            keys = {key for identifier in tasks[task] for key in
                    ([o['id'] for o in jobs[identifier]['outputs']] or [jobs[identifier]['result_key']])}
            if not set(expected['keys']) <= keys:
                raise ValueError('需求引用了未声明的结果键: ' + task)
        for identifier in requirement['figure_ids']:
            if identifier not in figures:
                raise ValueError('需求引用了不存在的图表意图')
            figures[identifier]['required'] = True
            required_tasks.update(figures[identifier]['task_ids'])
    if covered != sources:
        raise ValueError('需求清单遗漏原始请求片段: ' + ', '.join(sorted(sources - covered)))
    for figure in figures.values():
        if not set(figure['task_ids']) <= tasks.keys():
            raise ValueError('图表意图引用未知任务')
        keys = {key for task in figure['task_ids'] for identifier in tasks[task] for key in
                ([o['id'] for o in jobs[identifier]['outputs']] or [jobs[identifier]['result_key']])}
        if not set(figure['output_keys']) <= keys:
            raise ValueError('图表意图引用未声明的结果键')
        if figure['required']:
            required_tasks.update(figure['task_ids'])
    for task in required_tasks:
        for identifier in tasks[task]:
            jobs[identifier]['required'] = True
    return plan


def result_ids(plan, task_ids, keys=()):
    jobs = {j['id']: j for j in plan['jobs']}
    result = []
    for task in task_ids:
        for identifier in plan['task_groups'].get(task, []):
            job = jobs.get(identifier)
            if job is None:
                continue
            if job['outputs']:
                result.extend(identifier + ':' + item['id'] for item in job['outputs']
                              if not keys or item['id'] in keys)
            elif not keys or job['result_key'] in keys:
                result.append(identifier)
    return list(dict.fromkeys(result))


def check_results(plan, analysis):
    findings = {f['id']: f for f in analysis['findings']}
    ledger = []
    for requirement in plan['requirements']:
        supported = []
        for expected in requirement['results']:
            ids = result_ids(plan, [expected['task_id']], expected['keys'])
            if not ids or not set(ids) <= findings.keys():
                raise ContractError('INCOMPLETE_STATISTICS', 'analysis.requirements',
                                    '需求尚未完成: ' + requirement['description'])
            for identifier in ids:
                missing = [field for field in expected['fields'] if findings[identifier].get(field) is None]
                if missing:
                    raise ContractError('INCOMPLETE_STATISTICS', 'analysis.requirements',
                                        f'{identifier} 缺少必需结果字段: {", ".join(missing)}')
            supported.extend(ids)
        ledger.append(dict(id=requirement['id'], source_ids=requirement['source_ids'],
                           description=requirement['description'], finding_ids=list(dict.fromkeys(supported)),
                           figure_ids=requirement['figure_ids'], presentation=requirement['presentation']))
    return ledger


def check_delivery(plan, analysis, charts):
    ledger = check_results(plan, analysis)
    actual = {chart['id'] for chart in charts}
    expected = {f['id'] for f in plan['figure_requests'] if f['required']}
    if not expected <= actual:
        raise ContractError('INCOMPLETE_OUTPUT', 'analysis.requirements.figures',
                            '尚未交付必需图表: ' + ', '.join(sorted(expected - actual)))
    return ledger
