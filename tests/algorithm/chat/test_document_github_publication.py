import pytest
from lazyllm.tools.agent import ToolExecutionError
from lazyllm.tools.fs.supplier.github import GitHubRepoFS, GitHubWikiFS

from lazymind.document_tools import invoke_document_action
from lazymind.document_tools import resources as document_resources


@pytest.fixture(autouse=True)
def github_parent_metadata(monkeypatch):
    def resolve_parent(_fs, locator):
        return {'uri': locator, 'create_pending': True}

    monkeypatch.setattr(GitHubRepoFS, 'resolve_create_parent', resolve_parent)
    monkeypatch.setattr(GitHubWikiFS, 'resolve_create_parent', resolve_parent)


@pytest.mark.parametrize('argument', ['user_input', 'parent_uri'])
@pytest.mark.parametrize('locator', [
    'https://github.com/example/articles',
    'https://github.com/example/articles/wiki',
])
def test_first_github_publication_preserves_requested_destination(monkeypatch, argument, locator):
    targets = []

    class Provider:
        def require_capability(self, capability):
            assert capability == 'replace'

        def create_document(self, *_args):
            pytest.fail('The requested GitHub destination must be resolved before writing')

        def write_document(self, converted, target, **_kwargs):
            targets.append(target)
            return {
                'doc_id': 'article.md',
                'locator': locator + '/article.md',
                'adapter': 'github',
                'persisted_document': converted.content,
                'representation': 'markdown',
            }

    monkeypatch.setattr(document_resources, 'get_writer_provider', lambda _name: Provider())
    result = invoke_document_action(
        'builtin:document.write_document.v1',
        'execute',
        {
            'converted_document': {
                'provider': 'github',
                'format': 'markdown',
                'content': '# Draft\n',
                'source_document': {'document_id': 'draft-1', 'title': 'Draft'},
            },
            argument: locator if argument == 'parent_uri' else f'将文章发布到 {locator}',
        },
    )

    assert len(targets) == 1
    assert targets[0].uri == locator
    assert targets[0].meta['create_pending'] is True
    assert targets[0].title == 'Draft'
    assert result['provider_synced'] is True


def test_github_destination_accepts_repetition_but_rejects_ambiguity():
    locator = 'https://github.com/example/articles'
    resolved = document_resources._provider_create_target(f'{locator}\n{locator}')
    assert resolved is not None
    assert resolved[0] == locator

    with pytest.raises(ToolExecutionError, match='Exactly one provider document creation target'):
        document_resources._provider_create_target(f'{locator}\nhttps://github.com/example/other')
