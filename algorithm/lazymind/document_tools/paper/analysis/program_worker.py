"""Standalone runner: host-owned DataFrame loading, result collection and serialization."""
import contextlib
import io
import json
import sys
from types import MappingProxyType

INTERFACE = {
    'function': 'analyze', 'arguments': ['frames', 'params', 'rng'],
    'frames': {'analysis': 'Selected observations as a pandas.DataFrame with original row indices.',
               'baseline': 'Only for comparisons: baseline observations, separately selected.',
               'combined': 'Only for comparisons: union of analysis and baseline, original indices retained.'},
    'params': 'A read-only Mapping containing the locked task parameters. Use params["column"], never params.column. Do not mutate it.',
    'rng': 'The supplied numpy.random.Generator; use it directly for resampling without reseeding.',
}


def run(payload):
    import numpy as np
    import pandas as pd
    task, table = payload['task'], payload['table']
    original = json.dumps(payload, sort_keys=True, allow_nan=False)
    frame = pd.DataFrame(table['rows'], columns=table['columns'], index=payload['row_ids'])
    findings, notes = [], []

    def emit(key, value, rows, *, unit=None, kind='estimate', interval=None, test=None,
             probability=None, n=None, sample_unit='observation'):
        # Convert only library scalar types; unsupported arrays and non-finite values fail visibly.
        def indices_of(values):
            return [int(i) if isinstance(i, np.integer) else i for i in values]
        indices = {name: indices_of(values) for name, values in rows.items()} if isinstance(rows, dict) else indices_of(rows)
        findings.append(dict(key=key, value=float(value) if value is not None else None,
            rows=indices, unit=unit, kind=kind, interval=interval, test=test, probability=probability,
            n=int(n) if isinstance(n, np.integer) else n, sample_unit=sample_unit))

    def diagnose(name, outcome, details, value=None, blocking=False):
        notes.append(dict(name=name, outcome=outcome, details=details, value=value, blocking=blocking))

    frames = {'analysis': frame.loc[task['selected_rows']].copy()}
    if task['baseline_filters']:
        frames.update(baseline=frame.loc[task['baseline_rows']].copy(), combined=frame.copy())
    frames.update({name: pd.DataFrame(frame['rows'], columns=frame['columns'], index=frame['row_ids'])
                   for name, frame in payload['additional_frames'].items()})
    params = {key: value for key, value in task.items() if key not in ('selected_rows', 'baseline_rows')}
    params_before = json.dumps(params, sort_keys=True, allow_nan=False)
    namespace = {'__name__': 'paper_analysis_task', 'emit': emit, 'diagnose': diagnose}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(payload['code'], '<analysis-task>', 'exec'), namespace)
        namespace[INTERFACE['function']](frames, MappingProxyType(params), np.random.default_rng(42))
    if (original != json.dumps(payload, sort_keys=True, allow_nan=False)
            or params_before != json.dumps(params, sort_keys=True, allow_nan=False)):
        raise ValueError('代码修改了锁定任务或输入；只允许修改工作数据副本')
    return dict(findings=findings, diagnostics=notes)


if __name__ == '__main__':
    try:
        print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(json.dumps({'error': f'{type(exc).__name__}: {str(exc)[-4000:]}'}, ensure_ascii=False))
        sys.exit(1)
