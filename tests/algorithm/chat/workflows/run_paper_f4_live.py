"""Frozen real-model F4 acceptance. No mocks, deployment changes or case deletion.
Run inside the analysis image with configured model credentials; results remain separate for every repetition.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

MANIFEST = Path(__file__).resolve().parents[3] / 'fixtures/paper_f4/live_cases.json'


def expected(case):
    import numpy as np
    from scipy import stats
    import statsmodels.api as sm
    rule = case['oracle']; rows = case['input']['data'].get('rows', [])
    kind = rule.get('kind')
    values = list(rule.get('values', [])); tests = []; intervals = []
    if kind in ('correlation', 'matrix'):
        a = np.array(rows, dtype=float)
        pairs = [(0, 1)] if kind == 'correlation' else [(0, 1), (0, 2), (1, 2)]
        fn = stats.spearmanr if rule.get('method') == 'spearman' else stats.pearsonr
        values = [float(fn(a[:, i], a[:, j]).statistic) for i, j in pairs]
    elif kind == 'ols':
        a = np.array(rows, dtype=float)
        values = sm.OLS(a[:, 1], sm.add_constant(a[:, 0])).fit().params.tolist()
    elif kind == 'logistic':
        a = np.array(rows, dtype=float)
        fit = sm.Logit(a[:, 1], sm.add_constant(a[:, 0])).fit(disp=False)
        b = float(fit.params[1]); lo, hi = fit.conf_int()[1]
        values = [b, math.exp(b)]
        intervals = [(b, float(lo), float(hi)), (math.exp(b), math.exp(lo), math.exp(hi))]
    elif kind in ('chi2', 'fisher'):
        table = [[sum(r == [g, y] for r in rows) for y in (0, 1)] for g in ('a', 'b')]
        stat = stats.chi2_contingency(table, correction=False) if kind == 'chi2' else stats.fisher_exact(table)
        tests = [(float(stat.statistic), float(stat.pvalue))]
    elif kind == 'mannwhitney':
        stat = stats.mannwhitneyu([r[1] for r in rows if r[0] == 'a'], [r[1] for r in rows if r[0] == 'b'],
                                 method='exact', alternative='two-sided')
        tests = [(float(stat.statistic), float(stat.pvalue))]
    elif kind == 'anova':
        stat = stats.f_oneway(*[[r[1] for r in rows if r[0] == g] for g in ('a', 'b', 'c')])
        tests = [(float(stat.statistic), float(stat.pvalue))]
    if rule.get('bootstrap'):
        a = np.array(rows)[:, 0]
        means = np.random.default_rng(42).choice(a, size=(2000, len(a)), replace=True).mean(axis=1)
        lo, hi = np.quantile(means, [.025, .975]); intervals = [(float(a.mean()), float(lo), float(hi))]
    return values, tests, intervals


def verify(case, result):
    findings = result['analysis']['findings']; errors = []
    values, tests, intervals = expected(case)
    available = [f['value'] for f in findings if f['value'] is not None]
    for value in values:
        match = next((i for i, v in enumerate(available) if math.isclose(v, value, rel_tol=1e-5, abs_tol=1e-6)), None)
        if match is None: errors.append(f'missing/wrong value {value:.8g}')
        else: available.pop(match)
    for stat, p in tests:
        actual = [(f['test']['statistic'], f['test']['p_value']) for f in findings if f['test']]
        if not any(s is not None and math.isclose(s, stat, rel_tol=1e-5, abs_tol=1e-6)
                   and math.isclose(q, p, rel_tol=1e-4, abs_tol=1e-8) for s, q in actual):
            errors.append('missing/wrong test statistic or p value')
    for value, lo, hi in intervals:
        tolerance = .7 if case['oracle'].get('bootstrap') else 1e-4
        matches = [f['interval'] for f in findings if f['value'] is not None
                   and math.isclose(f['value'], value, rel_tol=1e-5) and f['interval']]
        if not any(abs(i['lower'] - lo) <= tolerance and abs(i['upper'] - hi) <= tolerance
                   and i['kind'] == 'confidence' and i['level'] == .95 for i in matches):
            errors.append('missing/wrong interval')
    if len(result['charts']) < case['min_charts']: errors.append('missing requested figure')
    if case['oracle'].get('ns') and sorted(f['sample_size']['n'] for f in findings) != sorted(case['oracle']['ns']):
        errors.append('incorrect sample sizes')
    for chart in result['charts']:
        if '<svg' not in chart['svg'] or not chart['finding_ids']: errors.append('invalid figure')
    if not result['analysis']['summary'].strip(): errors.append('missing interpretation')
    return errors


def worker(case, output, repetition, config_path):
    from lazymind.model_config import inject_model_config
    from lazymind.document_tools.paper.analysis import pipeline, trace, recovery
    output.mkdir(parents=True, exist_ok=False)
    configuration = json.loads(Path(config_path).read_text())
    if set(configuration) != {'llm'} or not configuration['llm'].get('model'):
        raise ValueError('验收须复用完整的当前 llm 配置，不能猜模型或环境变量凭据')
    inject_model_config(configuration)
    start = time.monotonic()
    record = dict(case=case['id'], repetition=repetition, model=configuration['llm']['model'],
                  source=configuration['llm'].get('source'))
    try:
        with trace.capture(output, case['id']), recovery.workspace(output), pipeline.capture_execution() as execution:
            result = pipeline.run(case['input'])
            errors = verify(case, result)
            (output / 'output.json').write_text(json.dumps(result, ensure_ascii=False))
            (output / 'execution.json').write_text(json.dumps(execution, ensure_ascii=False))
        record.update(passed=not errors, errors=errors)
    except Exception as exc:
        record.update(passed=False, errors=[trace.safe_error(exc)])
    record['seconds'] = round(time.monotonic() - start, 2)
    (output / 'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps(record, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--model-config', required=True, help='Private JSON with complete backend-resolved llm configuration')
    parser.add_argument('--case'); parser.add_argument('--repetition', type=int, default=1)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--smoke', action='store_true', help='Only first case once; not acceptance')
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text()); output = Path(args.output)
    if args.case:
        worker(next(c for c in manifest['cases'] if c['id'] == args.case), output, args.repetition, args.model_config); return
    from lazymind.document_tools.paper.analysis.schema import normalize_input
    for case in manifest['cases']:
        normalize_input(case['input'])
        expected(case)
    output.mkdir(parents=True, exist_ok=False)
    (output/'manifest.json').write_bytes(MANIFEST.read_bytes())
    from lazymind.document_tools.paper.analysis.recovery import implementation
    metadata = dict(manifest_sha256=hashlib.sha256(MANIFEST.read_bytes()).hexdigest(), implementation=implementation(),
                    cases=len(manifest['cases']), repetitions=manifest['repetitions'], smoke=args.smoke)
    (output/'metadata.json').write_text(json.dumps(metadata, indent=2))
    cases = manifest['cases'][:1] if args.smoke else manifest['cases']
    def run_one(case, rep):
        directory = output / (case['id'] + '-' + str(rep))
        with (output / (directory.name + '.private.log')).open('w') as stream:
            process = subprocess.run([sys.executable, __file__, '--case', case['id'], '--repetition', str(rep),
                '--output', str(directory), '--model-config', args.model_config], stdout=stream, stderr=stream, timeout=1200)
        path = directory/'result.json'
        if path.exists(): return json.loads(path.read_text())
        return dict(case=case['id'], repetition=rep, passed=False, errors=['worker exited '+str(process.returncode)])
    records=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_one, c, r) for c in cases for r in range(1, (1 if args.smoke else manifest['repetitions'])+1)]
        for future in as_completed(futures):
            try: record = future.result()
            except Exception as exc: record = dict(passed=False, errors=[type(exc).__name__])
            records.append(record)
            summary=dict(total=len(futures), completed=len(records), passed=sum(r['passed'] for r in records), records=records)
            (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
            print(json.dumps({k:v for k,v in record.items() if k!='errors'},ensure_ascii=False),flush=True)
    # Invalid input is a separate rejection check and never added to successful-analysis counts.
    from lazymind.document_tools.paper.analysis.schema import normalize_input
    rejected=[]
    for name, data in [('empty',dict(columns=['x'],column_types=['number'],units=[None],rows=[])),
                       ('inconsistent',dict(columns=['x'],column_types=['number'],units=[None],rows=[[1,2]]))]:
        try: normalize_input(dict(topic='negative',data=data)); rejected.append(dict(case=name,rejected=False))
        except ValueError: rejected.append(dict(case=name,rejected=True))
    (output/'invalid-inputs.json').write_text(json.dumps(rejected))


if __name__ == '__main__': main()
