"""SlidesGen-Bench's 50 Million Steps QuizBank and optional visual metrics."""

import json
import os
import re
import sys
from xml.etree import ElementTree
from zipfile import ZipFile

from .codex import image, text
from .runner import Check, pages, read, save


TOPIC = '50-Million-Steps-Campaign-Toolkit-2025'
ANSWER_FIELDS = {'question_id': {'type': 'integer'}, 'selected_answer': {'type': 'string'},
                 'reasoning': {'type': 'string'}}
SCHEMA = {'type': 'object', 'properties': {'answers': {'type': 'array', 'items': {
    'type': 'object', 'properties': ANSWER_FIELDS, 'required': list(ANSWER_FIELDS),
    'additionalProperties': False}}}, 'required': ['answers'], 'additionalProperties': False}


def prepare(checkout, pdf, output):
    sys.path.insert(0, str(checkout / 'eval'))
    from eval_config import QUIZ_EVALUATION_PROMPT, SINGLE_SLIDE_EXTRACTION_PROMPT

    bank = read(checkout / 'dataset/slidegen-test.json')
    sample, = (document for document in bank['documents']
               if document['meta_info']['source_path'] == f'./brand_promote/{TOPIC}/{TOPIC}.pdf')
    questions = sample['quiz_data']['quiz_bank']
    if len({q['id'] for q in questions}) != 10 or len(questions) != 10:
        raise ValueError('Expected ten distinct questions from the pinned QuizBank')
    prompt = {'extraction': SINGLE_SLIDE_EXTRACTION_PROMPT, 'quiz': QUIZ_EVALUATION_PROMPT}
    slides = pages(pdf, output / 'slide_images', dpi=150, ext='png', renderer='poppler')
    return (slides, questions, prompt), {'questions': questions, 'prompts': prompt}


def quiz_answers(answer, questions):
    answers = json.loads(answer)['answers']
    ids = [item['question_id'] for item in answers]
    expected = {q['id'] for q in questions}
    if len(ids) != len(expected) or set(ids) != expected:
        raise ValueError('Quiz answers must include each question ID exactly once')
    return answers


def quiz_prompt(template, extractions, questions):
    quiz = '\n\n'.join('\n'.join([f'Question {q["id"]} (Type: {q["type"]}):', q['question'],
                                   'Options:', *q['options']]) for q in questions)
    content = '\n'.join(answer.removeprefix('```markdown').removeprefix('```').lstrip('\n')
                        .removesuffix('```').rstrip('\n') for answer in extractions)
    return template.format(topic=TOPIC, slide_contents=content, quiz_questions=quiz)


def grade(answers, questions):
    expected = {q['id']: q for q in questions}
    details = []
    for answer in answers:
        question = expected[answer['question_id']]
        details.append({**answer, 'type': question['type'], 'question': question['question'],
                        'correct_answer': question['correct_answer'],
                        'is_correct': answer['selected_answer'] == question['correct_answer']})
    scores = {}
    for key in ('Concept', 'Data', 'Total'):
        items = [row for row in details if key == 'Total' or row['type'] == key]
        scores[key] = {'correct': sum(row['is_correct'] for row in items), 'total': len(items)}
    return {'scores': scores, 'details': details}


def run(codex, progress, prepared):
    slides, questions, prompt = prepared
    checks = [Check(f'slidesgen.extract.{index}', [text('Apply the loaded extraction rubric to this slide.'),
                                                   image(slide)])
              for index, slide in enumerate(slides, 1)]
    progress.group(codex, 'slidesgen.extract', [text(prompt['extraction'])], checks)
    quiz = Check('slidesgen.quiz', [text(quiz_prompt(prompt['quiz'],
                                                    (progress.answer(check.name) for check in checks),
                                                    questions))], SCHEMA,
                 lambda answer: quiz_answers(answer, questions))
    if progress.pending([quiz]):
        with codex.thread(ephemeral=True) as thread:
            progress.check(codex, thread, quiz)
    return grade(quiz_answers(progress.answer(quiz.name), questions), questions)


def editability_evidence(pptx):
    """Report native objects for the benchmark's manual PEI inspection."""
    slides = []
    text_tag = '{http://schemas.openxmlformats.org/drawingml/2006/main}t'
    with ZipFile(pptx) as archive:
        for name in archive.namelist():
            match = re.fullmatch(r'ppt/slides/slide(\d+)\.xml', name)
            if not match:
                continue
            tree = ElementTree.fromstring(archive.read(name))
            tags = [item.tag.rsplit('}', 1)[-1] for item in tree.iter()]
            slides.append({
                'part_number': int(match[1]),
                'native_text_runs': sum(bool((item.text or '').strip()) for item in tree.iter(text_tag)),
                'native_shapes': tags.count('sp'), 'pictures': tags.count('pic'),
                'groups': tags.count('grpSp'), 'charts': tags.count('chart'), 'svg_blips': tags.count('svgBlip'),
            })
    return {'pei': None, 'slides': sorted(slides, key=lambda slide: slide['part_number'])}


def aesthetics(checkout, slides, output, model_dir):
    os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', '1')
    os.environ.setdefault('PADDLE_PDX_CACHE_HOME', str(output / '.paddlex-cache'))
    os.environ.setdefault('MPLCONFIGDIR', str(output / '.matplotlib-cache'))
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    from paddleocr import LayoutDetection
    from aesthetics_metrics import (SlideAestheticsCalculator, calculate_total_aesthetics_score,
                                    load_parameters_from_config)

    detector = LayoutDetection(model_name='PP-DocLayout_plus-L', model_dir=str(model_dir), device='cpu')
    detection = output / 'detection'
    detection.mkdir(exist_ok=True)
    for slide in slides:
        result, = detector.predict(str(slide), batch_size=1, layout_nms=True)
        result.save_to_json(save_path=str(detection / f'{slide.stem}.json'))
    report = SlideAestheticsCalculator().calculate_presentation_metrics(
        [str(slide) for slide in slides], aggregate=True, n_workers=2)
    metrics = report['aggregated']
    required = {'figure_ground_contrast', 'colorfulness', 'color_harmony', 'subband_entropy', 'visual_hrv'}
    if not required.issubset(metrics):
        raise RuntimeError(f'Incomplete SlidesGen-Bench aesthetics metrics: {required - metrics.keys()}')
    config = load_parameters_from_config(str(checkout / 'eval/aesthetics_config.json'))
    score, components = calculate_total_aesthetics_score(metrics, config)
    save(output / 'aesthetics-metrics.json', report)
    return {'score': score, 'components': components, 'aggregated': metrics}
