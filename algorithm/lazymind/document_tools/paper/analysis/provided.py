"""Interpret supplied aggregate values and reuse registered figures without execution."""
import copy

from . import model, trace as diagnostics
from .schema import ContractError, table_map


def run(inputs):
    from .pipeline import build_analysis, _trace_sink

    descriptions = {item['id']: item['description'] for item in
                    (inputs.get('data') or {}).get('tables', [])}
    plan, rows = {'questions': [], 'figures': []}, []
    for table_id, table in table_map(inputs).items():
        if not any(kind in ('integer', 'number') and any(row[index] is not None for row in table['rows'])
                   for index, kind in enumerate(table['column_types'])):
            continue
        question_id = f'q{len(plan["questions"]) + 1}'
        plan['questions'].append(descriptions.get(table_id) or table_id)
        for row_index, row in enumerate(table['rows'], 1):
            coordinates = {name: value for name, kind, value in zip(
                table['columns'], table['column_types'], row) if kind not in ('integer', 'number')}
            label = ', '.join(f'{name}={value}' for name, value in coordinates.items()) or f'row {row_index}'
            for name, kind, unit, value in zip(table['columns'], table['column_types'], table['units'], row):
                if kind not in ('integer', 'number') or value is None:
                    continue
                rows.append(dict(question_id=question_id, label=f'{label}: {name}', metric=name,
                                 method='Reported aggregate value; not reproduced', value=value, unit=unit,
                                 columns=[{'table_id': table_id, 'column': name}],
                                 scope=f'Supplied table {table_id}, row {row_index}',
                                 coordinates=coordinates,
                                 sample_size={'source_rows': [{'table_id': table_id, 'count': 1}],
                                              'n': None, 'excluded_n': None, 'unit': 'reported aggregate'}))
    if not rows:
        raise ContractError('MISSING_MATERIAL', 'input.data', '已有结果中没有可解读的数值')
    results = {'results': rows, 'models': [], 'limitations': [
        '数据由用户提供，平台未复现实验。', '未提供的样本量、方差和显著性不能从汇总得分推断。']}
    # Reuse the same result/source validation as ordinary analysis, without a worker.
    analysis = build_analysis(inputs, plan, results, executed=False)
    assets = (inputs.get('media_assets') or {}).get('assets', {})
    for asset in assets.values():
        plan['figures'].append({
            'id': asset['media_asset_id'], 'title': asset.get('title') or asset['media_asset_id'],
            'purpose': 'Provided figure, reused without redrawing',
            'caption': '\n\n'.join(filter(None, [asset.get('caption'), asset.get('summary')])) or 'Provided figure'})
    with diagnostics.stage('provided_result_import'):
        diagnostics.save('provided-results.json', analysis)
        diagnostics.save('reused-figures.json', plan['figures'])
    summary, interpretation = model.interpret_results(inputs, copy.deepcopy(analysis), copy.deepcopy(plan))
    analysis['summary'] = summary
    diagnostics.save('analysis.json', analysis)
    diagnostics.save('interpretation.json', interpretation)
    sink = _trace_sink.get()
    if sink is not None:
        sink.append(dict(mode='interpret_provided', input_sha256=analysis['input_sha256'],
                         plan=plan, interpretation=interpretation, figures=[],
                         reused_asset_ids=list(assets), execution_status='not_run'))
    return dict(is_demo=False, analysis=analysis, charts=[])
