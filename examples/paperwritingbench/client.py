"""Owner-authenticated LazyMind MCP client; provider credentials stay server-side."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
import uuid


class Client:
    def __init__(self, base_url=None, auth_file=None):
        self.base_url = (base_url or os.environ.get('LAZYMIND_BASE_URL', 'http://127.0.0.1:8091')).rstrip('/')
        self.credentials = {}
        if auth_file:
            value = json.loads(Path(auth_file).read_text())
            self.credentials.update(value.get('data', value))
        for key in ('access_token', 'refresh_token'):
            if os.environ.get('LAZYMIND_' + key.upper()):
                self.credentials[key] = os.environ['LAZYMIND_' + key.upper()]
        if not self.credentials.get('access_token'):
            raise ValueError('Set LAZYMIND_ACCESS_TOKEN or supply --auth-file with an owner session')

    def _send(self, payload):
        request = urllib.request.Request(
            self.base_url + '/api/core/mcp/capabilities/v1', data=json.dumps(payload).encode(),
            headers={'Authorization': 'Bearer ' + self.credentials['access_token'],
                     'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                     'MCP-Protocol-Version': '2025-06-18', 'X-LazyMind-Agent-Provider': 'codex'})
        return urllib.request.urlopen(request, timeout=300)

    def call_tool(self, name, arguments, record):
        record = Path(record)
        payload = {'jsonrpc': '2.0', 'id': str(uuid.uuid4()), 'method': 'tools/call',
                   'params': {'name': name, 'arguments': arguments}}
        record.with_name(record.name + '-request.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            response = self._send(payload)
        except urllib.error.HTTPError as error:
            if error.code != 401 or not self.credentials.get('refresh_token'):
                raise
            request = urllib.request.Request(
                self.base_url + '/api/authservice/auth/refresh',
                data=json.dumps({'refresh_token': self.credentials['refresh_token']}).encode(),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=30) as refreshed:
                value = json.load(refreshed)
            self.credentials.update(value.get('data', value))
            response = self._send(payload)
        with response:
            raw = response.read()
        record.with_name(record.name + '-response.private.json').write_bytes(raw)
        value = json.loads(raw)
        if value.get('error'):
            raise RuntimeError('MCP error: ' + json.dumps(value['error'], ensure_ascii=False))
        result = value['result']
        if result.get('isError'):
            raise RuntimeError('LazyMind model tool failed; inspect the saved response')
        if result.get('structuredContent') is not None:
            return result['structuredContent']
        for item in result.get('content', []):
            if item.get('type') == 'text':
                return json.loads(item['text'])
        raise RuntimeError('MCP response did not contain structured output')

    def model(self, name, model_id, directory):
        models = self.call_tool('model.list', {}, directory / 'model-list')['items']
        matches = [model for model in models if model['id'] == model_id] if model_id else [
            model for model in models if model['name'].casefold() == name.casefold()]
        if len(matches) != 1:
            raise ValueError('Select one available judge with --model or an unambiguous --model-id')
        return matches[0]
