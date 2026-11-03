"""Run pinned upstream raters using LazyMind's authenticated model.chat proxy."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import types

from . import transport
from .client import Client

os.umask(0o077)
ROOT = Path(__file__).resolve().parent
UPSTREAM = ROOT / 'upstream'
sys.path.insert(0, str(UPSTREAM))


def load_rater(metric):
    name = 'lit_review_quality' if metric == 'literature' else 'agent_review'
    path = UPSTREAM / 'autoraters' / (name + '.py')
    source = path.read_text()
    old = ('from utils.llm_backend_utils import call_llm_with_pdf' if metric == 'literature'
           else 'from utils.gemini_utils import call_gemini_with_text_prompt')
    new = ('from examples.paperwritingbench.transport import call_llm_with_pdf' if metric == 'literature'
           else 'from examples.paperwritingbench.transport import call_text as call_gemini_with_text_prompt')
    if source.count(old) != 1:
        raise ValueError('Pinned upstream import boundary changed')
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(source.replace(old, new), str(path), 'exec'), module.__dict__)
    return module


def bounded(value, low, high):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high)


def validate(metric, result):
    if not isinstance(result, dict):
        raise ValueError('Rater did not return a score object')
    if metric == 'citations':
        for key in ('overall_precision', 'overall_recall', 'overall_f1', 'p0_recall', 'p1_recall'):
            if not bounded(result.get(key), 0, 1):
                raise ValueError('Invalid citation metric: ' + key)
    elif metric == 'literature':
        axes = ['coverage_and_completeness', 'relevance_and_focus', 'critical_analysis_and_synthesis',
                'positioning_and_novelty', 'organization_and_writing', 'citation_practices_and_rigor']
        if not bounded(result.get('overall_score'), 0, 100):
            raise ValueError('Invalid literature overall score')
        for key in axes:
            axis = result.get('axis_scores', {}).get(key, {})
            if not bounded(axis.get('score'), 0, 100) or not axis.get('justification'):
                raise ValueError('Missing or invalid literature axis: ' + key)
    else:
        for key in ['Originality', 'Quality', 'Clarity', 'Significance', 'Soundness', 'Presentation', 'Contribution']:
            if not bounded(result.get(key), 1, 4):
                raise ValueError('Invalid review axis: ' + key)
        if not bounded(result.get('Overall'), 1, 10) or result.get('Decision') not in ['Accept', 'Reject']:
            raise ValueError('Invalid overall review or decision')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('metric', choices=['literature', 'review', 'citations'])
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--input-role', choices=['candidate', 'reference_control'], default='candidate')
    parser.add_argument('--ensemble', type=int, default=3)
    parser.add_argument('--model', default='gpt-6-luna')
    parser.add_argument('--model-id')
    parser.add_argument('--base-url')
    parser.add_argument('--auth-file', type=Path)
    parser.add_argument('--avg-citation-count', type=int, default=58,
                        help='Reference average from the official rubric; upstream default: 58')
    parser.add_argument('--generation-policy', choices=['provider_defaults', 'forward'], default='provider_defaults')
    parser.add_argument('--identity-cache', type=Path)
    parser.add_argument('--gt-citations', type=Path, help='Dataset ground-truth citation JSON; required for citations')
    args = parser.parse_args()
    if args.metric == 'citations' and args.gt_citations is None:
        parser.error('citations requires --gt-citations from the selected dataset sample')
    if args.ensemble < 1 or args.avg_citation_count < 1:
        parser.error('ensemble and reference average must be positive')
    if not args.pdf.is_file():
        parser.error('PDF does not exist')
    if args.out.exists():
        raise ValueError('Output already exists; use a fresh output directory')
    args.out.mkdir(parents=True)
    config = {'model': args.model, 'transport': 'lazymind_mcp',
              'generation_parameter_policy': args.generation_policy}
    transport.CONFIG = config
    transport._calls = 0
    transport.RECORD_DIR = args.out.resolve()
    transport.CALL_BUDGET = (2 if args.metric == 'citations' else
                             1 if args.metric == 'literature' or args.ensemble == 1 else args.ensemble + 1)
    started = time.time()
    status = {'status': 'running', 'metric': args.metric, 'judge': config['model'],
              'transport': config['transport'], 'input_role': args.input_role,
              'input_pdf': str(args.pdf.resolve()), 'input_sha256': hashlib.sha256(args.pdf.read_bytes()).hexdigest(),
              'upstream_commit': 'ca1b3fa01c2970fc7cda32d16245db38d57b3f56',
              'started_at_unix': started, 'ensemble': args.ensemble if args.metric == 'review' else None,
              'scoring_logic': 'Pinned upstream function and rubric; only the model-call import is adapted.',
              'visual_layout_evaluation': False,
              'generation_parameter_policy': config.get('generation_parameter_policy', 'forward')}
    if args.metric == 'literature':
        status.update(avg_citation_count=args.avg_citation_count,
                      citation_baseline_source='explicit CLI value or upstream default; not inferred from the sample')
    if args.metric == 'citations':
        status.update(scoring_logic='Pinned upstream ID-set precision/recall/F1 arithmetic.',
                      extraction='Upstream reference/title rules; two model calls with batched title extraction.',
                      identity_resolution=('Exact normalized title identity cache, otherwise upstream '
                                           'Semantic Scholar first-result lookup.'),
                      gt_citations=str(args.gt_citations.resolve()))
    (args.out / 'status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2))
    try:
        from .fetch_upstream import verify
        verify()
        client = Client(args.base_url, args.auth_file)
        model = client.model(args.model, args.model_id, args.out)
        config.update(model=model['name'], model_id=model['id'])
        transport.CLIENT = client
        status.update(judge=model['name'], model_id=model['id'])
        if args.metric == 'citations':
            from . import citations
            result = citations.score(args.pdf, args.gt_citations, config['model'], args.out, args.identity_cache)
        elif args.metric == 'literature':
            rater = load_rater(args.metric)
            result = rater.rate_single_paper(str(args.pdf), model_name=config['model'],
                                             avg_citation_count=args.avg_citation_count)
        else:
            rater = load_rater(args.metric)
            result = rater.perform_review_agentreview(transport.load_paper(str(args.pdf)), config['model'],
                                                      num_review_ensemble=args.ensemble)
        validate(args.metric, result)
        (args.out / 'score.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        score_key = {'citations': 'overall_f1', 'literature': 'overall_score', 'review': 'Overall'}[args.metric]
        status.update(status='succeeded', score=result[score_key])
    except Exception as exc:
        status.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        status['elapsed_seconds'] = round(time.time() - started, 3)
        (args.out / 'status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2))
        print(json.dumps(status, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
