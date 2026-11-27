"""Shared configuration for the two pinned generation baselines."""

import os


MODEL = 'deepseek/deepseek-v4.1-flash'
EFFORT = 'low'


def connection():
    base = os.environ.get('PPT_GENERATION_BASE_URL', '').rstrip('/')
    key = os.environ.get('PPT_GENERATION_API_KEY', '')
    if not base.startswith(('https://', 'http://')) or not key:
        raise RuntimeError('Set PPT_GENERATION_BASE_URL and PPT_GENERATION_API_KEY for the generation endpoint')
    return base, key
