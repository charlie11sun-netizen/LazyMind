import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from examples.presentbench.run import current_native_deck, prepare, presentation_summary, selected_pages


class PresentBenchTest(unittest.TestCase):
    def test_prepare_only_uses_generation_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case = root / 'case'
            task = case / 'generation_task'
            task.mkdir(parents=True)
            (task / 'instructions.md').write_text('Make slides.')
            (task / 'reference.txt').write_text('reference answer')
            (case / 'material.md').write_text('source facts')
            out = root / 'run'
            prepare(case, out, 'Deliver a PPTX.')
            self.assertEqual((out / 'request.txt').read_text(), 'Make slides.\n\nDelivery requirements:\nDeliver a PPTX.\n')
            manifest = json.loads((out / 'inputs.json').read_text())
            self.assertEqual([Path(item['path']).name for item in manifest['material_files']], ['material.md'])

    def test_selected_pages_keep_slide_order_and_match_notes(self):
        first = {'slot': 'preview_html', 'selected': True, 'sort_order': 0, 'list_index': 2}
        last = {'slot': 'preview_html', 'selected': True, 'sort_order': 1, 'list_index': 1}
        notes = {'slot': 'preview_notes', 'selected': True, 'sort_order': 0, 'list_index': 2}
        stale = {**first, 'selected': False}
        self.assertEqual(selected_pages([last, stale, notes, first]), [(first, notes), (last, None)])
        with self.assertRaisesRegex(ValueError, 'unique ordered positions'):
            selected_pages([first, first])

    def test_flattened_slide_is_distinguished_from_editable_text(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'slides.pptx'
            p = 'http://schemas.openxmlformats.org/presentationml/2006/main'
            a = 'http://schemas.openxmlformats.org/drawingml/2006/main'
            with ZipFile(path, 'w') as package:
                package.writestr('ppt/presentation.xml', f'<p:presentation xmlns:p="{p}"/>')
                package.writestr('ppt/slides/slide1.xml', f'<p:sld xmlns:p="{p}"><p:pic/></p:sld>')
                package.writestr('ppt/slides/slide2.xml', f'<p:sld xmlns:p="{p}" xmlns:a="{a}"><p:sp><a:t>Editable</a:t></p:sp></p:sld>')
            self.assertEqual(presentation_summary(path), {
                'slides': 2, 'slides_with_editable_text': 1, 'text_shapes': 1,
                'native_tables': 0, 'native_charts': 0, 'pictures': 1,
            })

    def test_updated_slide_invalidates_an_older_native_deck(self):
        deck = {'slot': 'native_pptx', 'selected': True, 'created_at': '2026-11-26T03:00:00Z',
                'producer_attempt_id': 'generation'}
        page = {'slot': 'preview_html', 'selected': True, 'created_at': '2026-11-26T03:05:00Z',
                'producer_attempt_id': 'generation'}
        self.assertEqual(current_native_deck([deck, page]), deck)
        self.assertIsNone(current_native_deck([deck, {**page, 'draft_version': 2}]))
        self.assertIsNone(current_native_deck([deck, {**page, 'producer_attempt_id': 'regeneration'}]))


if __name__ == '__main__':
    unittest.main()
