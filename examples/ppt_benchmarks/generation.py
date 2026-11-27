"""Shared configuration for the two pinned generation baselines."""

from contextlib import contextmanager
import os


MODEL = 'deepseek/deepseek-v4.1-flash'
CODEX_MODEL = 'gpt-6.2-sol'
EFFORT = 'low'
BACKENDS = ('api', 'codex')


def connection():
    base = os.environ.get('PPT_GENERATION_BASE_URL', '').rstrip('/')
    key = os.environ.get('PPT_GENERATION_API_KEY', '')
    if not base.startswith(('https://', 'http://')) or not key:
        raise RuntimeError('Set PPT_GENERATION_BASE_URL and PPT_GENERATION_API_KEY for the generation endpoint')
    return base, key


@contextmanager
def model_session(backend, output):
    if backend == 'api':
        base, key = connection()
        yield MODEL, base, key
        return
    if backend != 'codex':
        raise ValueError(f'Unknown generation backend: {backend}')

    from .codex_backend import CodexBridge, local_endpoint

    if output.exists():
        raise FileExistsError(f'Use a new output directory: {output}')
    work = output.with_name(output.name + '-codex')
    with CodexBridge(CODEX_MODEL, work, EFFORT) as bridge, local_endpoint(bridge) as (base, nonce):
        yield CODEX_MODEL, base, nonce
