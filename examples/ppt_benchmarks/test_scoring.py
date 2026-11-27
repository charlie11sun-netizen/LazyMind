import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from examples.ppt_benchmarks.scoring.codex import Codex, MODEL, text
from examples.ppt_benchmarks.scoring.presentbench import boxed_verdict
from examples.ppt_benchmarks.scoring.runner import Check, Progress
from examples.ppt_benchmarks.scoring.slidesgen import grade, quiz_answers, quiz_prompt


class ScoringTests(unittest.TestCase):
    def test_fork_requires_exact_evidence_history(self):
        codex = Codex(Path.cwd())
        fork_result = {'thread': {'id': 'branch'}, 'model': MODEL, 'modelProvider': 'openai'}
        codex._request = Mock(side_effect=[fork_result, {'data': [{'id': 'warm'}]}, {}])
        with codex.branch('parent', 'warm') as branch:
            self.assertEqual(branch, 'branch')
        self.assertEqual(codex._request.call_args_list[0].args[1]['lastTurnId'], 'warm')
        self.assertEqual(codex._request.call_args_list[-1].args, ('thread/archive', {'threadId': 'branch'}))

        codex._request = Mock(side_effect=[fork_result, {'data': [{'id': 'check'}, {'id': 'warm'}]}, {}])
        with self.assertRaisesRegex(RuntimeError, 'shared evidence'), codex.branch('parent', 'warm'):
            self.fail('A prior verdict was made visible in another check')

    def test_invalid_verdict_is_retryable_and_completed_result_is_reused(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            progress = Progress(output, {'deck': 'incoming'})
            check = Check('check', [text('Question')], validate=boxed_verdict)
            usage = {'inputTokens': 1000, 'cachedInputTokens': 800, 'outputTokens': 5}
            codex = Mock()
            codex.turn.side_effect = [('turn', 'unparseable', usage), ('turn', r'\boxed{\text{yes}}', usage)]
            with self.assertRaises(ValueError):
                progress.check(codex, 'branch', check)
            self.assertEqual(progress.pending([check]), [check])
            progress.check(codex, 'branch', check)
            reopened = Progress(output, {'deck': 'incoming'})
            self.assertEqual(reopened.pending([check]), [])
            self.assertEqual(reopened.data['usage']['cacheHitRate'], 0.8)
            with self.assertRaisesRegex(ValueError, 'different scoring inputs'):
                Progress(output, {'deck': 'changed'})

    def test_quiz_prompt_excludes_answer_key(self):
        questions = [{'id': 1, 'type': 'Concept', 'question': 'First question?', 'options': ['A. One', 'B. Two'],
                      'correct_answer': 'B'}]
        prompt = quiz_prompt('{topic}\n{slide_contents}\n{quiz_questions}', ['Slide text'], questions)
        self.assertIn('A. One\nB. Two', prompt)
        self.assertFalse(prompt.endswith('\nB'))
        answer = {'question_id': 1, 'selected_answer': 'B', 'reasoning': 'Evidence'}
        answers = quiz_answers(json.dumps({'answers': [answer]}), questions)
        self.assertEqual(grade(answers, questions)['scores']['Concept'], {'correct': 1, 'total': 1})
        with self.assertRaises(ValueError):
            quiz_answers(json.dumps({'answers': [answer, answer]}), questions)


if __name__ == '__main__':
    unittest.main()
