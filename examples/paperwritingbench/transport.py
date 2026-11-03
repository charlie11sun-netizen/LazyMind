"""Transport adapter for upstream PaperWritingBench raters; no provider secrets."""
import ast
from functools import lru_cache
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

RECORD_DIR = ROOT / 'runs'
CALL_BUDGET = 1
_calls = 0
CLIENT = None
CONFIG = {}


@lru_cache(maxsize=1)
def _parser():
    # Load the pure parser without initializing upstream's unused API client.
    source = ROOT / 'upstream/utils/openai_utils.py'
    tree = ast.parse(source.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'parse_openai_json_results')
    scope = {'json': json, 're': re}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), scope)
    return scope['parse_openai_json_results']


def parse_json(raw):
    return _parser()(raw)


def load_paper(path):
    import pymupdf
    with pymupdf.open(path) as document:
        text = ''.join(page.get_text() for page in document)
    if len(text) < 100:
        raise ValueError('PDF text is too short for evaluation')
    return text


def call_text(prompt, model_name, generation_configs=None, **_kwargs):
    global _calls
    config = CONFIG
    if model_name != config['model']:
        raise ValueError('Requested judge differs from the connected model')
    if _calls >= CALL_BUDGET:
        raise RuntimeError('Evaluation call budget exhausted; inspect the saved response')
    _calls += 1
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    generation_configs = generation_configs or {}
    messages = []
    if generation_configs.get('system_instruction'):
        messages.append({'role': 'system', 'content': generation_configs['system_instruction']})
    messages.append({'role': 'user', 'content': prompt})
    arguments = {'model_id': config['model_id'], 'messages': messages}
    if config.get('generation_parameter_policy', 'forward') == 'forward':
        arguments.update(temperature=generation_configs.get('temperature', 0), max_tokens=8192)
    record = RECORD_DIR / ('call-%03d' % _calls)
    started = time.time()
    event = {'call': _calls, 'model': model_name, 'transport': 'lazymind_mcp',
             'started_at_unix': started, 'request_file': record.name + '-request.json',
             'generation_parameter_policy': config.get('generation_parameter_policy', 'forward'),
             'requested_temperature': generation_configs.get('temperature')}
    try:
        result = CLIENT.call_tool('model.chat', arguments, record)
        if result.get('finish_reason') != 'stop':
            raise ValueError('Judge did not finish normally: ' + str(result.get('finish_reason')))
        raw = result['content'].strip()
        expect_json = _kwargs.get('return_json', True)
        parsed = parse_json(raw) if expect_json else None
        if expect_json and not isinstance(parsed, dict):
            raise ValueError('Judge must return a JSON object')
        event.update(status='succeeded', usage=result.get('usage'), response_id=result.get('id'))
        return {'raw_response': result['content'], 'parsed_response': parsed}
    except Exception as exc:
        event.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        event['elapsed_seconds'] = round(time.time() - started, 3)
        with (RECORD_DIR / 'trace.jsonl').open('a') as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')


def call_llm_with_pdf(pdf_path, prompt, model_name, system_instruction=None, temperature=0, **kwargs):
    # Match the upstream OpenAI path: extract full PDF text, then append the task.
    text = load_paper(pdf_path)
    return call_text('Paper Content:\n' + text + '\n\nTask:\n' + prompt, model_name,
                     {'system_instruction': system_instruction, 'temperature': temperature}, **kwargs)
