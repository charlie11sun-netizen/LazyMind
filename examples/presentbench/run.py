"""Submit one public PresentBench case to LazyMind's native PPT workflow."""

import argparse
from base64 import b64decode, b64encode
from datetime import datetime
import hashlib
from html import unescape
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import ZipFile


def save(path, payload):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def prepare(case, output, delivery):
    instructions = case / 'generation_task' / 'instructions.md'
    materials = sorted(path for path in case.glob('material*') if path.is_file())
    if not materials:
        raise ValueError(f'No material files found in {case}')
    prepare_files(case, instructions, materials, output, delivery)


def prepare_files(case, instructions, materials, output, delivery):
    prompt = instructions.read_text(encoding='utf-8').strip()
    if delivery:
        prompt += '\n\nDelivery requirements:\n' + delivery.strip()
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'submission.json').exists():
        raise ValueError('Prepare a new output directory for a new submission')
    request = (prompt + '\n').encode()
    (output / 'request.txt').write_bytes(request)
    save(output / 'inputs.json', {
        'case': str(case.resolve()),
        'instructions_sha256': hashlib.sha256(instructions.read_bytes()).hexdigest(),
        'request_sha256': hashlib.sha256(request).hexdigest(),
        'material_files': [
            {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in materials
        ],
    })


class LazyMind:
    def __init__(self, base_url, auth_file):
        self.base_url = base_url.rstrip('/')
        auth = json.loads(auth_file.read_text())
        self.token = auth.get('data', auth)['access_token']

    def request(self, path, body=None, content_type='application/json', timeout=120):
        url = self.base_url + path
        headers = {
            'Origin': self.base_url,
            'Content-Type': content_type,
        }
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        request = Request(url, data=body, headers=headers)
        request.add_unredirected_header('Authorization', f'Bearer {self.token}')
        return urlopen(request, timeout=timeout)

    def read(self, path, body=None, content_type='application/json'):
        with self.request(path, body, content_type) as response:
            payload = json.load(response)
        return payload.get('data', payload)

    def artifact_bytes(self, value):
        if isinstance(value, str):
            return value.encode()
        if value.get('storage') == 'inline_base64':
            return b64decode(value['content_base64'], validate=True)
        if isinstance(value.get('text'), str):
            return value['text'].encode()
        path = value.get('url') or value.get('path')
        if not path:
            raise ValueError('The artifact has no inline content or download URL')
        parsed = urlsplit(path)
        expiry = parse_qs(parsed.query).get('expires', [None])[0]
        if '/static-files/' in parsed.path and (not expiry or int(expiry) < datetime.now().timestamp() + 30):
            key = '/static-files/' + parsed.path.split('/static-files/', 1)[1]
            urls = self.read('/api/core/static-files:sign', {'paths': [key]})['urls']
            if key not in urls:
                raise ValueError('Could not refresh the signed artifact URL')
            path = urls[key]
        if path.startswith('/static-files/'):
            path = '/api/core' + path
        url = urljoin(self.base_url + '/', path)
        origin = urlsplit(self.base_url)
        target = urlsplit(url)
        if (target.scheme, target.netloc) == (origin.scheme, origin.netloc):
            relative_url = target._replace(scheme='', netloc='').geturl()
            response = self.request(relative_url)
        elif target.scheme == 'https' and value.get('url'):
            response = urlopen(url, timeout=120)
        else:
            raise ValueError('Invalid artifact URL')
        with response:
            return response.read()

    def embed_slide_assets(self, html):
        pattern = re.compile(r'''(?<=["'(\s])(?:/api/core)?/static-files/[^\s"'<>\)]+''')
        paths = {path: unescape(path).removeprefix('/api/core').split('?', 1)[0]
                 for path in pattern.findall(html)}
        if not paths:
            return html
        signed = self.read('/api/core/static-files:sign', {'paths': sorted(set(paths.values()))})['urls']
        embedded = {}
        for path in set(paths.values()):
            mime = mimetypes.guess_type(path)[0] or ''
            if not mime.startswith(('image/', 'font/')) or path not in signed:
                raise ValueError(f'Could not resolve a slide asset: {path}')
            data = self.artifact_bytes({'url': signed[path]})
            embedded[path] = f'data:{mime};base64,' + b64encode(data).decode('ascii')
        return pattern.sub(lambda match: embedded[paths[match[0]]], html)

    def upload(self, paths):
        boundary = 'PresentBench' + uuid4().hex
        chunks = []
        for path in paths:
            mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            chunks.extend([
                (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                 f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n').encode(),
                path.read_bytes(), b'\r\n',
            ])
        chunks.append(f'--{boundary}--\r\n'.encode())
        uploaded = self.read('/api/core/temp/uploads', b''.join(chunks),
                             'multipart/form-data; boundary=' + boundary)['files']
        if sorted(item['filename'] for item in uploaded) != sorted(path.name for path in paths):
            raise RuntimeError('Uploaded filenames do not match the prepared inputs')
        return [{'input_type': 'file', 'uri': item['stored_path'], 'filename': item['filename']}
                for item in uploaded]


def submit(client, output, model_id, thinking_depth=None):
    state_path = output / 'submission.json'
    if state_path.exists():
        raise ValueError(f'An existing submission is recorded in {state_path}')
    inputs = json.loads((output / 'inputs.json').read_text())
    request = (output / 'request.txt').read_bytes()
    if hashlib.sha256(request).hexdigest() != inputs['request_sha256']:
        raise ValueError('The request changed after preparation')
    material_paths = [Path(item['path']) for item in inputs['material_files']]
    for path, metadata in zip(material_paths, inputs['material_files']):
        if hashlib.sha256(path.read_bytes()).hexdigest() != metadata['sha256']:
            raise ValueError(f'Material changed after preparation: {path}')
    settings = client.read('/api/core/user/chat-settings')['new_task']
    thinking_depth = thinking_depth if thinking_depth is not None else settings['thinking_depth']
    conversation_settings = dict(settings['conversation_settings'])
    conversation_settings.update(chat_executor='lazymind', enable_workflow=True,
                                 workflow_mode='auto', enable_subagent=True)
    files = client.upload(material_paths)
    payload = {
        'input': [{'input_type': 'text', 'text': request.decode()}, *files],
        'stream': True, 'trace': True, 'run_in_background': True,
        'thinking_depth': thinking_depth,
        'initial_conversation_settings': conversation_settings,
        'mentions': [{'mention_id': str(uuid4()), 'type': 'workflow',
                      'resource_id': 'builtin:ppt-workflow', 'display_name': 'AI PPT Planner'}],
    }
    if model_id:
        payload['initial_model_selection'] = {'mode': 'fixed', 'model_id': model_id, 'source': 'own'}
    with state_path.open('x') as state_file:
        json.dump({'status': 'submitting'}, state_file)
    state = {'status': 'submitting', 'base_url': client.base_url, 'model_id': model_id,
             'thinking_depth': thinking_depth, 'conversation_settings': conversation_settings}
    save(state_path, state)
    chat(client, payload, output / 'submission.private.sse', state_path, state)


def chat(client, payload, log_path, state_path, state):
    with client.request('/api/core/conversations:chat', payload) as response:
        with log_path.open('wb') as log:
            for line in response:
                log.write(line)
                if not line.startswith(b'data:') or line[5:].strip() in (b'', b'[DONE]'):
                    continue
                event = json.loads(line[5:])
                event_data = event.get('result', event.get('data', event))
                if isinstance(event_data, dict):
                    for key in ('conversation_id', 'run_id', 'error'):
                        if event_data.get(key):
                            state[key] = event_data[key]
                    if state.get('conversation_id') and state['status'] == 'submitting':
                        state['status'] = 'accepted'
                    if event_data.get('model_route', {}).get('model_name'):
                        state['model_name'] = event_data['model_route']['model_name']
                    runtime = event_data.get('runtime_event', {})
                    if runtime.get('run_id'):
                        state['run_id'] = runtime['run_id']
                    if pending := event_data.get('ask_pending'):
                        save(log_path.with_suffix('.questions.json'), pending)
                        state['status'] = 'awaiting_input'
                    save(state_path, state)
    if not state.get('conversation_id'):
        raise RuntimeError('The stream did not return a conversation ID. Inspect the saved stream before retrying.')
    print(json.dumps(state, ensure_ascii=False))


def clarify(client, output, answers):
    sources = sorted(output.glob('*.private.questions.json'), key=lambda path: path.stat().st_mtime_ns)
    if not sources:
        raise ValueError('No startup questions were saved')
    source = sources[-1]
    pending = json.loads(source.read_text())
    questions = []
    supplied = dict(answer.split('=', 1) for answer in answers)
    if set(supplied) != {question['id'] for question in pending['questions']}:
        raise ValueError('Supply one --answer per startup question')
    for question in pending['questions']:
        choices = question.get('choices', [])
        value = supplied[question['id']]
        answer = {'type': question['type'], 'value': value}
        if choices and value not in choices:
            if '__ask_user_other__' not in choices:
                raise ValueError(f'Unexpected value for {question["id"]}')
            answer.update(value='__ask_user_other__', otherText=value)
        questions.append({**question, 'custom_choices': choices, 'answer': answer})
    path = output / 'submission.json'
    state = json.loads(path.read_text())
    if state.get('status') != 'awaiting_input' or client.base_url != state.get('base_url'):
        raise ValueError('The saved submission is not awaiting its startup answers')
    payload = {
        'conversation_id': state['conversation_id'], 'stream': True, 'trace': True,
        'run_in_background': True,
        'input': [{'input_type': 'text', 'text': '\n'.join(supplied.values())}],
        'ask_answers_structured': {'ask_id': pending['ask_id'], 'questions': questions},
    }
    state['status'] = 'submitting'
    chat(client, payload, output / f'clarification-{pending["ask_id"]}.private.sse', path, state)


def status(client, output):
    state = json.loads((output / 'submission.json').read_text())
    if client.base_url != state.get('base_url'):
        raise ValueError('The server differs from the original submission')
    conversation = '/api/core/conversations/' + state['conversation_id']
    session = client.read(conversation + '/workflow-sessions:latest').get('session') or {}
    tasks = client.read(conversation + '/tasks').get('tasks', [])
    result = {
        'conversation_id': state['conversation_id'],
        'workflow': {key: session.get(key) for key in ('session_id', 'workflow_id', 'status',
                     'current_step_id', 'pinned_revision_id', 'workflow_mode', 'created_at', 'updated_at')},
        'steps': [{key: step.get(key) for key in ('step_id', 'status', 'validity')}
                  for step in session.get('steps') or []],
        'tasks': [{key: task.get(key) for key in ('task_id', 'title', 'status', 'current_phase')}
                  for task in tasks],
    }
    save(output / 'workflow.json', result)
    return result


def selected_pages(slots):
    html = [s for s in slots if s.get('selected') and s['slot'] == 'preview_html']
    notes = {s.get('sort_order', s.get('list_index')): s for s in slots
             if s.get('selected') and s['slot'] == 'preview_notes'}
    order = [s.get('sort_order', s.get('list_index')) for s in html]
    if not html or None in order or len(order) != len(set(order)):
        raise ValueError('Expected selected preview pages with unique ordered positions')
    return [(page, notes.get(position)) for position, page in sorted(zip(order, html))]


def current_native_deck(slots):
    selected = [slot for slot in slots if slot.get('selected')]
    decks = [slot for slot in selected if slot['slot'] == 'native_pptx']
    if len(decks) > 1:
        raise ValueError('The workflow selected multiple native decks')
    if not decks:
        return None
    deck = decks[0]
    attempt = deck.get('producer_attempt_id')
    created_at = datetime.fromisoformat(deck['created_at'].replace('Z', '+00:00'))
    for slot in selected:
        if slot['slot'] not in ('preview_html', 'preview_notes'):
            continue
        if (slot.get('draft_version', 1) > 1 or ((not attempt or slot.get('producer_attempt_id') != attempt)
                and datetime.fromisoformat(slot['created_at'].replace('Z', '+00:00')) > created_at)):
            return None
    return deck


def presentation_summary(path):
    namespaces = {
        'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
        'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
        'c': 'http://schemas.openxmlformats.org/drawingml/2006/chart',
    }
    counts = {'slides': 0, 'slides_with_editable_text': 0, 'text_shapes': 0,
              'native_tables': 0, 'native_charts': 0, 'pictures': 0}
    with ZipFile(path) as package:
        ElementTree.fromstring(package.read('ppt/presentation.xml'))
        for name in package.namelist():
            if not re.fullmatch(r'ppt/slides/slide\d+\.xml', name):
                continue
            root = ElementTree.fromstring(package.read(name))
            text = sum(any(node.text and node.text.strip()
                           for node in shape.findall('.//a:t', namespaces))
                       for shape in root.findall('.//p:sp', namespaces))
            counts['slides'] += 1
            counts['slides_with_editable_text'] += bool(text)
            counts['text_shapes'] += text
            for key, tag in {'native_tables': 'a:tbl', 'native_charts': 'c:chart', 'pictures': 'p:pic'}.items():
                counts[key] += len(root.findall('.//' + tag, namespaces))
    if not counts['slides']:
        raise ValueError('The supplied package contains no PowerPoint slides')
    return counts


def export(client, output, exporter):
    run = status(client, output)
    workflow = run['workflow']
    tasks = run['tasks']
    finished_generation = (tasks and tasks[-1]['title'] == 'ppt-workflow:generate_ppt'
                           and tasks[-1]['status'] == 'succeeded')
    ready = workflow['status'] == 'completed' or (workflow['status'] == 'active' and finished_generation)
    if workflow['workflow_id'] != 'ppt-workflow' or not ready:
        raise ValueError(f'Expected a published deck from a finished generation step, found {workflow}')
    path = '/api/core/workflow-sessions/' + workflow['session_id'] + '/slots'
    slots = client.read(path)['slots']
    deck = output / 'slides.pptx'
    temporary = output / 'export.pptx'
    native = current_native_deck(slots)
    expected_count = None
    if native:
        temporary.write_bytes(client.artifact_bytes(native['artifact_value']))
        export_mode = 'native_pptx'
    else:
        pages = []
        for slide, notes in selected_pages(slots):
            text = client.artifact_bytes(slide['artifact_value']).decode('utf-8')
            text = re.sub(r'<think\b[^>]*>.*?</think>', '', text, flags=re.I | re.S)
            html = re.search(r'(?:<!doctype\s+html\s*>\s*)?<html\b.*?</html>', text, re.I | re.S)
            if not html or re.search(r'id=["\'](?:root|app)["\']\s*>\s*</', html[0], re.I):
                raise ValueError(f'Invalid selected HTML slide at index {len(pages) + 1}')
            notes_text = client.artifact_bytes(notes['artifact_value']).decode('utf-8') if notes else ''
            pages.append({'html': client.embed_slide_assets(html[0]), 'notes': notes_text})
        expected_count = len(pages)
        save(output / 'export-pages.json', pages)
        if exporter:
            with TemporaryDirectory(dir=output, prefix='html-deck-') as folder:
                staged = Path(folder).resolve()
                staged_pages = staged / 'pages'
                staged_pages.mkdir()
                save(staged / 'task_pack.json', {'deck_id': 'presentbench', 'deck_dir': str(staged)})
                (staged / 'review.md').write_text('# PresentBench export\n', encoding='utf-8')
                for number, page in enumerate(pages, 1):
                    (staged_pages / f'page_{number:03}.html').write_text(page['html'], encoding='utf-8')
                    (staged_pages / f'page_{number:03}.notes.txt').write_text(page['notes'], encoding='utf-8')
                temporary.unlink(missing_ok=True)
                with (output / 'export.log').open('w') as log:
                    subprocess.run(['node', str(exporter.resolve()), '--deck-dir', str(staged),
                                    '--output', str(temporary.resolve()), '--force'],
                                   stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1200)
            export_mode = 'local_lazy_mind_exporter'
        else:
            with client.request('/api/core/exporters/html-presentation:export',
                                {'format': 'editable-pptx', 'filename': 'slides.pptx', 'pages': pages},
                                timeout=1200) as response:
                temporary.write_bytes(response.read())
            export_mode = 'server_editable_exporter'
    summary = presentation_summary(temporary)
    if expected_count is not None and summary['slides'] != expected_count:
        raise ValueError(f'Exported {summary["slides"]} slides from {expected_count} selected pages')
    temporary.replace(deck)
    summary.update(export_mode=export_mode, sha256=hashlib.sha256(deck.read_bytes()).hexdigest(),
                   workflow_session_id=workflow['session_id'], workflow_status=workflow['status'])
    if export_mode == 'local_lazy_mind_exporter':
        files = [exporter, exporter.with_name('package-lock.json'),
                 *sorted((exporter.parent / 'lib').glob('*.mjs'))]
        summary['exporter_files'] = {str(path.relative_to(exporter.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in files if path.is_file()}
    save(output / 'presentation.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_cmd = commands.add_parser('prepare')
    prepare_cmd.add_argument('case', type=Path)
    prepare_cmd.add_argument('output', type=Path)
    prepare_cmd.add_argument('--delivery', default='Deliver the final deck as an editable PowerPoint (.pptx).')
    submit_cmd = commands.add_parser('submit')
    submit_cmd.add_argument('output', type=Path)
    submit_cmd.add_argument('--model-id', required=True)
    submit_cmd.add_argument('--thinking-depth', choices=('low', 'medium', 'high'))
    clarify_cmd = commands.add_parser('clarify')
    clarify_cmd.add_argument('output', type=Path)
    clarify_cmd.add_argument('--answer', action='append', required=True)
    status_cmd = commands.add_parser('status')
    status_cmd.add_argument('output', type=Path)
    export_cmd = commands.add_parser('export')
    export_cmd.add_argument('output', type=Path)
    export_cmd.add_argument('--local-exporter', type=Path)
    for command in (submit_cmd, clarify_cmd, status_cmd, export_cmd):
        command.add_argument('--base-url', required=True)
        command.add_argument('--auth-file', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'prepare':
        prepare(args.case, args.output, args.delivery)
    else:
        scheme = urlsplit(args.base_url).scheme
        if scheme not in ('http', 'https'):
            parser.error('--base-url must be an HTTP(S) URL')
        client = LazyMind(args.base_url, args.auth_file)
        if args.command == 'submit':
            submit(client, args.output, args.model_id, args.thinking_depth)
        elif args.command == 'clarify':
            clarify(client, args.output, args.answer)
        elif args.command == 'status':
            print(json.dumps(status(client, args.output), ensure_ascii=False))
        else:
            print(json.dumps(export(client, args.output, args.local_exporter), ensure_ascii=False))


if __name__ == '__main__':
    main()
