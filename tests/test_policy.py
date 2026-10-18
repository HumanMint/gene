import pytest

from gene.policy import validate_paths


@pytest.mark.parametrize('path', ['src/gene/api.py', 'ops/compose.yml', '.github/workflows/ci.yml',
                                 'pyproject.toml', 'AGENTS.md', 'tests/test_api.py',
                                 'cogs/../src/gene/api.py', 'cogs/x/.git/config', 'cogs/bin.exe'])
def test_feature_jobs_cannot_change_control_plane(path):
    with pytest.raises(ValueError):
        validate_paths([path])


def test_feature_code_and_feature_tests_are_allowed():
    validate_paths(['cogs/ping/ping.py', 'tests/features/test_ping.py', 'docs/features/ping.md'])

