import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from examples.ppt_benchmarks.codex_backend import CodexBridge


class CodexBackendTest(unittest.TestCase):
    def test_tool_arguments_and_context_usage_are_translated(self):
        model = 'gpt-6.2-sol'
        request = {
            'model': model,
            'messages': [{'role': 'user', 'content': 'record the title'}],
            'tools': [{'type': 'function', 'function': {'name': 'record_title', 'description': 'Save a title',
                'parameters': {'type': 'object', 'properties': {'title': {'type': 'string'}}}}}],
        }
        thread = {'model': model, 'modelProvider': 'openai', 'thread': {'id': 'thread'}}
        tool = {'method': 'item/tool/call', 'id': 7, 'params': {'threadId': 'thread',
                'tool': 'ppt_record_title', 'arguments': {'title': 'Alphabet'}}}
        usage = {'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread', 'tokenUsage': {
            'total': {'inputTokens': 90, 'outputTokens': 20, 'totalTokens': 110},
            'last': {'inputTokens': 46, 'outputTokens': 5, 'totalTokens': 51},
        }}}
        done = {'method': 'turn/completed', 'params': {'threadId': 'thread',
                'turn': {'id': 'turn', 'status': 'completed'}}}
        with TemporaryDirectory() as folder:
            bridge = CodexBridge(model, Path(folder))
            with patch.object(bridge, '_request', side_effect=[thread, {'turn': {'id': 'turn'}}]) as rpc, \
                 patch.object(bridge, '_receive', side_effect=[tool, usage, done]), \
                 patch.object(bridge, '_send') as send:
                result = bridge.complete(request)
            self.assertEqual(rpc.call_args_list[0].args[1]['dynamicTools'][0]['name'], 'ppt_record_title')
            self.assertTrue(send.call_args.args[0]['result']['success'])
        choice = result['choices'][0]
        self.assertEqual(choice['finish_reason'], 'tool_calls')
        fn = choice['message']['tool_calls'][0]['function']
        self.assertEqual(fn['name'], 'record_title')
        self.assertEqual(json.loads(fn['arguments']), {'title': 'Alphabet'})
        self.assertEqual(result['usage']['total_tokens'], 51)

    def test_unsupported_model_fails_before_starting_a_turn(self):
        bridge = CodexBridge('gpt-6.2-sol', Path('.'))
        with self.assertRaisesRegex(ValueError, 'selected model'):
            bridge.complete({'model': 'different-model', 'messages': []})


if __name__ == '__main__':
    unittest.main()
