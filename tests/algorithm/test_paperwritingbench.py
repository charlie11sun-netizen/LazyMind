"""Portable benchmark preparation, audit, authenticated transport and CLI failures."""
import io
import json
import sys
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from examples.paperwritingbench import audit_sources, client, prepare, score, transport


def test_prepare_and_source_audit_keep_results_and_exclude_reference_material(tmp_path):
    (tmp_path / 'figures').mkdir()
    (tmp_path / 'idea_sparse.md').write_text('# An idea\nInvestigate the supplied comparison.')
    log = '**Table Data: Results**\n| method | score |\n|---|---|\n| base | 80% |\n| ours | 82.38% |\n'
    (tmp_path / 'experimental_log.md').write_text(log)
    (tmp_path / 'figures/info.json').write_text('[{"name":"figure.png","caption":"Supplied caption"}]')
    (tmp_path / 'figures/figure.png').write_bytes(b'original image')
    (tmp_path / 'original_paper_gt_citations.json').write_text('THIS MUST NOT ENTER WRITING INPUT')
    value, manifest = prepare.prepare(
        tmp_path, exclude_titles=['Held-out Paper'], exclude_arxiv=['2504.11739'], cutoff='2025-06-30')
    assert value['experiment']['report'] == log
    assert value['data']['tables'][0]['table']['rows'] == [['base', 80.0], ['ours', 82.38]]
    assert value['result_meta']['data_origin'] == 'user_supplied'
    assert value['experiment']['executed'] is False
    assert 'THIS MUST NOT' not in json.dumps(value)
    assert manifest['upload_with_json'] == ['figures/figure.png']
    assert all(scope in manifest['workflow_request'] for scope in ('Held-out Paper', '2504.11739', '2025-06-30'))
    papers = [{'id': 'target', 'title': 'Held-out Paper', 'url': 'https://arxiv.org/abs/2504.11739v2',
               'metadata': {'published': '2025-04-01'}},
              {'id': 'future', 'title': 'Future Paper', 'metadata': {'published': '2026-01-01'}}]
    pool = {'materials': [{'kind': 'papers', 'content': papers}]}
    result = audit_sources.audit(pool, manifest)
    assert {v['reason'] for v in result['violations']} == {'excluded_target', 'published_after_cutoff'}
    pool['materials'][0]['content'] = [{'id': 'allowed', 'title': 'Earlier Paper',
                                       'metadata': {'published': '2025-01-01'}}]
    assert audit_sources.audit(pool, manifest)['status'] == 'passed'


def test_mcp_refreshes_once_and_records_no_authentication_headers(tmp_path, monkeypatch):
    monkeypatch.setenv('LAZYMIND_ACCESS_TOKEN', 'old-owner-session')
    monkeypatch.setenv('LAZYMIND_REFRESH_TOKEN', 'refresh-secret')
    requests = []

    def send(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            raise HTTPError(request.full_url, 401, 'expired', {}, None)
        if request.full_url.endswith('/refresh'):
            return io.BytesIO(b'{"data":{"access_token":"new-owner-session"}}')
        return io.BytesIO(b'{"result":{"structuredContent":{"items":[]}}}')

    monkeypatch.setattr(client.urllib.request, 'urlopen', send)
    api = client.Client('http://localhost:8091')
    assert api.call_tool('model.list', {}, tmp_path / 'models') == {'items': []}
    assert len(requests) == 3
    assert requests[-1].get_header('Authorization') == 'Bearer new-owner-session'
    saved = ''.join(path.read_text() for path in tmp_path.iterdir())
    assert all(secret not in saved for secret in ('old-owner-session', 'refresh-secret', 'new-owner-session'))


def test_cli_preserves_failed_run_and_forwards_explicit_literature_baseline(tmp_path, monkeypatch):
    from examples.paperwritingbench import fetch_upstream

    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'fixture PDF; the rater boundary is replaced, not text extraction')
    out = tmp_path / 'score'
    api = Mock()
    api.model.return_value = {'id': 'judge-id', 'name': 'gpt-6-luna'}
    monkeypatch.setattr(score, 'Client', Mock(return_value=api))
    monkeypatch.setattr(fetch_upstream, 'verify', lambda: None)
    rater = Mock()
    rater.rate_single_paper.side_effect = ValueError('malformed score')
    monkeypatch.setattr(score, 'load_rater', lambda metric: rater)
    monkeypatch.setattr(sys, 'argv', [
        'score', 'literature', str(pdf), '--out', str(out), '--avg-citation-count', '50'])
    with pytest.raises(ValueError, match='malformed score'):
        score.main()
    status = json.loads((out / 'status.json').read_text())
    assert status['status'] == 'failed' and status['avg_citation_count'] == 50
    assert not (out / 'score.json').exists()
    assert rater.rate_single_paper.call_args.kwargs['avg_citation_count'] == 50


def test_transport_supports_plain_extraction_and_enforces_call_budget(tmp_path, monkeypatch):
    api = Mock()
    api.call_tool.return_value = {'content': '[1] Provided reference', 'finish_reason': 'stop'}
    monkeypatch.setattr(transport, 'CLIENT', api)
    monkeypatch.setattr(transport, 'CONFIG', {
        'model': 'judge', 'model_id': 'id', 'generation_parameter_policy': 'provider_defaults'})
    monkeypatch.setattr(transport, 'RECORD_DIR', tmp_path)
    monkeypatch.setattr(transport, 'CALL_BUDGET', 1)
    monkeypatch.setattr(transport, '_calls', 0)
    assert transport.call_text('extract', 'judge', return_json=False)['parsed_response'] is None
    assert 'temperature' not in api.call_tool.call_args.args[1]
    with pytest.raises(RuntimeError, match='budget exhausted'):
        transport.call_text('retry', 'judge')


def test_citation_rate_limit_retains_query_failure_instead_of_a_score(tmp_path, monkeypatch):
    from examples.paperwritingbench import citations

    request = Mock(side_effect=HTTPError('url', 429, 'rate limited', {}, None))
    monkeypatch.setattr(citations.urllib.request, 'urlopen', request)
    monkeypatch.setattr(citations.time, 'sleep', lambda seconds: None)
    with pytest.raises(RuntimeError, match='no score produced'):
        citations.lookup('Candidate reference', tmp_path, 1)
    assert request.call_count == 3
    assert json.loads((tmp_path / 'lookup-001.json').read_text())['attempts'] == [{'status': 429}] * 3
