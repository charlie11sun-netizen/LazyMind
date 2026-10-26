"""Interpretation prose accepts numbers while retaining result-reference validation."""
import copy

import pytest

from lazymind.document_tools.paper.analysis.interpretation import validate


def test_numbers_are_allowed_in_every_interpretation_field():
    analysis = {'targets': [{'id': 'mean'}], 'findings': [{'id': 'm', 'target_id': 'mean'}]}
    value = {'summary': '2026 年的 F4 分析包含 7B 模型，准确率为 0.8。',
             'answers': [{'target_id': 'mean', 'conclusion': '第 1 组有 1e3 个样本，报告 95% 区间。',
                          'finding_ids': ['m'], 'key_finding_ids': ['m']}],
             'mechanism_explanation': '步骤 2 的设置不能证明因果关系。',
             'limitations': ['仅覆盖 3 个场景；差值为 -0.2，不能外推。']}
    result = validate(value, analysis, 'zh-CN')
    assert result == value
    invalid = copy.deepcopy(value)
    invalid['answers'][0]['finding_ids'] = ['missing']
    with pytest.raises(ValueError, match='解释引用未知'):
        validate(invalid, analysis, 'zh-CN')
