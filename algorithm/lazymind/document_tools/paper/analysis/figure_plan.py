"""Validate native figure prerequisites and preserve locked estimands during figure repair."""
import copy


def validate_figure(figure, analysis, inputs, jobs):
    """Check only the new figure against immutable findings, never rerun statistical validation."""
    from .plan import FigurePlan, uses_program
    from .schema import parse, refs_exist, table_map
    from .plan_bindings import bind_figure_mapping
    from .plan_repair import PlanRepairError
    figure = parse(FigurePlan, figure, 'analysis.figure')
    findings = {f['id']: f for f in analysis['findings']}
    if not figure['finding_ids'] or not set(figure['finding_ids']) <= findings.keys():
        raise ValueError('图表必须引用已完成的实际结果')
    if len(set(figure['finding_ids'])) != len(figure['finding_ids']):
        raise ValueError('图表结果引用重复')
    sources = table_map(inputs)
    refs_exist(figure['columns'], sources, 'analysis.figure.columns')
    indexed = {j['id']: j for j in jobs}
    if all(identifier in indexed and not uses_program(indexed[identifier]) for identifier in figure['finding_ids']):
        issues = bind_figure_mapping(figure, indexed, sources)
        if issues:
            raise PlanRepairError(issues)
    return figure


class FigurePlanRepairError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__('；'.join(f'{issue["figure_id"]}: {issue["message"]}' for issue in issues))


def check_figure_plan(plan, references):
    jobs = {job['id']: job for job in plan['jobs']}
    issues = []
    for figure in plan['figures']:
        if figure['style'] != 'bar_paired_delta':
            continue
        selected = [jobs[identifier] for identifier in figure['finding_ids'] if identifier in jobs]
        if len(selected) != len(figure['finding_ids']) or any(
            job['operation'] != 'mean' or job['id'] not in references for job in selected) or any(
            references[job['id']][0]['value'] < 0 for job in selected
        ):
            issues.append(dict(figure_id=figure['id'], message=(
                'bar_paired_delta 的原生函数要求非负的基线与方法绝对均值，不能接收差值作为两组原始值。'
                '保留全部 finding_ids 和图的研究目的，由模型改选可显示现有估计与区间的合适图型；'
                '不得伪造零基线观测、改数值、丢掉负结果或修改统计任务。')))
    if issues:
        raise FigurePlanRepairError(issues)


def apply_figure_updates(plan, response, issues):
    from .repair_contract import FigurePatch
    response = FigurePatch.model_validate(response).model_dump(exclude_unset=True)
    allowed = {issue['figure_id'] for issue in issues}
    previous = {figure['id']: figure for figure in plan['figures']}
    updates = {}
    for update in response['updates']:
        if not isinstance(update, dict) or set(update) != {'figure_id', 'figure'}:
            raise ValueError('图表修复项须包含 figure_id/figure')
        identifier, figure = update['figure_id'], update['figure']
        if identifier not in allowed or identifier in updates or not isinstance(figure, dict):
            raise ValueError('图表局部修复越出指定范围')
        if (figure.get('id') != identifier or figure.get('finding_ids') != previous[identifier]['finding_ids']
                or figure.get('columns') != previous[identifier]['columns']):
            raise ValueError('图表修复不得改变 ID、原始列引用或已选结果及其顺序')
        updates[identifier] = copy.deepcopy(figure)
    if updates.keys() != allowed:
        raise ValueError('图表修复缺少指定图表')
    result = copy.deepcopy(plan)
    result['figures'] = [updates.get(figure['id'], figure) for figure in result['figures']]
    return result
