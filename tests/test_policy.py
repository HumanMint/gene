import pytest

from gene.policy import validate_checkout, validate_paths


@pytest.mark.parametrize('path', ['src/gene/api.py', 'ops/compose.yml', '.github/workflows/ci.yml',
                                 'pyproject.toml', 'AGENTS.md', 'tests/test_api.py',
                                 'cogs/../src/gene/api.py', 'cogs/x/.git/config', 'cogs/bin.exe'])
def test_feature_jobs_cannot_change_control_plane(path):
    with pytest.raises(ValueError):
        validate_paths([path])


def test_feature_code_and_feature_tests_are_allowed():
    validate_paths(['cogs/ping/ping.py', 'tests/features/test_ping.py', 'docs/features/ping.md'])


@pytest.mark.parametrize('source', [
    "self.token = os.getenv('GENE_RUNTIME_TOKEN', '')",
    "token = config.runtime_token",
    "headers = {'Authorization': f'Bearer {self.token}'}",
])
def test_environment_secret_references_are_not_hardcoded_credentials(tmp_path, monkeypatch, source):
    path = tmp_path / 'cogs' / 'feature.py'
    path.parent.mkdir()
    path.write_text(source)
    monkeypatch.setattr('gene.policy.git', lambda *args: 'cogs/feature.py')
    assert validate_checkout(tmp_path) == ['cogs/feature.py']


@pytest.mark.parametrize('source', [
    "token = 'example-secret-value'",
    "token: str = 'example-secret-value'",
    "config = {'password': 'example-secret-value'}",
    "secret = 'example{secret}value'",
    "value = 'ghp_" + 'A' * 36 + "'",
])
def test_hardcoded_credentials_still_block_feature_publication(tmp_path, monkeypatch, source):
    path = tmp_path / 'cogs' / 'feature.py'
    path.parent.mkdir()
    path.write_text(source)
    monkeypatch.setattr('gene.policy.git', lambda *args: 'cogs/feature.py')
    with pytest.raises(ValueError, match='Potential credentials'):
        validate_checkout(tmp_path)
