"""Bounded full-data support summaries for planning, never fabricated observations."""
from collections import Counter
import json
from typing import Literal

from pydantic import Field

from .schema import Object, Text, table_map, digest
from .plan import Filter
from .reference import select

from ..plotting.model import profile

MAX_LEVELS = 20
MAX_CELLS = 200


class ProfileQuery(Object):
    id: Text
    table_id: Text
    kind: Literal['profile', 'levels', 'cells', 'rows', 'relation']
    columns: list[Text] = Field(default_factory=list)
    group_by: list[Text] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=50, ge=1, le=200)
    right_table: Text | None = None
    right_keys: list[Text] = Field(default_factory=list)


def query_data(inputs, value):
    """Read-only, bounded queries over the actual task data; explicit paging, never inferred levels."""
    query = ProfileQuery.model_validate(value).model_dump()
    tables = table_map(inputs)
    table = tables[query['table_id']]
    columns = query['columns'] or table['columns']
    positions = [table['columns'].index(c) for c in columns]
    selected = select(table, query['filters'])
    start, end = query['offset'], query['offset'] + query['limit']
    kind = query['kind']
    if kind == 'rows':
        result = dict(columns=columns, total=len(selected), offset=start,
                      rows=[dict(source_row=i, values=[table['rows'][i][j] for j in positions])
                            for i in selected[start:end]])
    elif kind == 'profile':
        subset = {**table, 'columns': columns, 'rows': [[table['rows'][i][j] for j in positions] for i in selected],
                  'column_types': [table['column_types'][j] for j in positions],
                  'units': [table['units'][j] for j in positions]}
        result = planning_profile(subset, query['group_by'])
    elif kind in ('levels', 'cells'):
        grouping = query['group_by'] or columns
        indices = [table['columns'].index(c) for c in grouping]
        counted = {}
        for i in selected:
            values = [table['rows'][i][j] for j in indices]
            item = counted.setdefault(digest(values), dict(values=values, row_count=0,
                                     non_null_counts=dict.fromkeys(columns, 0)))
            item['row_count'] += 1
            for name, j in zip(columns, positions):
                item['non_null_counts'][name] += table['rows'][i][j] is not None
        ordered = [counted[k] for k in sorted(counted)]
        result = dict(group_by=grouping, total=len(ordered), offset=start, cells=ordered[start:end])
    else:
        if not query['right_table'] or len(query['right_keys']) != len(columns):
            raise ValueError('relation 查询需要两侧等长的显式关联键')
        right = tables[query['right_table']]
        ri = [right['columns'].index(c) for c in query['right_keys']]
        left_keys = [tuple(table['rows'][i][j] for j in positions) for i in selected]
        right_keys = [tuple(row[j] for j in ri) for row in right['rows']]
        counts = Counter(key for key in right_keys if None not in key)
        result = dict(left_rows=len(left_keys), right_rows=len(right_keys),
                      right_duplicate_keys=sum(n > 1 for n in counts.values()),
                      left_unmatched_rows=sum(None in key or key not in counts for key in left_keys),
                      left_missing_keys=sum(None in key for key in left_keys))
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 32 * 1024:
        raise ValueError('查询结果超过 32 KiB；减少 columns、limit 或缩小 filters，不能把截断部分当不存在')
    return dict(id=query['id'], result=result)


def support(values):
    counts = Counter(v for v in values if v is not None)
    result = dict(unique_count=len(counts), levels_complete=len(counts) <= MAX_LEVELS)
    if result['levels_complete']:
        result['levels'] = [dict(value=v, row_count=n) for v, n in sorted(counts.items())]
    return result


def planning_profile(table, grouping):
    result = profile({**table, 'options': {}})
    numeric = [c for c, t in zip(table['columns'], table['column_types']) if t in ('integer', 'number')]
    positions = {c: i for i, c in enumerate(table['columns'])}
    for column in numeric:
        result['columns'][column].update(support(row[positions[column]] for row in table['rows']))
    groups = list(dict.fromkeys([*grouping, *result['group_profiles'],
                                *[c for c in numeric if result['columns'][c]['levels_complete']]]))
    groups = [c for c in groups if c in positions]
    cells = {}
    for row in table['rows']:
        key = tuple(row[positions[c]] for c in groups)
        if key not in cells:
            cells[key] = dict(group=dict(zip(groups, key)), row_count=0,
                              non_null_counts=dict.fromkeys(numeric, 0)) if len(cells) < MAX_CELLS else None
        cell = cells[key]
        if cell is not None:
            cell['row_count'] += 1
            for column in numeric:
                cell['non_null_counts'][column] += row[positions[column]] is not None
    result['observed_cells'] = dict(grouping=groups, total_cells=len(cells),
                                    complete=len(cells) <= MAX_CELLS,
                                    cells=[cell for cell in cells.values() if cell is not None])
    return result
