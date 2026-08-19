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

_CLASSIFIER_PROMPT = '''Classify only the presentation structure for a newly requested article.
Return one compact JSON object and nothing else:
{"structure_mode":"flat|sectioned|clarify"}

Understand the request semantically in any language. Do not classify by fixed keywords or by
whether one exact phrase is present. Apply these rules in order:
1. An explicit final presentation requirement has priority over article length and every other
   signal. If the user explicitly requests chapters, sections, or subheadings, use sectioned, even
   when the requested article is at or below 1200 Chinese characters/words. If the user explicitly
   requests continuous prose or no chapters, sections, or subheadings, use flat, even when the
   requested article is longer than 1200 Chinese characters/words. A request for an outline is a
   planning requirement, not by itself a final presentation requirement: do not use outline alone
   to force sectioned; honor it in the writing plan and continue with the length/presentation rules.
2. When there is no explicit presentation requirement, use the requested output length as a
   supporting signal: at or below 1200 Chinese characters/words -> flat; above 1200 -> sectioned.
   An unquantified request for a short article -> flat; an unquantified request for a long article
   -> sectioned.
3. When neither presentation nor length is clear, use clarify. Also use clarify when explicit
   presentation requirements contradict one another, such as simultaneously requiring and
   forbidding subheadings.

The following examples illustrate the intended meaning rather than fixed phrases to match:
- "写一篇1000字的文章" -> flat.
- "写一篇1000字的文章，要有小标题" -> sectioned.
- "写一篇1000字的文章，先列大纲再写" -> flat (the outline belongs to the plan).
- "写一篇2000字的文章，不要小标题，使用连续正文" -> flat.
- "write a 900-word article with subheadings" -> sectioned.
- "write a 900-word article and provide an outline first" -> flat (the outline belongs to the plan).
- "write a 2000-word report without sections" -> flat.
- A request with no clear length or presentation structure -> clarify.
Do not treat a merely mentioned or quoted length as the requested output length.
Never infer article length from topic complexity. When the input contains an original request plus
a clarification answer, honor the clarification.'''


def _extract_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or '').strip()
    fenced = re.search(r'```(?:json)?\s*([\s\S]*?)```', text, re.I)
    if fenced:
        text = fenced.group(1).strip()

    # Some reasoning models wrap their answer in <think>...</think> and may
    # repeat JSON examples or intermediate decisions before the final object.
    # Parse every valid JSON object and use the last one, rather than slicing
    # from the first ``{`` to the last ``}``, which makes the whole response
    # invalid when reasoning contains braces of its own.
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for match in re.finditer(r'\{', text):
        try:
            raw, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            objects.append(raw)
    if not objects:
        raise ValueError('writer structure classifier returned no JSON object')
    return objects[-1]


def resolve_writer_structure_route(
    query: str,
    *,
    classifier: Callable[[str], Any],
) -> WriterStructureRoute:
    """Return one authoritative task-mode route; uncertainty always asks the user."""
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
