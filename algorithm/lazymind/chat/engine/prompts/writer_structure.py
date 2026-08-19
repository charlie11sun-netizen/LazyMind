from __future__ import annotations

import json
import re
from typing import Any, Callable, Literal, Optional


WriterStructureRoute = Literal['flat', 'sectioned', 'clarify']


_STRUCTURE_QUESTION = '您希望文章使用哪种结构？'
_STRUCTURE_ANSWERS: dict[str, WriterStructureRoute] = {
    '连续正文（不使用小标题）': 'flat',
    '分章节展开': 'sectioned',
}

_EXPLICIT_LENGTH_RE = re.compile(
    r'(?P<prefix>不超过|至多|最多|约|大约|大概)?\s*'
    r'(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>万|千)?\s*字'
    r'(?P<suffix>左右|上下|以内|以下|以上)?'
)
_FLAT_STRUCTURE_RE = re.compile(
    r'连续正文|不使用(?:任何)?小标题|不使用(?:任何)?分标题|无小标题|不分章节|不要小标题',
)
_SECTIONED_STRUCTURE_RE = re.compile(
    r'分章节|分节展开|使用小标题|添加小标题|章节标题|章节结构|大纲',
)


_CLASSIFIER_PROMPT = '''Classify only the presentation structure for a newly requested article.
Return one compact JSON object and nothing else:
{"structure_mode":"flat|sectioned|clarify"}

Use flat only when the user explicitly requests no more than 1200 Chinese characters/words, a
short article within that limit, continuous prose, or no subheadings. Use sectioned only when the
user explicitly requests more than 1200 Chinese characters/words, a long-form article, chapters,
sections, subheadings, or an outline. Use clarify when neither length nor presentation structure is
explicit, or when the requirements conflict. Never infer article length from topic complexity.
When the input contains an original request plus a clarification answer, honor the clarification.'''


def _extract_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or '').strip()
    fenced = re.search(r'```(?:json)?\s*([\s\S]*?)```', text, re.I)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('writer structure classifier returned no JSON object')
    raw = json.loads(text[start:end + 1])
    if not isinstance(raw, dict):
        raise ValueError('writer structure classifier JSON must be an object')
    return raw


def _deterministic_writer_structure_route(query: str) -> Optional[WriterStructureRoute]:
    text = str(query or '').strip()
    flat_requested = bool(_FLAT_STRUCTURE_RE.search(text))
    sectioned_requested = bool(_SECTIONED_STRUCTURE_RE.search(text))
    if flat_requested and sectioned_requested:
        return None
    if flat_requested:
        return 'flat'
    if sectioned_requested:
        return None

    match = _EXPLICIT_LENGTH_RE.search(text)
    if match is None:
        return None
    suffix = match.group('suffix') or ''
    if suffix == '以上':
        return None
    multiplier = {'万': 10000, '千': 1000}.get(match.group('unit'), 1)
    target_chars = int(float(match.group('value')) * multiplier)
    return 'flat' if target_chars <= 1200 else None


def resolve_writer_structure_route(
    query: str,
    *,
    classifier: Callable[[str], Any],
) -> WriterStructureRoute:
    """Return one authoritative task-mode route; uncertainty always asks the user."""
    deterministic_route = _deterministic_writer_structure_route(query)
    if deterministic_route is not None:
        return deterministic_route
    try:
        raw = _extract_json(classifier(f'{_CLASSIFIER_PROMPT}\n\nCurrent request:\n{query[:4000]}'))
    except Exception:
        return 'clarify'
    route = str(raw.get('structure_mode') or '').strip().lower()
    if route not in {'flat', 'sectioned', 'clarify'}:
        return 'clarify'
    return route  # type: ignore[return-value]


def writer_structure_route_from_ask_answer(query: str) -> Optional[WriterStructureRoute]:
    """Map the fixed Ask User choice to its route without reclassifying the request."""
    normalized = re.sub(r'\s+', ' ', str(query or '')).strip()
    match = re.fullmatch(
        rf'{re.escape(_STRUCTURE_QUESTION)}\s*[:：]\s*(.+)',
        normalized,
    )
    if match is None:
        return None
    return _STRUCTURE_ANSWERS.get(match.group(1).strip())
